// WASAPI host for the shared A2A engine (ADR-0007). Windows 10 build 20348+ / Windows 11.
//
// One MMCSS "Pro Audio" thread services a process-loopback capture client and the default
// render endpoint, and calls a2a_engine_process for both, so every RT-owned engine field
// stays on that one thread (the ADR-0003 single-callback invariant). Its loop never
// allocates, locks, logs, or calls into Python. Formats are 32-bit float at the engine rate
// and channel count; WASAPI converts to and from the device mix format.

#ifndef A2A_WASAPI_H
#define A2A_WASAPI_H

#include <stdint.h>

#include "A2ARTShim.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct a2a_wasapi a2a_wasapi;

enum {
    A2A_LOOPBACK_INCLUDE_TREE = 0,   // capture only the target process and its children
    A2A_LOOPBACK_EXCLUDE_TREE = 1,   // capture every process except the target tree
};

typedef struct a2a_wasapi_config {
    uint32_t target_pid;          // 0: no capture client (render only)
    uint32_t loopback_mode;       // A2A_LOOPBACK_*
    uint32_t render;              // 1: render the engine output
    uint32_t sample_rate;         // engine rate (48000)
    uint32_t channels;            // engine channels (2)
    uint32_t capture_buffer_ms;   // capture client buffer (default 200)
    uint32_t render_buffer_ms;    // render client buffer (default 20)
    float capture_gain;           // linear gain applied to captured audio (0 means 1)
    const char *render_device_id; // UTF-8 endpoint ID; NULL or "" selects the default console endpoint
} a2a_wasapi_config;

enum {
    A2A_WASAPI_STARTING = 0,
    A2A_WASAPI_RUNNING = 1,
    A2A_WASAPI_STOPPED = 2,
    A2A_WASAPI_FAILED = -1,
};

typedef struct a2a_wasapi_stats {
    int32_t state;                    // A2A_WASAPI_*
    int32_t hresult;                  // last failing HRESULT, 0 when none
    uint64_t capture_packets;
    uint64_t capture_frames;
    uint64_t capture_silent_packets;
    uint64_t capture_discontinuities;
    uint64_t capture_timestamp_errors;
    uint64_t render_wakeups;
    uint64_t render_frames;
    uint64_t render_full_wakeups;     // woke with no room in the buffer
    uint64_t timeouts;                // waits that expired without any event
    uint64_t last_capture_ns;         // a2a_now_ns of the latest capture packet
    uint32_t render_buffer_frames;
    uint32_t render_mix_rate;         // the endpoint's own mix format
    uint32_t render_mix_channels;
    uint32_t reserved;
    double render_latency_ms;         // IAudioClient::GetStreamLatency
    char message[256];                // UTF-8 reason for a failure
    char render_device[256];          // UTF-8 friendly name of the render endpoint
} a2a_wasapi_stats;

/// Starts the engine thread and waits until both clients run (or fail). Returns NULL only
/// when the thread could not be created; otherwise check stats.state. `engine` must outlive
/// the session, and its channel count and rate must match `config`.
a2a_wasapi *a2a_wasapi_start(a2a_engine *engine, const a2a_wasapi_config *config);
void a2a_wasapi_get_stats(a2a_wasapi *session, a2a_wasapi_stats *out);
/// Stops the clients, joins the thread, and frees the session. Safe after a failure.
void a2a_wasapi_stop(a2a_wasapi *session);
/// Control thread. Linear gain for captured audio, applied before the engine sees it. A
/// source whose session volume is ducked to `v` is restored exactly by a gain of 1/v,
/// because the audio engine applies session volume as a float gain before process
/// loopback (measured by `a2a-wintap volume-probe`). Output is clamped to [-1, 1].
void a2a_wasapi_set_capture_gain(a2a_wasapi *session, float gain);

typedef struct a2a_endpoint_info {
    char id[256];
    char name[256];
    int32_t is_default;
    int32_t reserved;
} a2a_endpoint_info;

/// Active render endpoints. Returns the number written, or a negative HRESULT.
int32_t a2a_wasapi_list_endpoints(a2a_endpoint_info *out, int32_t max);

// MARK: Audio sessions (ordinary threads only)

typedef struct a2a_session_info {
    uint32_t pid;
    int32_t state;        // AudioSessionState: 0 inactive, 1 active, 2 expired
    int32_t muted;
    float volume;
    char display_name[128];
    char endpoint[128];
    // IAudioSessionControl2::GetSessionIdentifier, UTF-8. Stable identity for targeted restore.
    char session_id[256];
} a2a_session_info;

/// Render-session inventory over every active render endpoint. Returns the number of
/// sessions written (at most `max`), or a negative HRESULT.
int32_t a2a_wasapi_list_sessions(a2a_session_info *out, int32_t max);

/// Sets the mute state of every render session owned by one of `pids`. Returns the number
/// of sessions changed, or a negative HRESULT. Used for muting the tapped source during
/// translation-only playback and restoring it afterwards.
int32_t a2a_wasapi_set_session_mute(const uint32_t *pids, int32_t count, int32_t mute);

/// Sets the session volume (ISimpleAudioVolume, 0–1) of every render session owned by one
/// of `pids`. Returns the number of sessions set, or a negative HRESULT.
int32_t a2a_wasapi_set_session_volume(const uint32_t *pids, int32_t count, float level);

/// Target exactly one enumerated audio session, never every session that happens to reuse its PID.
int32_t a2a_wasapi_set_session_volume_by_id(const char *session_id, float level);
int32_t a2a_wasapi_set_session_mute_by_id(const char *session_id, int32_t mute);

/// UTF-8 endpoint ID of the current default render device ("" when none). Returns its length.
int32_t a2a_wasapi_default_render_id(char *out, int32_t capacity);

#ifdef __cplusplus
}
#endif

#endif
