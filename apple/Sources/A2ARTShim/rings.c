// Lock-free SPSC rings and host-time conversion. See A2ARTShim.h for RT rules.

#include "A2ARTShim.h"

#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

#if defined(__APPLE__)
#include <mach/mach_time.h>
#include <pthread.h>

// MARK: Time

static uint32_t g_numer = 0;
static uint32_t g_denom = 0;
static pthread_once_t g_time_once = PTHREAD_ONCE_INIT;

static void init_timebase(void) {
    mach_timebase_info_data_t info;
    mach_timebase_info(&info);
    g_numer = info.numer;
    g_denom = info.denom;
}

void a2a_time_init(void) {
    // Only called on ordinary threads. Publish once so other engine creations
    // and HostClock reads cannot race with the RT thread's cached conversion.
    pthread_once(&g_time_once, init_timebase);
}

uint64_t a2a_host_ticks_to_ns(uint64_t ticks) {
    if (g_numer == g_denom) return ticks;
    // 128-bit intermediate avoids overflow for long uptimes.
    return (uint64_t)(((__uint128_t)ticks * g_numer) / g_denom);
}

uint64_t a2a_now_ns(void) {
    return a2a_host_ticks_to_ns(mach_absolute_time());
}

static void *aligned_alloc64(size_t size) { return aligned_alloc(64, size); }
static void aligned_free64(void *p) { free(p); }

#elif defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <malloc.h>

// MARK: Time (portable runtime, ADR-0007)

// Host ticks are 100 ns units: WASAPI reports QPC positions in 100 ns, and
// a2a_now_ns matches Python's time.perf_counter_ns() (both scale the raw QPC counter).
static LARGE_INTEGER g_qpc_frequency;

void a2a_time_init(void) {
    QueryPerformanceFrequency(&g_qpc_frequency);
}

uint64_t a2a_host_ticks_to_ns(uint64_t ticks) { return ticks * 100; }

uint64_t a2a_now_ns(void) {
    if (!g_qpc_frequency.QuadPart) QueryPerformanceFrequency(&g_qpc_frequency);
    LARGE_INTEGER counter;
    QueryPerformanceCounter(&counter);
    const uint64_t f = (uint64_t)g_qpc_frequency.QuadPart, c = (uint64_t)counter.QuadPart;
    return (c / f) * 1000000000ull + (c % f) * 1000000000ull / f;
}

static void *aligned_alloc64(size_t size) { return _aligned_malloc(size, 64); }
static void aligned_free64(void *p) { _aligned_free(p); }

#else
#include <time.h>

// MARK: Time (portable runtime, ADR-0007): host ticks are CLOCK_MONOTONIC nanoseconds.

void a2a_time_init(void) {}
uint64_t a2a_host_ticks_to_ns(uint64_t ticks) { return ticks; }
uint64_t a2a_now_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ull + (uint64_t)ts.tv_nsec;
}

static void *aligned_alloc64(size_t size) { return aligned_alloc(64, size); }
static void aligned_free64(void *p) { free(p); }
#endif

// Allocate zeroed memory and touch every page now, on the creating thread. calloc maps
// pages lazily, so the first write from an IOProc would otherwise page-fault on the RT path.
void *a2a_prefaulted_calloc(size_t count, size_t size) {
    size_t bytes = count * size;
    if (count && bytes / count != size) return NULL;
    void *p = calloc(count, size);
    if (!p) return NULL;
    volatile char *bytes_p = (volatile char *)p;
    for (size_t i = 0; i < bytes; i += 4096) bytes_p[i] = 0;
    if (bytes) bytes_p[bytes - 1] = 0;
    return p;
}

static uint32_t next_pow2(uint32_t v) {
    if (v > (UINT32_C(1) << 31)) return 0;
    if (v < 2) return 2;
    v--;
    v |= v >> 1; v |= v >> 2; v |= v >> 4; v |= v >> 8; v |= v >> 16;
    return v + 1;
}

// MARK: Float ring

struct a2a_ring {
    _Alignas(64) _Atomic uint64_t write_index;
    _Alignas(64) _Atomic uint64_t read_index;
    _Alignas(64) _Atomic uint64_t overflow;
    uint32_t capacity;
    uint32_t mask;
    uint32_t channels;
    float *data;
};

a2a_ring *a2a_ring_create(uint32_t capacity_frames, uint32_t channels) {
    const uint32_t capacity = next_pow2(capacity_frames);
    if (channels == 0 || !capacity) return NULL;
    a2a_ring *ring = aligned_alloc64(sizeof(a2a_ring));
    if (!ring) return NULL;
    memset(ring, 0, sizeof(*ring));
    ring->capacity = capacity;
    ring->mask = ring->capacity - 1;
    ring->channels = channels;
    ring->data = a2a_prefaulted_calloc((size_t)ring->capacity * channels, sizeof(float));
    if (!ring->data) { aligned_free64(ring); return NULL; }
    atomic_init(&ring->write_index, 0);
    atomic_init(&ring->read_index, 0);
    atomic_init(&ring->overflow, 0);
    return ring;
}

void a2a_ring_destroy(a2a_ring *ring) {
    if (!ring) return;
    free(ring->data);
    aligned_free64(ring);
}

uint32_t a2a_ring_channels(const a2a_ring *ring) { return ring->channels; }
uint32_t a2a_ring_capacity(const a2a_ring *ring) { return ring->capacity; }

uint32_t a2a_ring_write(a2a_ring *ring, const float *src, uint32_t frames) {
    uint64_t w = atomic_load_explicit(&ring->write_index, memory_order_relaxed);
    uint64_t r = atomic_load_explicit(&ring->read_index, memory_order_acquire);
    uint32_t free_frames = ring->capacity - (uint32_t)(w - r);
    uint32_t n = frames < free_frames ? frames : free_frames;
    uint32_t start = (uint32_t)(w & ring->mask);
    uint32_t first = ring->capacity - start;
    if (first > n) first = n;
    const size_t ch = ring->channels;
    memcpy(ring->data + start * ch, src, first * ch * sizeof(float));
    if (n > first) memcpy(ring->data, src + first * ch, (n - first) * ch * sizeof(float));
    atomic_store_explicit(&ring->write_index, w + n, memory_order_release);
    if (n < frames) atomic_fetch_add_explicit(&ring->overflow, frames - n, memory_order_relaxed);
    return n;
}

uint32_t a2a_ring_read(a2a_ring *ring, float *dst, uint32_t frames) {
    uint64_t r = atomic_load_explicit(&ring->read_index, memory_order_relaxed);
    uint64_t w = atomic_load_explicit(&ring->write_index, memory_order_acquire);
    uint32_t avail = (uint32_t)(w - r);
    uint32_t n = frames < avail ? frames : avail;
    uint32_t start = (uint32_t)(r & ring->mask);
    uint32_t first = ring->capacity - start;
    if (first > n) first = n;
    const size_t ch = ring->channels;
    memcpy(dst, ring->data + start * ch, first * ch * sizeof(float));
    if (n > first) memcpy(dst + first * ch, ring->data, (n - first) * ch * sizeof(float));
    atomic_store_explicit(&ring->read_index, r + n, memory_order_release);
    return n;
}

uint32_t a2a_ring_skip(a2a_ring *ring, uint32_t frames) {
    uint64_t r = atomic_load_explicit(&ring->read_index, memory_order_relaxed);
    uint64_t w = atomic_load_explicit(&ring->write_index, memory_order_acquire);
    uint32_t avail = (uint32_t)(w - r);
    uint32_t n = frames < avail ? frames : avail;
    atomic_store_explicit(&ring->read_index, r + n, memory_order_release);
    return n;
}

uint32_t a2a_ring_readable(const a2a_ring *ring) {
    uint64_t w = atomic_load_explicit(&((a2a_ring *)ring)->write_index, memory_order_acquire);
    uint64_t r = atomic_load_explicit(&((a2a_ring *)ring)->read_index, memory_order_acquire);
    return (uint32_t)(w - r);
}

uint32_t a2a_ring_writable(const a2a_ring *ring) {
    return ring->capacity - a2a_ring_readable(ring);
}

uint64_t a2a_ring_total_written(const a2a_ring *ring) {
    return atomic_load_explicit(&((a2a_ring *)ring)->write_index, memory_order_acquire);
}

uint64_t a2a_ring_total_read(const a2a_ring *ring) {
    return atomic_load_explicit(&((a2a_ring *)ring)->read_index, memory_order_acquire);
}

uint64_t a2a_ring_overflow_frames(const a2a_ring *ring) {
    return atomic_load_explicit(&((a2a_ring *)ring)->overflow, memory_order_relaxed);
}

// MARK: Record ring

struct a2a_record_ring {
    _Alignas(64) _Atomic uint64_t write_index;
    _Alignas(64) _Atomic uint64_t read_index;
    _Alignas(64) _Atomic uint64_t dropped;
    uint32_t capacity;
    uint32_t mask;
    a2a_rt_record *data;
};

a2a_record_ring *a2a_record_ring_create(uint32_t capacity) {
    capacity = next_pow2(capacity);
    if (!capacity) return NULL;
    a2a_record_ring *ring = aligned_alloc64(sizeof(a2a_record_ring));
    if (!ring) return NULL;
    memset(ring, 0, sizeof(*ring));
    ring->capacity = capacity;
    ring->mask = ring->capacity - 1;
    ring->data = a2a_prefaulted_calloc(ring->capacity, sizeof(a2a_rt_record));
    if (!ring->data) { aligned_free64(ring); return NULL; }
    atomic_init(&ring->write_index, 0);
    atomic_init(&ring->read_index, 0);
    atomic_init(&ring->dropped, 0);
    return ring;
}

void a2a_record_ring_destroy(a2a_record_ring *ring) {
    if (!ring) return;
    free(ring->data);
    aligned_free64(ring);
}

bool a2a_record_ring_push(a2a_record_ring *ring, const a2a_rt_record *record) {
    uint64_t w = atomic_load_explicit(&ring->write_index, memory_order_relaxed);
    uint64_t r = atomic_load_explicit(&ring->read_index, memory_order_acquire);
    if (w - r >= ring->capacity) {
        atomic_fetch_add_explicit(&ring->dropped, 1, memory_order_relaxed);
        return false;
    }
    ring->data[w & ring->mask] = *record;
    atomic_store_explicit(&ring->write_index, w + 1, memory_order_release);
    return true;
}

uint32_t a2a_record_ring_pop(a2a_record_ring *ring, a2a_rt_record *out, uint32_t max) {
    uint64_t r = atomic_load_explicit(&ring->read_index, memory_order_relaxed);
    uint64_t w = atomic_load_explicit(&ring->write_index, memory_order_acquire);
    uint32_t n = 0;
    while (r + n < w && n < max) {
        out[n] = ring->data[(r + n) & ring->mask];
        n++;
    }
    atomic_store_explicit(&ring->read_index, r + n, memory_order_release);
    return n;
}

uint64_t a2a_record_ring_dropped(const a2a_record_ring *ring) {
    return atomic_load_explicit(&((a2a_record_ring *)ring)->dropped, memory_order_relaxed);
}
