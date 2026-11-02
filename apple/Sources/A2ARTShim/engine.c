// Capture branch and original/translated mixer with an optional separated background bus.
// Runs on the Core Audio IOProc thread.
//
// Control → RT commands travel through single 64-bit atomics (gains, delay) or
// request/acknowledge counters (flush), so the render thread never waits.
//
// The translated bus is mono and never steps between signal and silence: audio that
// starts after a dry spell fades in, a bus that runs dry ramps its last sample to zero,
// and a flush fades out the head of the discarded audio (all over `fade_frames`).

#include "A2ARTShim.h"

#include <math.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>

#define GAIN_SCALE 16384.0f   // 16-bit fixed point, range [0, 4)

struct a2a_engine {
    a2a_engine_config cfg;
    a2a_ring *capture;
    a2a_ring *translated;
    a2a_ring *background;
    float *scratch_background;
    _Atomic uint64_t background_cmd;
    uint64_t background_seq;
    float gain_b, target_b, step_b;
    uint32_t ramp_b;
    bool background_active;
    uint32_t background_fade, background_hold;
    float background_last, background_tail;
    a2a_ring *rendered;
    a2a_record_ring *records;

    // Control → RT.
    _Atomic uint64_t gain_cmd;        // seq:8 | ramp:24 | original:16 | translated:16
    _Atomic uint32_t delay_target;
    _Atomic uint64_t flush_to;        // translated-ring position: frames before it are discarded
    _Atomic uint32_t flush_requests;
    _Atomic bool render_output;
    _Atomic bool capture_rendered;
    _Atomic uint32_t input_offset;

    // RT-owned state.
    uint64_t gain_seq_applied;
    uint32_t flush_done;
    uint32_t delay_applied;
    bool delay_valid;
    float gain_o, gain_t, step_o, step_t, target_o, target_t;
    uint32_t ramp_left;
    float *delay;
    uint32_t delay_mask;
    int64_t delay_w;
    int64_t delay_r;
    float *scratch_in;
    float *scratch_out;
    float *scratch_trans;

    // RT-owned translated-bus edge smoothing (fade_frames > 0).
    float *fade_curve;                // rising raised-cosine envelope, values in (0, 1)
    float *fade_tail;                 // head of flushed audio, played out falling to zero
    uint32_t fade_tail_len;
    uint32_t fade_tail_pos;
    uint32_t fade_in_left;            // resumed audio still fading in
    uint32_t hold_left;               // dry-bus ramp still playing
    float hold_value;                 // last delivered sample, ramped to zero when the bus runs dry
    float trans_last;
    bool trans_active;                // the previous block ended on delivered audio

    // RT → observers.
    _Atomic uint32_t gain_o_bits;
    _Atomic uint32_t gain_t_bits;
    _Atomic uint32_t gain_b_bits;
    _Atomic uint64_t callbacks;
    _Atomic uint64_t captured;
    _Atomic uint64_t output;
    _Atomic uint64_t trans_consumed;
    _Atomic uint64_t trans_starved;
    _Atomic uint64_t rejected;
};

static uint32_t pow2_at_least(uint32_t v) {
    uint32_t p = 2;
    while (p < v) p <<= 1;
    return p;
}

static uint32_t float_bits(float f) { uint32_t u; memcpy(&u, &f, 4); return u; }
static float bits_float(uint32_t u) { float f; memcpy(&f, &u, 4); return f; }

a2a_engine *a2a_engine_create(const a2a_engine_config *config) {
    if (!config || config->channels == 0 || config->max_block_frames == 0) return NULL;
    const uint64_t delay_required = (uint64_t)config->max_delay_frames + 4 * (uint64_t)config->max_block_frames;
    if (delay_required > (UINT32_C(1) << 31)) return NULL;
    a2a_engine *e = calloc(1, sizeof(a2a_engine));
    if (!e) return NULL;
    e->cfg = *config;
    const uint32_t ch = config->channels;
    const uint32_t fade = config->fade_frames;
    e->capture = a2a_ring_create(config->capture_ring_frames, ch);
    e->translated = a2a_ring_create(config->translated_ring_frames, 1);
    if (config->rendered_ring_frames) e->rendered = a2a_ring_create(config->rendered_ring_frames, ch);
    e->records = a2a_record_ring_create(config->record_capacity ? config->record_capacity : 4096);
    uint32_t delay_frames = pow2_at_least((uint32_t)delay_required);
    e->delay = a2a_prefaulted_calloc((size_t)delay_frames * ch, sizeof(float));
    e->delay_mask = delay_frames - 1;
    e->scratch_in = a2a_prefaulted_calloc((size_t)config->max_block_frames * ch, sizeof(float));
    e->scratch_out = a2a_prefaulted_calloc((size_t)config->max_block_frames * ch, sizeof(float));
    e->scratch_trans = a2a_prefaulted_calloc(config->max_block_frames, sizeof(float));
    if (fade) {
        e->fade_curve = a2a_prefaulted_calloc(fade, sizeof(float));
        e->fade_tail = a2a_prefaulted_calloc(fade, sizeof(float));
    }
    if (!e->capture || !e->translated || (config->rendered_ring_frames && !e->rendered) || !e->records || !e->delay
        || !e->scratch_in || !e->scratch_out || !e->scratch_trans || (fade && (!e->fade_curve || !e->fade_tail))) {
        a2a_engine_destroy(e);
        return NULL;
    }
    // sin² rises smoothly from just above 0 to just below 1; reversed, it falls the same way.
    for (uint32_t k = 0; k < fade; k++) {
        float s = sinf(0.5f * (float)M_PI * (float)(k + 1) / (float)(fade + 1));
        e->fade_curve[k] = s * s;
    }
    e->fade_in_left = fade;           // the first translated audio fades in too
    e->gain_o = e->target_o = 1.0f;
    e->gain_t = e->target_t = 0.0f;
    atomic_init(&e->gain_cmd, 0);
    atomic_init(&e->background_cmd, 0);
    atomic_init(&e->delay_target, 0);
    atomic_init(&e->flush_to, 0);
    atomic_init(&e->flush_requests, 0);
    atomic_init(&e->render_output, false);
    atomic_init(&e->capture_rendered, false);
    atomic_init(&e->gain_o_bits, float_bits(1.0f));
    atomic_init(&e->gain_t_bits, float_bits(0.0f));
    atomic_init(&e->gain_b_bits, float_bits(0.0f));
    a2a_time_init();
    return e;
}

void a2a_engine_destroy(a2a_engine *e) {
    if (!e) return;
    a2a_ring_destroy(e->capture);
    a2a_ring_destroy(e->translated);
    a2a_ring_destroy(e->background);
    free(e->scratch_background);
    a2a_ring_destroy(e->rendered);
    a2a_record_ring_destroy(e->records);
    free(e->delay);
    free(e->scratch_in);
    free(e->scratch_out);
    free(e->scratch_trans);
    free(e->fade_curve);
    free(e->fade_tail);
    free(e);
}

a2a_ring *a2a_engine_capture_ring(a2a_engine *e) { return e->capture; }
a2a_ring *a2a_engine_translated_ring(a2a_engine *e) { return e->translated; }
a2a_ring *a2a_engine_background_ring(a2a_engine *e) { return e->background; }
bool a2a_engine_enable_background(a2a_engine *e, uint32_t capacity) {
    if (!e || !capacity || e->background || atomic_load_explicit(&e->callbacks, memory_order_relaxed)) return false;
    a2a_ring *ring = a2a_ring_create(capacity, 1);
    float *scratch = a2a_prefaulted_calloc(e->cfg.max_block_frames, sizeof(float));
    if (!ring || !scratch) { a2a_ring_destroy(ring); free(scratch); return false; }
    e->background = ring;
    e->scratch_background = scratch;
    e->background_fade = e->cfg.fade_frames;
    return true;
}
a2a_ring *a2a_engine_rendered_ring(a2a_engine *e) { return e->rendered; }
a2a_record_ring *a2a_engine_records(a2a_engine *e) { return e->records; }

void a2a_engine_set_input_channel_offset(a2a_engine *e, uint32_t offset) {
    atomic_store_explicit(&e->input_offset, offset, memory_order_release);
}

void a2a_engine_set_render_output(a2a_engine *e, bool enabled) {
    atomic_store_explicit(&e->render_output, enabled, memory_order_release);
}

void a2a_engine_set_capture_rendered(a2a_engine *e, bool enabled) {
    atomic_store_explicit(&e->capture_rendered, enabled, memory_order_release);
}

void a2a_engine_set_delay_frames(a2a_engine *e, uint32_t frames) {
    if (frames > e->cfg.max_delay_frames) frames = e->cfg.max_delay_frames;
    atomic_store_explicit(&e->delay_target, frames, memory_order_release);
}

static uint16_t gain_fixed(float g) {
    if (!(g > 0.0f)) return 0;
    float v = g * GAIN_SCALE;
    if (v > 65535.0f) v = 65535.0f;
    return (uint16_t)lrintf(v);
}

void a2a_engine_set_gains(a2a_engine *e, float original, float translated, uint32_t ramp_frames) {
    uint64_t prev = atomic_load_explicit(&e->gain_cmd, memory_order_relaxed);
    uint64_t seq = ((prev >> 56) + 1) & 0xFF;
    if (seq == 0) seq = 1;
    if (ramp_frames > 0xFFFFFF) ramp_frames = 0xFFFFFF;
    uint64_t cmd = (seq << 56) | ((uint64_t)ramp_frames << 32)
        | ((uint64_t)gain_fixed(original) << 16) | (uint64_t)gain_fixed(translated);
    atomic_store_explicit(&e->gain_cmd, cmd, memory_order_release);
}

void a2a_engine_set_background_gain(a2a_engine *e, float gain, uint32_t ramp_frames) {
    uint64_t previous = atomic_load_explicit(&e->background_cmd, memory_order_relaxed);
    uint64_t seq = ((previous >> 56) + 1) & 0xFF;
    if (!seq) seq = 1;
    if (ramp_frames > 0xFFFFFF) ramp_frames = 0xFFFFFF;
    atomic_store_explicit(&e->background_cmd, (seq << 56) | ((uint64_t)ramp_frames << 32)
        | gain_fixed(gain), memory_order_release);
}

void a2a_engine_flush_translated(a2a_engine *e) {
    // Only what is queued now is discarded. The position only grows, so a render thread that
    // sees a newer position than the request it acknowledges still discards nothing newer.
    atomic_store_explicit(&e->flush_to, a2a_ring_total_written(e->translated), memory_order_release);
    atomic_fetch_add_explicit(&e->flush_requests, 1, memory_order_release);
}

float a2a_engine_current_original_gain(const a2a_engine *e) {
    return bits_float(atomic_load_explicit(&((a2a_engine *)e)->gain_o_bits, memory_order_relaxed));
}
float a2a_engine_current_translated_gain(const a2a_engine *e) {
    return bits_float(atomic_load_explicit(&((a2a_engine *)e)->gain_t_bits, memory_order_relaxed));
}
float a2a_engine_current_background_gain(const a2a_engine *e) {
    return bits_float(atomic_load_explicit(&((a2a_engine *)e)->gain_b_bits, memory_order_relaxed));
}
#define LOAD64(field) atomic_load_explicit(&((a2a_engine *)e)->field, memory_order_relaxed)
uint64_t a2a_engine_callbacks(const a2a_engine *e) { return LOAD64(callbacks); }
uint64_t a2a_engine_captured_frames(const a2a_engine *e) { return LOAD64(captured); }
uint64_t a2a_engine_output_frames(const a2a_engine *e) { return LOAD64(output); }
uint64_t a2a_engine_translated_consumed(const a2a_engine *e) { return LOAD64(trans_consumed); }
uint64_t a2a_engine_translated_starved_frames(const a2a_engine *e) { return LOAD64(trans_starved); }
uint64_t a2a_engine_rejected_blocks(const a2a_engine *e) { return LOAD64(rejected); }

// MARK: RT

/// Discards `n` queued translated frames. Audio cutting off mid-signal keeps its first
/// `fade_frames` as a fade-out tail; a bus that is not currently sounding just skips.
static void discard_translated(a2a_engine *e, uint32_t n) {
    const uint32_t fade = e->cfg.fade_frames;
    uint32_t kept = 0;
    if (fade && e->trans_active && e->fade_tail_pos >= e->fade_tail_len) {
        kept = a2a_ring_read(e->translated, e->fade_tail, n < fade ? n : fade);
        // Continue a fade-in still in progress so the tail starts at the level just played.
        for (uint32_t i = 0; i < kept && e->fade_in_left; i++, e->fade_in_left--)
            e->fade_tail[i] *= e->fade_curve[fade - e->fade_in_left];
        e->fade_tail_len = kept;
        e->fade_tail_pos = 0;
        e->trans_active = false;      // the tail, not the dry-bus ramp, ends this signal
        e->fade_in_left = fade;
    }
    a2a_ring_skip(e->translated, n - kept);
    atomic_fetch_add_explicit(&e->trans_consumed, n, memory_order_relaxed);
}

/// Click-free translated-bus edges for the block in scratch_trans (`got` delivered frames).
static void shape_translated(a2a_engine *e, uint32_t got, uint32_t frames) {
    const uint32_t fade = e->cfg.fade_frames;
    if (!fade || !frames) return;
    float *t = e->scratch_trans;
    const float *curve = e->fade_curve;
    for (uint32_t i = 0; i < frames; i++) {
        if (i < got) {
            if (e->fade_in_left) t[i] *= curve[fade - e->fade_in_left--];
            e->trans_active = true;
        } else if (e->trans_active) {
            // Include a previous dry-bus tail in the restart level. A very short
            // resumption can run dry again before that tail has finished.
            e->hold_value = e->trans_last;
            e->hold_left = fade;
            e->trans_active = false;
            e->fade_in_left = fade;
        }
        // Keep the old tail during resumed PCM too: dropping it at the first
        // resumed sample creates a click when an I/O block is shorter than the fade.
        if (e->hold_left) t[i] += e->hold_value * curve[--e->hold_left];
        e->trans_last = t[i];
    }
    const uint32_t len = e->fade_tail_len;
    for (uint32_t i = 0; i < frames && e->fade_tail_pos < len; i++, e->fade_tail_pos++)
        t[i] += e->fade_tail[e->fade_tail_pos] * curve[(len - 1 - e->fade_tail_pos) * fade / len];
}

static void apply_commands(a2a_engine *e) {
    uint64_t bg = atomic_load_explicit(&e->background_cmd, memory_order_acquire);
    if ((bg >> 56) && (bg >> 56) != e->background_seq) {
        e->background_seq = bg >> 56;
        e->target_b = (float)(bg & 0xFFFF) / GAIN_SCALE;
        e->ramp_b = (uint32_t)((bg >> 32) & 0xFFFFFF);
        if (e->ramp_b) e->step_b = (e->target_b - e->gain_b) / (float)e->ramp_b;
        else e->gain_b = e->target_b;
    }
    uint64_t cmd = atomic_load_explicit(&e->gain_cmd, memory_order_acquire);
    uint64_t seq = cmd >> 56;
    if (seq != 0 && seq != e->gain_seq_applied) {
        e->gain_seq_applied = seq;
        uint32_t ramp = (uint32_t)((cmd >> 32) & 0xFFFFFF);
        e->target_o = (float)((cmd >> 16) & 0xFFFF) / GAIN_SCALE;
        e->target_t = (float)(cmd & 0xFFFF) / GAIN_SCALE;
        if (ramp == 0) {
            e->gain_o = e->target_o;
            e->gain_t = e->target_t;
            e->ramp_left = 0;
        } else {
            e->step_o = (e->target_o - e->gain_o) / (float)ramp;
            e->step_t = (e->target_t - e->gain_t) / (float)ramp;
            e->ramp_left = ramp;
        }
    }
    uint32_t flushes = atomic_load_explicit(&e->flush_requests, memory_order_acquire);
    if (flushes != e->flush_done) {
        e->flush_done = flushes;
        uint64_t to = atomic_load_explicit(&e->flush_to, memory_order_acquire);
        uint64_t read = a2a_ring_total_read(e->translated);
        if (to > read) {
            uint32_t n = (uint32_t)(to - read);
            uint32_t queued = a2a_ring_readable(e->translated);
            discard_translated(e, n < queued ? n : queued);
        }
    }
    uint32_t delay = atomic_load_explicit(&e->delay_target, memory_order_acquire);
    if (!e->delay_valid || delay != e->delay_applied) {
        e->delay_applied = delay;
        e->delay_valid = false;   // reposition the read cursor on the next output block
    }
}

static void push_record(a2a_engine *e, uint32_t kind, const AudioTimeStamp *ts, int64_t index,
                        uint32_t frames, int64_t aux64) {
    a2a_rt_record rec = {0};
    rec.kind = kind;
    rec.sample_index = index;
    rec.frames = frames;
    rec.aux64 = aux64;
    if (ts) {
        rec.flags = ts->mFlags;
        if (ts->mFlags & kAudioTimeStampHostTimeValid) rec.host_ns = a2a_host_ticks_to_ns(ts->mHostTime);
        if (ts->mFlags & kAudioTimeStampRateScalarValid) rec.rate_scalar = ts->mRateScalar;
    }
    a2a_record_ring_push(e->records, &rec);
    if (ts && !(ts->mFlags & kAudioTimeStampHostTimeValid)) {
        a2a_rt_record bad = {0};
        bad.kind = A2A_REC_TIMESTAMP_INVALID;
        bad.flags = ts->mFlags;
        bad.sample_index = index;
        bad.aux = kind;
        a2a_record_ring_push(e->records, &bad);
    }
}

void a2a_engine_process(a2a_engine *e,
                        const float *_Nullable in, uint32_t in_frames, const AudioTimeStamp *_Nullable in_time,
                        float *_Nullable out, uint32_t out_frames, const AudioTimeStamp *_Nullable out_time) {
    const uint32_t ch = e->cfg.channels;
    atomic_fetch_add_explicit(&e->callbacks, 1, memory_order_relaxed);
    apply_commands(e);

    if ((in && in_frames > e->cfg.max_block_frames) || (out && out_frames > e->cfg.max_block_frames)) {
        atomic_fetch_add_explicit(&e->rejected, 1, memory_order_relaxed);
        if (out) {
            memset(out, 0, (size_t)out_frames * ch * sizeof(float));
            if (e->rendered && atomic_load_explicit(&e->capture_rendered, memory_order_acquire))
                a2a_ring_write(e->rendered, out, out_frames);
        }
        return;
    }

    if (in && in_frames) {
        int64_t index = (int64_t)atomic_load_explicit(&e->captured, memory_order_relaxed);
        push_record(e, A2A_REC_CAPTURE, in_time, index, in_frames, 0);
        uint32_t written = a2a_ring_write(e->capture, in, in_frames);
        if (written < in_frames) {
            a2a_rt_record rec = {0};
            rec.kind = A2A_REC_CAPTURE_OVERFLOW;
            rec.sample_index = index + written;
            rec.frames = in_frames - written;
            a2a_record_ring_push(e->records, &rec);
        }
        const uint32_t size = e->delay_mask + 1;
        const uint32_t start = (uint32_t)(e->delay_w & e->delay_mask);
        const uint32_t first = in_frames < size - start ? in_frames : size - start;
        memcpy(e->delay + (size_t)start * ch, in, (size_t)first * ch * sizeof(float));
        memcpy(e->delay, in + (size_t)first * ch, (size_t)(in_frames - first) * ch * sizeof(float));
        e->delay_w += in_frames;
        atomic_fetch_add_explicit(&e->captured, in_frames, memory_order_relaxed);
    }

    if (!out || out_frames == 0) return;

    if (!e->delay_valid) {
        e->delay_r = e->delay_w - (int64_t)out_frames - (int64_t)e->delay_applied;
        e->delay_valid = true;
    }
    // Resynchronize if the cursor fell outside the delay line (clock slip or a long stall).
    int64_t lag = e->delay_w - e->delay_r;
    if (lag > (int64_t)(e->delay_mask + 1) - (int64_t)e->cfg.max_block_frames) {
        e->delay_r = e->delay_w - (int64_t)out_frames - (int64_t)e->delay_applied;
    }

    const bool render = atomic_load_explicit(&e->render_output, memory_order_acquire);
    uint64_t consumed_before = atomic_load_explicit(&e->trans_consumed, memory_order_relaxed);
    uint32_t got = a2a_ring_read(e->translated, e->scratch_trans, out_frames);
    if (got < out_frames) memset(e->scratch_trans + got, 0, (size_t)(out_frames - got) * sizeof(float));
    atomic_fetch_add_explicit(&e->trans_consumed, got, memory_order_relaxed);
    if (got < out_frames && (e->gain_t > 0.0f || e->ramp_left) && consumed_before + got > 0) {
        uint32_t missing = out_frames - got;
        atomic_fetch_add_explicit(&e->trans_starved, missing, memory_order_relaxed);
        a2a_rt_record rec = {0};
        rec.kind = A2A_REC_TRANSLATED_STARVED;
        rec.sample_index = (int64_t)atomic_load_explicit(&e->output, memory_order_relaxed);
        rec.frames = missing;
        a2a_record_ring_push(e->records, &rec);
    }
    shape_translated(e, got, out_frames);
    uint32_t background_got = 0;
    if (e->background) {
        background_got = a2a_ring_read(e->background, e->scratch_background, out_frames);
        if (background_got < out_frames)
            memset(e->scratch_background + background_got, 0, (out_frames - background_got) * sizeof(float));
    }

    for (uint32_t i = 0; i < out_frames; i++) {
        int64_t idx = e->delay_r + i;
        const bool have = idx >= 0 && idx < e->delay_w;
        const float *orig = e->delay + ((size_t)(idx & e->delay_mask)) * ch;
        const float trans = e->scratch_trans[i];
        float bed = e->background ? e->scratch_background[i] : 0;
        if (e->background && e->cfg.fade_frames) {
            if (i < background_got) {
                if (!e->background_active) e->background_fade = e->cfg.fade_frames;
                if (e->background_fade) bed *= e->fade_curve[e->cfg.fade_frames - e->background_fade--];
                e->background_active = true;
            } else if (e->background_active) {
                e->background_tail = e->background_last;
                e->background_hold = e->cfg.fade_frames;
                e->background_active = false;
            }
            if (e->background_hold) bed += e->background_tail * e->fade_curve[--e->background_hold];
            e->background_last = bed;
        }
        if (e->ramp_b && --e->ramp_b == 0) e->gain_b = e->target_b;
        else if (e->ramp_b) e->gain_b += e->step_b;
        float *dst = out + (size_t)i * ch;
        if (e->ramp_left) {
            e->gain_o += e->step_o;
            e->gain_t += e->step_t;
            if (--e->ramp_left == 0) {
                // Land exactly on the target to avoid accumulated rounding error.
                e->gain_o = e->target_o;
                e->gain_t = e->target_t;
            }
        }
        for (uint32_t c = 0; c < ch; c++) {
            float o = (render && have) ? orig[c] : 0.0f;
            float mixed = o * e->gain_o + trans * e->gain_t + bed * e->gain_b;
            if (e->background && e->gain_b > 0) mixed = fmaxf(-1, fminf(1, mixed));
            dst[c] = render ? mixed : 0.0f;
        }
    }
    int64_t available = e->delay_w - e->delay_r;
    // Explicit research monitoring only: copy the actual post-gain render buffer,
    // including silence. The preallocated SPSC ring is drained on an ordinary thread.
    if (e->rendered && atomic_load_explicit(&e->capture_rendered, memory_order_acquire))
        a2a_ring_write(e->rendered, out, out_frames);
    e->delay_r += available < (int64_t)out_frames ? (available > 0 ? available : 0) : (int64_t)out_frames;

    int64_t out_index = (int64_t)atomic_load_explicit(&e->output, memory_order_relaxed);
    push_record(e, A2A_REC_OUTPUT, out_time, out_index, out_frames, (int64_t)consumed_before);
    atomic_fetch_add_explicit(&e->output, out_frames, memory_order_relaxed);
    atomic_store_explicit(&e->gain_o_bits, float_bits(e->gain_o), memory_order_relaxed);
    atomic_store_explicit(&e->gain_t_bits, float_bits(e->gain_t), memory_order_relaxed);
    atomic_store_explicit(&e->gain_b_bits, float_bits(e->gain_b), memory_order_relaxed);
}

// MARK: IOProc adapter

// Frame count of a buffer list: the largest buffer (empty sub-streams report 0 bytes).
static uint32_t abl_frames(const AudioBufferList *abl) {
    if (!abl) return 0;
    uint32_t frames = 0;
    for (uint32_t i = 0; i < abl->mNumberBuffers; i++) {
        const AudioBuffer *b = &abl->mBuffers[i];
        uint32_t chans = b->mNumberChannels ? b->mNumberChannels : 1;
        uint32_t f = b->mDataByteSize / (chans * (uint32_t)sizeof(float));
        if (f > frames) frames = f;
    }
    return frames;
}

// Gather `ch` channels starting at channel `offset` of a buffer list into interleaved `dst`.
static void gather(const AudioBufferList *abl, float *dst, uint32_t frames, uint32_t ch, uint32_t offset) {
    uint32_t written = 0;
    uint32_t seen = 0;
    for (uint32_t b = 0; b < abl->mNumberBuffers && written < ch; b++) {
        const AudioBuffer *buf = &abl->mBuffers[b];
        const uint32_t n = buf->mNumberChannels ? buf->mNumberChannels : 1;
        const float *src = (const float *)buf->mData;
        uint32_t cap = buf->mDataByteSize / (n * (uint32_t)sizeof(float));
        if (cap < frames) { seen += n; continue; }
        if (!src) { seen += n; continue; }
        for (uint32_t c = 0; c < n && written < ch; c++, seen++) {
            if (seen < offset) continue;
            written++;
            for (uint32_t i = 0; i < frames; i++) dst[(size_t)i * ch + written - 1] = src[(size_t)i * n + c];
        }
    }
    if (written == 0) {
        memset(dst, 0, (size_t)frames * ch * sizeof(float));
        return;
    }
    // Mono (or fewer channels) source: replicate the last channel.
    for (uint32_t c = written; c < ch; c++) {
        for (uint32_t i = 0; i < frames; i++) dst[(size_t)i * ch + c] = dst[(size_t)i * ch + written - 1];
    }
}

static void scatter(const float *src, AudioBufferList *abl, uint32_t frames, uint32_t ch) {
    uint32_t used = 0;
    for (uint32_t b = 0; b < abl->mNumberBuffers; b++) {
        AudioBuffer *buf = &abl->mBuffers[b];
        const uint32_t n = buf->mNumberChannels ? buf->mNumberChannels : 1;
        float *dst = (float *)buf->mData;
        if (!dst) continue;
        uint32_t cap = buf->mDataByteSize / (n * (uint32_t)sizeof(float));
        uint32_t f = frames < cap ? frames : cap;
        for (uint32_t c = 0; c < n; c++, used++) {
            for (uint32_t i = 0; i < f; i++) {
                float v;
                if (ch == 1) v = src[i];
                else if (used < ch) v = src[(size_t)i * ch + used];
                else v = 0.0f;
                dst[(size_t)i * n + c] = v;
            }
        }
    }
}

OSStatus a2a_engine_ioproc(AudioObjectID device, const AudioTimeStamp *_Nonnull now,
                           const AudioBufferList *_Nonnull input, const AudioTimeStamp *_Nonnull input_time,
                           AudioBufferList *_Nonnull output, const AudioTimeStamp *_Nonnull output_time,
                           void *_Nullable client) {
    (void)device;
    (void)now;
    a2a_engine *e = (a2a_engine *)client;
    const uint32_t ch = e->cfg.channels;
    uint32_t in_frames = abl_frames(input);
    uint32_t out_frames = abl_frames(output);
    if (in_frames > e->cfg.max_block_frames || out_frames > e->cfg.max_block_frames) {
        // A device reconfiguration can exceed the preallocated scratch buffers.
        // Reject the whole cycle: clamping would leave the output tail untouched
        // and silently drop captured frames without reporting a rejected block.
        atomic_fetch_add_explicit(&e->callbacks, 1, memory_order_relaxed);
        atomic_fetch_add_explicit(&e->rejected, 1, memory_order_relaxed);
        for (uint32_t b = 0; b < output->mNumberBuffers; b++) {
            AudioBuffer *buffer = &output->mBuffers[b];
            if (buffer->mData) memset(buffer->mData, 0, buffer->mDataByteSize);
        }
        return noErr;
    }
    const float *in = NULL;
    if (in_frames) {
        gather(input, e->scratch_in, in_frames, ch, atomic_load_explicit(&e->input_offset, memory_order_relaxed));
        in = e->scratch_in;
    }
    a2a_engine_process(e, in, in_frames, input_time, out_frames ? e->scratch_out : NULL, out_frames, output_time);
    if (out_frames) scatter(e->scratch_out, output, out_frames, ch);
    return noErr;
}
