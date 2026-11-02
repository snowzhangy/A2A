// WASAPI host for the shared A2A engine. See a2a_wasapi.h for the threading contract.
//
// Capture uses process loopback (AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK), which taps
// one process tree, or everything except it, without a virtual device. Render uses the
// default console endpoint in shared, event-driven mode. Both clients run at the engine's
// float format with AUTOCONVERTPCM, so the device mix rate never reaches the engine.

#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>

#include <audioclient.h>
#include <audioclientactivationparams.h>
#include <audiopolicy.h>
#include <avrt.h>
#include <mmdeviceapi.h>
#include <initguid.h>
#include <functiondiscoverykeys_devpkey.h>

#include <atomic>
#include <cstdio>
#include <cstring>

#include "a2a_wasapi.h"

namespace {

// Engine blocks are bounded well below the engine's max_block_frames (4096 in the runtime).
constexpr UINT32 kChunk = 1024;
constexpr UINT32 kZeroFrames = kChunk;

template <class T> void release(T *&p) {
    if (p) { p->Release(); p = nullptr; }
}

uint64_t qpc_100ns() {
    static LARGE_INTEGER frequency = [] { LARGE_INTEGER f; QueryPerformanceFrequency(&f); return f; }();
    LARGE_INTEGER c;
    QueryPerformanceCounter(&c);
    const uint64_t f = (uint64_t)frequency.QuadPart, v = (uint64_t)c.QuadPart;
    return (v / f) * 10000000ull + (v % f) * 10000000ull / f;
}

void utf8(const wchar_t *w, char *out, int capacity) {
    if (!capacity) return;
    out[0] = 0;
    if (w) WideCharToMultiByte(CP_UTF8, 0, w, -1, out, capacity, nullptr, nullptr);
    out[capacity - 1] = 0;
}

void friendly_name(IMMDevice *device, char *out, int capacity) {
    out[0] = 0;
    IPropertyStore *props = nullptr;
    if (FAILED(device->OpenPropertyStore(STGM_READ, &props))) return;
    PROPVARIANT value;
    PropVariantInit(&value);
    if (SUCCEEDED(props->GetValue(PKEY_Device_FriendlyName, &value)) && value.vt == VT_LPWSTR) {
        utf8(value.pwszVal, out, capacity);
    }
    PropVariantClear(&value);
    props->Release();
}

/// COM on the calling thread for the session helpers; tolerates an existing STA.
struct ComScope {
    bool owned;
    ComScope() { owned = SUCCEEDED(CoInitializeEx(nullptr, COINIT_MULTITHREADED)); }
    ~ComScope() { if (owned) CoUninitialize(); }
};

/// Process-loopback activation completes asynchronously; the handler must be agile.
class ActivationHandler final : public IActivateAudioInterfaceCompletionHandler, public IAgileObject {
public:
    ActivationHandler() { done_ = CreateEventW(nullptr, TRUE, FALSE, nullptr); }

    STDMETHODIMP QueryInterface(REFIID riid, void **out) override {
        if (riid == __uuidof(IUnknown) || riid == __uuidof(IActivateAudioInterfaceCompletionHandler)) {
            *out = static_cast<IActivateAudioInterfaceCompletionHandler *>(this);
        } else if (riid == __uuidof(IAgileObject)) {
            *out = static_cast<IAgileObject *>(this);
        } else {
            *out = nullptr;
            return E_NOINTERFACE;
        }
        AddRef();
        return S_OK;
    }
    STDMETHODIMP_(ULONG) AddRef() override { return (ULONG)InterlockedIncrement(&refs_); }
    STDMETHODIMP_(ULONG) Release() override {
        LONG r = InterlockedDecrement(&refs_);
        if (!r) delete this;
        return (ULONG)r;
    }

    STDMETHODIMP ActivateCompleted(IActivateAudioInterfaceAsyncOperation *operation) override {
        HRESULT activated = E_FAIL;
        IUnknown *unknown = nullptr;
        HRESULT hr = operation->GetActivateResult(&activated, &unknown);
        if (SUCCEEDED(hr) && SUCCEEDED(activated) && unknown) {
            result = unknown->QueryInterface(__uuidof(IAudioClient), reinterpret_cast<void **>(&client));
        } else {
            result = FAILED(hr) ? hr : activated;
        }
        release(unknown);
        SetEvent(done_);
        return S_OK;
    }

    bool wait(DWORD ms) const { return WaitForSingleObject(done_, ms) == WAIT_OBJECT_0; }

    HRESULT result = E_PENDING;
    IAudioClient *client = nullptr;

private:
    ~ActivationHandler() {
        release(client);
        CloseHandle(done_);
    }
    LONG refs_ = 1;
    HANDLE done_;
};

}  // namespace

struct a2a_wasapi {
    a2a_engine *engine = nullptr;
    a2a_wasapi_config cfg{};
    HANDLE thread = nullptr;
    HANDLE stop_event = nullptr;
    HANDLE ready_event = nullptr;

    std::atomic<int32_t> state{A2A_WASAPI_STARTING};
    std::atomic<int32_t> hresult{0};
    std::atomic<uint64_t> capture_packets{0}, capture_frames{0}, capture_silent{0}, capture_discontinuities{0};
    std::atomic<uint64_t> capture_ts_errors{0}, render_wakeups{0}, render_frames{0}, render_full{0}, timeouts{0};
    std::atomic<uint64_t> last_capture_ns{0};
    std::atomic<float> capture_gain{1.0f};

    // Set before the thread starts.
    wchar_t render_id[256] = {};

    // Written by the engine thread before it signals `ready_event` (or before a failure state).
    uint32_t render_buffer_frames = 0, mix_rate = 0, mix_channels = 0;
    double latency_ms = 0;
    char message[256] = {};
    char device[256] = {};

    // Preallocated before the loop: the silent-packet source and the gain scratch.
    float zeros[kZeroFrames * 8] = {};
    float scaled[kChunk * 8] = {};
};

namespace {

void fail(a2a_wasapi *s, HRESULT hr, const char *what) {
    std::snprintf(s->message, sizeof s->message, "%s (HRESULT 0x%08lX)", what, (unsigned long)hr);
    s->hresult.store((int32_t)hr, std::memory_order_relaxed);
    s->state.store(A2A_WASAPI_FAILED, std::memory_order_release);
}

WAVEFORMATEX engine_format(const a2a_wasapi_config &c) {
    WAVEFORMATEX f{};
    f.wFormatTag = WAVE_FORMAT_IEEE_FLOAT;
    f.nChannels = (WORD)c.channels;
    f.nSamplesPerSec = c.sample_rate;
    f.wBitsPerSample = 32;
    f.nBlockAlign = (WORD)(c.channels * 4);
    f.nAvgBytesPerSec = c.sample_rate * c.channels * 4;
    f.cbSize = 0;
    return f;
}

HRESULT open_render(a2a_wasapi *s, HANDLE event, IAudioClient **client, IAudioRenderClient **render,
                    REFERENCE_TIME *latency) {
    IMMDeviceEnumerator *enumerator = nullptr;
    IMMDevice *device = nullptr;
    HRESULT hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator),
                                  reinterpret_cast<void **>(&enumerator));
    if (SUCCEEDED(hr)) {
        hr = s->render_id[0] ? enumerator->GetDevice(s->render_id, &device)
                             : enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &device);
    }
    if (SUCCEEDED(hr)) {
        friendly_name(device, s->device, sizeof s->device);
        hr = device->Activate(__uuidof(IAudioClient), CLSCTX_ALL, nullptr, reinterpret_cast<void **>(client));
    }
    if (SUCCEEDED(hr)) {
        WAVEFORMATEX *mix = nullptr;
        if (SUCCEEDED((*client)->GetMixFormat(&mix)) && mix) {
            s->mix_rate = mix->nSamplesPerSec;
            s->mix_channels = mix->nChannels;
            CoTaskMemFree(mix);
        }
        WAVEFORMATEX format = engine_format(s->cfg);
        const REFERENCE_TIME duration = (REFERENCE_TIME)s->cfg.render_buffer_ms * 10000;
        hr = (*client)->Initialize(AUDCLNT_SHAREMODE_SHARED,
                                   AUDCLNT_STREAMFLAGS_EVENTCALLBACK | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                                       | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                                   duration, 0, &format, nullptr);
    }
    if (SUCCEEDED(hr)) hr = (*client)->SetEventHandle(event);
    if (SUCCEEDED(hr)) hr = (*client)->GetBufferSize(&s->render_buffer_frames);
    if (SUCCEEDED(hr)) {
        if (FAILED((*client)->GetStreamLatency(latency))) *latency = 0;
        s->latency_ms = (double)*latency / 10000.0;
        hr = (*client)->GetService(__uuidof(IAudioRenderClient), reinterpret_cast<void **>(render));
    }
    if (SUCCEEDED(hr)) {
        // Start on silence so the first period cannot underrun.
        BYTE *data = nullptr;
        if (SUCCEEDED((*render)->GetBuffer(s->render_buffer_frames, &data))) {
            (*render)->ReleaseBuffer(s->render_buffer_frames, AUDCLNT_BUFFERFLAGS_SILENT);
        }
    }
    release(device);
    release(enumerator);
    return hr;
}

HRESULT open_capture(a2a_wasapi *s, HANDLE event, IAudioClient **client, IAudioCaptureClient **capture) {
    AUDIOCLIENT_ACTIVATION_PARAMS params{};
    params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK;
    params.ProcessLoopbackParams.TargetProcessId = s->cfg.target_pid;
    params.ProcessLoopbackParams.ProcessLoopbackMode = s->cfg.loopback_mode == A2A_LOOPBACK_EXCLUDE_TREE
        ? PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
        : PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE;
    PROPVARIANT activation{};
    activation.vt = VT_BLOB;
    activation.blob.cbSize = sizeof(params);
    activation.blob.pBlobData = reinterpret_cast<BYTE *>(&params);

    auto *handler = new ActivationHandler();
    IActivateAudioInterfaceAsyncOperation *operation = nullptr;
    HRESULT hr = ActivateAudioInterfaceAsync(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, __uuidof(IAudioClient), &activation,
                                             handler, &operation);
    if (SUCCEEDED(hr)) {
        hr = handler->wait(10000) ? handler->result : HRESULT_FROM_WIN32(ERROR_TIMEOUT);
        if (SUCCEEDED(hr)) {
            *client = handler->client;
            handler->client = nullptr;
        }
    }
    release(operation);
    handler->Release();
    if (SUCCEEDED(hr)) {
        WAVEFORMATEX format = engine_format(s->cfg);
        const REFERENCE_TIME duration = (REFERENCE_TIME)s->cfg.capture_buffer_ms * 10000;
        hr = (*client)->Initialize(AUDCLNT_SHAREMODE_SHARED,
                                   AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                                       | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
                                   duration, 0, &format, nullptr);
    }
    if (SUCCEEDED(hr)) hr = (*client)->SetEventHandle(event);
    if (SUCCEEDED(hr)) hr = (*client)->GetService(__uuidof(IAudioCaptureClient), reinterpret_cast<void **>(capture));
    return hr;
}

// RT: drains every pending capture packet into the engine.
HRESULT drain_capture(a2a_wasapi *s, IAudioCaptureClient *capture) {
    const uint32_t ch = s->cfg.channels, rate = s->cfg.sample_rate;
    UINT32 next = 0;
    HRESULT hr;
    while (SUCCEEDED(hr = capture->GetNextPacketSize(&next)) && next) {
        BYTE *data = nullptr;
        UINT32 frames = 0;
        DWORD flags = 0;
        UINT64 position = 0, qpc = 0;
        hr = capture->GetBuffer(&data, &frames, &flags, &position, &qpc);
        if (FAILED(hr)) return hr;
        AudioTimeStamp ts{};
        if (qpc && !(flags & AUDCLNT_BUFFERFLAGS_TIMESTAMP_ERROR)) {
            ts.mHostTime = qpc;
            ts.mFlags = kAudioTimeStampHostTimeValid;
        } else {
            s->capture_ts_errors.fetch_add(1, std::memory_order_relaxed);
        }
        if (flags & AUDCLNT_BUFFERFLAGS_DATA_DISCONTINUITY) s->capture_discontinuities.fetch_add(1, std::memory_order_relaxed);
        const bool silent = (flags & AUDCLNT_BUFFERFLAGS_SILENT) != 0 || !data;
        if (silent) s->capture_silent.fetch_add(1, std::memory_order_relaxed);
        const float gain = s->capture_gain.load(std::memory_order_relaxed);
        for (UINT32 offset = 0; offset < frames; offset += kChunk) {
            const UINT32 n = frames - offset < kChunk ? frames - offset : kChunk;
            const float *in = silent ? s->zeros : reinterpret_cast<const float *>(data) + (size_t)offset * ch;
            if (!silent && gain != 1.0f) {
                // Bounded, preallocated scaling: restores a ducked source to its own level.
                const size_t count = (size_t)n * ch;
                for (size_t k = 0; k < count; k++) {
                    const float v = in[k] * gain;
                    s->scaled[k] = v > 1.0f ? 1.0f : (v < -1.0f ? -1.0f : v);
                }
                in = s->scaled;
            }
            AudioTimeStamp t = ts;
            if (t.mFlags) t.mHostTime += (uint64_t)offset * 10000000ull / rate;
            a2a_engine_process(s->engine, in, n, &t, nullptr, 0, nullptr);
        }
        capture->ReleaseBuffer(frames);
        s->capture_packets.fetch_add(1, std::memory_order_relaxed);
        s->capture_frames.fetch_add(frames, std::memory_order_relaxed);
        s->last_capture_ns.store(a2a_now_ns(), std::memory_order_relaxed);
    }
    return hr;
}

// RT: fills the free part of the render buffer from the engine mixer.
HRESULT fill_render(a2a_wasapi *s, IAudioClient *client, IAudioRenderClient *render, REFERENCE_TIME latency) {
    const uint32_t ch = s->cfg.channels, rate = s->cfg.sample_rate;
    UINT32 padding = 0;
    HRESULT hr = client->GetCurrentPadding(&padding);
    if (FAILED(hr)) return hr;
    const UINT32 room = s->render_buffer_frames - padding;
    s->render_wakeups.fetch_add(1, std::memory_order_relaxed);
    if (!room) {
        s->render_full.fetch_add(1, std::memory_order_relaxed);
        return S_OK;
    }
    BYTE *data = nullptr;
    hr = render->GetBuffer(room, &data);
    if (FAILED(hr)) return hr;
    // The first new frame is heard after the queued padding plus the stream latency.
    const uint64_t presentation = qpc_100ns() + (uint64_t)padding * 10000000ull / rate + (uint64_t)latency;
    for (UINT32 offset = 0; offset < room; offset += kChunk) {
        const UINT32 n = room - offset < kChunk ? room - offset : kChunk;
        AudioTimeStamp ts{};
        ts.mHostTime = presentation + (uint64_t)offset * 10000000ull / rate;
        ts.mFlags = kAudioTimeStampHostTimeValid;
        a2a_engine_process(s->engine, nullptr, 0, nullptr, reinterpret_cast<float *>(data) + (size_t)offset * ch, n, &ts);
    }
    hr = render->ReleaseBuffer(room, 0);
    s->render_frames.fetch_add(room, std::memory_order_relaxed);
    return hr;
}

DWORD WINAPI engine_thread(void *arg) {
    auto *s = static_cast<a2a_wasapi *>(arg);
    const bool com = SUCCEEDED(CoInitializeEx(nullptr, COINIT_MULTITHREADED));
    DWORD task = 0;
    HANDLE mmcss = AvSetMmThreadCharacteristicsW(L"Pro Audio", &task);
    HANDLE capture_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    HANDLE render_event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    IAudioClient *capture_client = nullptr, *render_client = nullptr;
    IAudioCaptureClient *capture = nullptr;
    IAudioRenderClient *render = nullptr;
    REFERENCE_TIME latency = 0;
    HRESULT hr = S_OK;

    if (s->cfg.render) {
        hr = open_render(s, render_event, &render_client, &render, &latency);
        if (FAILED(hr)) fail(s, hr, "could not open the default output device");
    }
    if (SUCCEEDED(hr) && s->cfg.target_pid) {
        hr = open_capture(s, capture_event, &capture_client, &capture);
        if (FAILED(hr)) fail(s, hr, "process loopback capture could not start");
    }
    if (SUCCEEDED(hr) && capture_client) {
        hr = capture_client->Start();
        if (FAILED(hr)) fail(s, hr, "capture start failed");
    }
    if (SUCCEEDED(hr) && render_client) {
        hr = render_client->Start();
        if (FAILED(hr)) fail(s, hr, "render start failed");
    }
    if (SUCCEEDED(hr)) s->state.store(A2A_WASAPI_RUNNING, std::memory_order_release);
    SetEvent(s->ready_event);

    if (SUCCEEDED(hr)) {
        HANDLE waits[3] = {s->stop_event, capture_event, render_event};
        for (;;) {
            const DWORD w = WaitForMultipleObjects(3, waits, FALSE, 200);
            if (w == WAIT_OBJECT_0) break;
            if (w == WAIT_TIMEOUT) s->timeouts.fetch_add(1, std::memory_order_relaxed);
            if (capture) {
                hr = drain_capture(s, capture);
                if (FAILED(hr)) {
                    fail(s, hr, hr == AUDCLNT_E_DEVICE_INVALIDATED ? "capture device invalidated" : "capture failed");
                    break;
                }
            }
            if (render) {
                hr = fill_render(s, render_client, render, latency);
                if (FAILED(hr)) {
                    fail(s, hr, hr == AUDCLNT_E_DEVICE_INVALIDATED ? "output device changed or removed" : "render failed");
                    break;
                }
            }
        }
    }

    if (capture_client) capture_client->Stop();
    if (render_client) render_client->Stop();
    release(capture);
    release(render);
    release(capture_client);
    release(render_client);
    CloseHandle(capture_event);
    CloseHandle(render_event);
    if (mmcss) AvRevertMmThreadCharacteristics(mmcss);
    if (com) CoUninitialize();
    int32_t expected = A2A_WASAPI_RUNNING;
    s->state.compare_exchange_strong(expected, A2A_WASAPI_STOPPED);
    return 0;
}

template <class Visit> HRESULT for_each_session(Visit visit) {
    ComScope com;
    IMMDeviceEnumerator *enumerator = nullptr;
    IMMDeviceCollection *devices = nullptr;
    HRESULT hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator),
                                  reinterpret_cast<void **>(&enumerator));
    if (SUCCEEDED(hr)) hr = enumerator->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &devices);
    UINT count = 0;
    if (SUCCEEDED(hr)) hr = devices->GetCount(&count);
    for (UINT i = 0; SUCCEEDED(hr) && i < count; i++) {
        IMMDevice *device = nullptr;
        IAudioSessionManager2 *manager = nullptr;
        IAudioSessionEnumerator *sessions = nullptr;
        char endpoint[128];
        if (FAILED(devices->Item(i, &device))) continue;
        friendly_name(device, endpoint, sizeof endpoint);
        if (SUCCEEDED(device->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr,
                                       reinterpret_cast<void **>(&manager)))
            && SUCCEEDED(manager->GetSessionEnumerator(&sessions))) {
            int n = 0;
            sessions->GetCount(&n);
            for (int j = 0; j < n; j++) {
                IAudioSessionControl *control = nullptr;
                IAudioSessionControl2 *control2 = nullptr;
                ISimpleAudioVolume *volume = nullptr;
                if (SUCCEEDED(sessions->GetSession(j, &control))
                    && SUCCEEDED(control->QueryInterface(__uuidof(IAudioSessionControl2), reinterpret_cast<void **>(&control2)))
                    && SUCCEEDED(control->QueryInterface(__uuidof(ISimpleAudioVolume), reinterpret_cast<void **>(&volume)))) {
                    visit(control2, volume, endpoint);
                }
                release(volume);
                release(control2);
                release(control);
            }
        }
        release(sessions);
        release(manager);
        release(device);
    }
    release(devices);
    release(enumerator);
    return hr;
}

}  // namespace

extern "C" {

a2a_wasapi *a2a_wasapi_start(a2a_engine *engine, const a2a_wasapi_config *config) {
    if (!engine || !config || !config->channels || config->channels > 8 || !config->sample_rate) return nullptr;
    auto *s = new a2a_wasapi();
    s->engine = engine;
    s->cfg = *config;
    if (!s->cfg.capture_buffer_ms) s->cfg.capture_buffer_ms = 200;
    if (!s->cfg.render_buffer_ms) s->cfg.render_buffer_ms = 20;
    s->capture_gain.store(config->capture_gain > 0 ? config->capture_gain : 1.0f);
    if (config->render_device_id && config->render_device_id[0]) {
        MultiByteToWideChar(CP_UTF8, 0, config->render_device_id, -1, s->render_id, 255);
    }
    s->cfg.render_device_id = nullptr;  // the caller's string need not outlive this call
    a2a_time_init();
    s->stop_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    s->ready_event = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    s->thread = CreateThread(nullptr, 0, engine_thread, s, 0, nullptr);
    if (!s->thread) {
        CloseHandle(s->stop_event);
        CloseHandle(s->ready_event);
        delete s;
        return nullptr;
    }
    if (WaitForSingleObject(s->ready_event, 15000) != WAIT_OBJECT_0) {
        fail(s, HRESULT_FROM_WIN32(ERROR_TIMEOUT), "audio clients did not start");
    }
    return s;
}

void a2a_wasapi_get_stats(a2a_wasapi *s, a2a_wasapi_stats *out) {
    std::memset(out, 0, sizeof *out);
    out->state = s->state.load(std::memory_order_acquire);
    out->hresult = s->hresult.load(std::memory_order_relaxed);
    out->capture_packets = s->capture_packets.load(std::memory_order_relaxed);
    out->capture_frames = s->capture_frames.load(std::memory_order_relaxed);
    out->capture_silent_packets = s->capture_silent.load(std::memory_order_relaxed);
    out->capture_discontinuities = s->capture_discontinuities.load(std::memory_order_relaxed);
    out->capture_timestamp_errors = s->capture_ts_errors.load(std::memory_order_relaxed);
    out->render_wakeups = s->render_wakeups.load(std::memory_order_relaxed);
    out->render_frames = s->render_frames.load(std::memory_order_relaxed);
    out->render_full_wakeups = s->render_full.load(std::memory_order_relaxed);
    out->timeouts = s->timeouts.load(std::memory_order_relaxed);
    out->last_capture_ns = s->last_capture_ns.load(std::memory_order_relaxed);
    out->render_buffer_frames = s->render_buffer_frames;
    out->render_mix_rate = s->mix_rate;
    out->render_mix_channels = s->mix_channels;
    out->render_latency_ms = s->latency_ms;
    std::memcpy(out->message, s->message, sizeof out->message);
    std::memcpy(out->render_device, s->device, sizeof out->render_device);
}

void a2a_wasapi_stop(a2a_wasapi *s) {
    if (!s) return;
    SetEvent(s->stop_event);
    WaitForSingleObject(s->thread, INFINITE);
    CloseHandle(s->thread);
    CloseHandle(s->stop_event);
    CloseHandle(s->ready_event);
    delete s;
}

void a2a_wasapi_set_capture_gain(a2a_wasapi *s, float gain) {
    if (s && gain > 0) s->capture_gain.store(gain, std::memory_order_relaxed);
}

int32_t a2a_wasapi_list_endpoints(a2a_endpoint_info *out, int32_t max) {
    ComScope com;
    IMMDeviceEnumerator *enumerator = nullptr;
    IMMDeviceCollection *devices = nullptr;
    IMMDevice *fallback = nullptr;
    LPWSTR default_id = nullptr;
    int32_t written = 0;
    HRESULT hr = CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator),
                                  reinterpret_cast<void **>(&enumerator));
    if (SUCCEEDED(hr) && SUCCEEDED(enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &fallback))) {
        fallback->GetId(&default_id);
    }
    if (SUCCEEDED(hr)) hr = enumerator->EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE, &devices);
    UINT count = 0;
    if (SUCCEEDED(hr)) hr = devices->GetCount(&count);
    for (UINT i = 0; SUCCEEDED(hr) && i < count && written < max; i++) {
        IMMDevice *device = nullptr;
        LPWSTR id = nullptr;
        if (FAILED(devices->Item(i, &device))) continue;
        if (SUCCEEDED(device->GetId(&id))) {
            a2a_endpoint_info &info = out[written++];
            std::memset(&info, 0, sizeof info);
            utf8(id, info.id, sizeof info.id);
            friendly_name(device, info.name, sizeof info.name);
            info.is_default = default_id && wcscmp(id, default_id) == 0;
            CoTaskMemFree(id);
        }
        release(device);
    }
    if (default_id) CoTaskMemFree(default_id);
    release(fallback);
    release(devices);
    release(enumerator);
    return FAILED(hr) ? (int32_t)hr : written;
}

int32_t a2a_wasapi_list_sessions(a2a_session_info *out, int32_t max) {
    int32_t written = 0;
    HRESULT hr = for_each_session([&](IAudioSessionControl2 *control, ISimpleAudioVolume *volume, const char *endpoint) {
        if (written >= max) return;
        a2a_session_info &info = out[written];
        std::memset(&info, 0, sizeof info);
        DWORD pid = 0;
        control->GetProcessId(&pid);
        AudioSessionState state = AudioSessionStateInactive;
        control->GetState(&state);
        BOOL muted = FALSE;
        volume->GetMute(&muted);
        float level = 0;
        volume->GetMasterVolume(&level);
        LPWSTR name = nullptr;
        if (SUCCEEDED(control->GetDisplayName(&name))) {
            utf8(name, info.display_name, sizeof info.display_name);
            CoTaskMemFree(name);
        }
        std::snprintf(info.endpoint, sizeof info.endpoint, "%s", endpoint);
        info.pid = pid;
        info.state = (int32_t)state;
        info.muted = muted ? 1 : 0;
        info.volume = level;
        written++;
    });
    return FAILED(hr) ? (int32_t)hr : written;
}

int32_t a2a_wasapi_set_session_mute(const uint32_t *pids, int32_t count, int32_t mute) {
    int32_t changed = 0;
    HRESULT hr = for_each_session([&](IAudioSessionControl2 *control, ISimpleAudioVolume *volume, const char *) {
        DWORD pid = 0;
        control->GetProcessId(&pid);
        if (!pid) return;
        bool target = false;
        for (int32_t i = 0; i < count && !target; i++) target = pids[i] == pid;
        if (!target) return;
        BOOL current = FALSE;
        volume->GetMute(&current);
        if ((current != FALSE) != (mute != 0) && SUCCEEDED(volume->SetMute(mute ? TRUE : FALSE, nullptr))) changed++;
    });
    return FAILED(hr) ? (int32_t)hr : changed;
}

int32_t a2a_wasapi_set_session_volume(const uint32_t *pids, int32_t count, float level) {
    int32_t changed = 0;
    const float clamped = level < 0 ? 0.0f : (level > 1 ? 1.0f : level);
    HRESULT hr = for_each_session([&](IAudioSessionControl2 *control, ISimpleAudioVolume *volume, const char *) {
        DWORD pid = 0;
        control->GetProcessId(&pid);
        if (!pid) return;
        bool target = false;
        for (int32_t i = 0; i < count && !target; i++) target = pids[i] == pid;
        if (target && SUCCEEDED(volume->SetMasterVolume(clamped, nullptr))) changed++;
    });
    return FAILED(hr) ? (int32_t)hr : changed;
}

int32_t a2a_wasapi_default_render_id(char *out, int32_t capacity) {
    if (!out || capacity <= 0) return 0;
    out[0] = 0;
    ComScope com;
    IMMDeviceEnumerator *enumerator = nullptr;
    IMMDevice *device = nullptr;
    LPWSTR id = nullptr;
    if (SUCCEEDED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL, __uuidof(IMMDeviceEnumerator),
                                   reinterpret_cast<void **>(&enumerator)))
        && SUCCEEDED(enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &device)) && SUCCEEDED(device->GetId(&id))) {
        utf8(id, out, capacity);
        CoTaskMemFree(id);
    }
    release(device);
    release(enumerator);
    return (int32_t)std::strlen(out);
}

}  // extern "C"
