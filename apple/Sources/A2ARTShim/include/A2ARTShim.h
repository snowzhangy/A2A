// Real-time shim (ADR-0001): everything that runs on a Core Audio IOProc thread.
//
// Rules (AGENTS.md): no allocation, locks, logging, Objective-C/Swift runtime calls, or
// blocking on the callback path. All memory is allocated by *_create on a normal thread.
// Each ring is single-producer/single-consumer; indices are C11 atomics.

#ifndef A2A_RT_SHIM_H
#define A2A_RT_SHIM_H

#include <stdbool.h>
#include <stdint.h>
#include <CoreAudio/CoreAudio.h>

#ifdef __cplusplus
extern "C" {
#endif

#pragma clang assume_nonnull begin

// MARK: Time

/// Call once on a normal thread before any RT use (caches mach_timebase_info).
void a2a_time_init(void);
uint64_t a2a_host_ticks_to_ns(uint64_t ticks);
uint64_t a2a_now_ns(void);

/// Zeroed allocation with every page touched up front (no first-write page faults on RT threads).
void *_Nullable a2a_prefaulted_calloc(size_t count, size_t size);

// MARK: Float SPSC ring (interleaved frames)

typedef struct a2a_ring a2a_ring;

/// Capacity is rounded up to a power of two frames; unrepresentable capacities return NULL.
a2a_ring *_Nullable a2a_ring_create(uint32_t capacity_frames, uint32_t channels);
void a2a_ring_destroy(a2a_ring *_Nullable ring);
uint32_t a2a_ring_channels(const a2a_ring *ring);
uint32_t a2a_ring_capacity(const a2a_ring *ring);
/// Producer side. Writes up to `frames`; frames that do not fit are counted as overflow.
uint32_t a2a_ring_write(a2a_ring *ring, const float *interleaved, uint32_t frames);
/// Consumer side. Reads up to `frames`.
uint32_t a2a_ring_read(a2a_ring *ring, float *interleaved, uint32_t frames);
/// Consumer side. Discards up to `frames` without copying.
uint32_t a2a_ring_skip(a2a_ring *ring, uint32_t frames);
uint32_t a2a_ring_readable(const a2a_ring *ring);
uint32_t a2a_ring_writable(const a2a_ring *ring);
uint64_t a2a_ring_total_written(const a2a_ring *ring);
uint64_t a2a_ring_total_read(const a2a_ring *ring);
uint64_t a2a_ring_overflow_frames(const a2a_ring *ring);

// MARK: Timing record ring

typedef enum a2a_record_kind {
    A2A_REC_CAPTURE = 1,         // input block: host time of first frame, capture frame index
    A2A_REC_OUTPUT = 2,          // output block: presentation host time, output frame index
    A2A_REC_CAPTURE_OVERFLOW = 3,
    A2A_REC_TRANSLATED_STARVED = 4,
    A2A_REC_TIMESTAMP_INVALID = 5,
    A2A_REC_DISCONTINUITY = 6,
} a2a_record_kind;

typedef struct a2a_rt_record {
    uint32_t kind;
    uint32_t flags;          // AudioTimeStamp.mFlags
    uint64_t host_ns;        // 0 when the timestamp had no valid host time
    int64_t sample_index;    // frames since engine start on this branch
    uint32_t frames;
    uint32_t aux;
    int64_t aux64;           // OUTPUT: translated frames consumed before this block
    double rate_scalar;      // AudioTimeStamp.mRateScalar when valid, else 0
} a2a_rt_record;

typedef struct a2a_record_ring a2a_record_ring;

a2a_record_ring *_Nullable a2a_record_ring_create(uint32_t capacity);
void a2a_record_ring_destroy(a2a_record_ring *_Nullable ring);
/// Returns false (and counts a drop) when full.
bool a2a_record_ring_push(a2a_record_ring *ring, const a2a_rt_record *record);
uint32_t a2a_record_ring_pop(a2a_record_ring *ring, a2a_rt_record *out, uint32_t max);
uint64_t a2a_record_ring_dropped(const a2a_record_ring *ring);

// MARK: Engine: capture branch + two-bus output mixer

typedef struct a2a_engine_config {
    uint32_t channels;               // canonical engine channel count (2)
    uint32_t capture_ring_frames;    // capture branch → writer / inference feeder
    uint32_t max_delay_frames;       // original-bus delay line (0–3 s)
    uint32_t translated_ring_frames; // translated mono PCM at device rate
    uint32_t max_block_frames;       // largest IOProc block accepted
    uint32_t record_capacity;
    uint32_t rendered_ring_frames;   // post-gain monitor ring; 0 = not allocated
    uint32_t fade_frames;            // translated-bus edge smoothing (about 5 ms); 0 = hard edges
} a2a_engine_config;

typedef struct a2a_engine a2a_engine;

a2a_engine *_Nullable a2a_engine_create(const a2a_engine_config *config);
void a2a_engine_destroy(a2a_engine *_Nullable engine);

a2a_ring *a2a_engine_capture_ring(a2a_engine *engine);
/// Mono: the mixer feeds the same translated sample to every output channel.
a2a_ring *a2a_engine_translated_ring(a2a_engine *engine);
/// Before callbacks start only. Allocates a bounded, prefaulted mono background bus.
bool a2a_engine_enable_background(a2a_engine *engine, uint32_t capacity_frames);
a2a_ring *_Nullable a2a_engine_background_ring(a2a_engine *engine);
/// Control thread. Independent linear background-gain ramp; original audio stays separate.
void a2a_engine_set_background_gain(a2a_engine *engine, float gain, uint32_t ramp_frames);
/// Post-gain render PCM at the device rate, or NULL without `rendered_ring_frames`.
/// One non-RT consumer; engine owns the ring.
a2a_ring *_Nullable a2a_engine_rendered_ring(a2a_engine *engine);
a2a_record_ring *a2a_engine_records(a2a_engine *engine);

/// Before start. Index of the first tapped channel in the device input (aggregate
/// sub-device inputs precede tap channels).
void a2a_engine_set_input_channel_offset(a2a_engine *engine, uint32_t offset);
/// Control thread. Render captured audio to the output (re-render path, A2).
void a2a_engine_set_render_output(a2a_engine *engine, bool enabled);
/// Explicit diagnostic capture of the post-gain output. Disabled by default; no file I/O on RT.
void a2a_engine_set_capture_rendered(a2a_engine *engine, bool enabled);
/// Control thread. Delay of the original bus in frames; clamps to max_delay_frames.
void a2a_engine_set_delay_frames(a2a_engine *engine, uint32_t frames);
/// Control thread. Linear gain ramp of both buses over `ramp_frames`.
void a2a_engine_set_gains(a2a_engine *engine, float original, float translated, uint32_t ramp_frames);
/// Translated-ring producer thread. Discard translated audio queued at the time of the call
/// (fallback); audio written afterwards plays. The discarded head fades out over
/// `fade_frames` instead of cutting off mid-waveform.
void a2a_engine_flush_translated(a2a_engine *engine);

/// Current gains as seen by the render thread (for tests and telemetry).
float a2a_engine_current_original_gain(const a2a_engine *engine);
float a2a_engine_current_translated_gain(const a2a_engine *engine);
float a2a_engine_current_background_gain(const a2a_engine *engine);
uint64_t a2a_engine_callbacks(const a2a_engine *engine);
uint64_t a2a_engine_captured_frames(const a2a_engine *engine);
uint64_t a2a_engine_output_frames(const a2a_engine *engine);
uint64_t a2a_engine_translated_consumed(const a2a_engine *engine);
uint64_t a2a_engine_translated_starved_frames(const a2a_engine *engine);
uint64_t a2a_engine_rejected_blocks(const a2a_engine *engine);

/// RT. Process one block of interleaved input (engine channels) and produce interleaved output.
/// `in` may be NULL (no input this cycle); `out` may be NULL (capture only).
/// When the translated bus runs dry mid-signal it ramps to zero, and audio that resumes
/// after a dry spell or flush fades in, both over `fade_frames`.
void a2a_engine_process(a2a_engine *engine,
                        const float *_Nullable in, uint32_t in_frames, const AudioTimeStamp *_Nullable in_time,
                        float *_Nullable out, uint32_t out_frames, const AudioTimeStamp *_Nullable out_time);

/// RT. AudioDeviceIOProc entry point; `client` is the engine. Converts buffer lists
/// (Float32, interleaved or not) to engine channels and calls a2a_engine_process.
OSStatus a2a_engine_ioproc(AudioObjectID device, const AudioTimeStamp *_Nonnull now,
                           const AudioBufferList *_Nonnull input, const AudioTimeStamp *_Nonnull input_time,
                           AudioBufferList *_Nonnull output, const AudioTimeStamp *_Nonnull output_time,
                           void *_Nullable client);

#pragma clang assume_nonnull end

#ifdef __cplusplus
}
#endif

#endif
