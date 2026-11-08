// Native Hy-SimulMT worker for the A2A pipeline (research only).
//
// Keeps Hy-SimulMT-30B-A3B resident (manifest `hy-simulmt-30b-a3b-q4km-gguf`, run by the llama.cpp fork for
// PR ggml-org/llama.cpp#31198 on Metal) and translates speech turn by turn in one rolling conversation, the
// way the model was trained and the way the fork's `run_turn_stream` feeds it: the first turn is 1600 ms of
// audio, every later turn 800 ms, and each turn is answered (often with nothing, a WAIT) before the next one.
// The conversation lives in this process's KV cache, so a turn costs one encoder call for its own audio plus
// its answer. Speech still unanswered when the host flushes goes through Tencent's sentence-translation (ST)
// fallback on a second sequence, so every span returns its own text.
//
// The host (A2AModels.HyLocalTranslator) talks to it over the same length-prefixed frames as
// research/tts_worker.py:
//
//     <u32 header bytes, little endian> <u32 payload bytes> <header JSON> <payload>
//
// Host -> worker
//     {"op":"append","id":N,"direction":"zh-en"|"en-zh","rate":R}  payload: f32le mono PCM at R Hz
//         Buffers the audio, feeds every complete turn, and sends {"ev":"delta","id":N,"text":...} for each
//         turn that commits text. A span may arrive in several appends while it is still being spoken (the
//         host streams it); at a rate that is a multiple of 16 kHz the pieces are resampled exactly as the
//         whole span would be, so a streamed span is answered with the same text.
//     {"op":"flush","id":N}
//         Feeds the remainder as a last short turn, runs ST over unanswered speech, then sends
//         {"ev":"done","id":N,"text":<all text committed since the previous flush>,"turns":k,"st":bool,
//          "ms":<since the span's first audio>,"flush_ms":<since this flush>}.
//     {"op":"discard"} forgets a streamed span that was never closed (no text, no ST).
//     {"op":"reset"}   starts a new conversation.
// Worker -> host
//     {"ev":"ready","model":...,"load_s":...,"warmup_s":...,"protocol":2} once, then delta/done, or
//     {"ev":"error","id":N,"message":...}. Protocol 2 accepts streamed spans and `discard`; a worker without
//     the field takes each span in one append.
//
// A span from the pipeline is one or more appends, then flush. The conversation resets after `--window` seconds of
// audio, a pause of `--reset-gap` seconds between requests, a direction change, a runaway or end-of-sentence
// turn, or a nearly full context. Stdin EOF ends the process, so a killed host never leaves the model
// resident. Stdout carries frames only. Stderr gets ready, reset and failure lines; --verbose adds per-turn
// timings, never text (text only with --text).
//
// Test mode (no host): hy-worker --model GGUF --test WAV --spans a:b,c:d | --cuts S --from A --to B [--stream-ms N]
// Throughput probe (no host): hy-worker --model GGUF --bench N  (text prefill + N greedy steps)
// Runtime tuning: [--ctx N] [--batch N] [--ubatch N] [--threads N] [--threads-batch N] [--tower-threads N]
//                 [--kv f16|q8_0] [--mlock] [--perf] [--no-merge-prompt] [--no-kv-unified] [--flash-attn auto|on|off]
// The merged turn batch (`merge_prompt`, on by default, `--no-merge-prompt` to compare) is the one setting that
// measurably helped on the M3 Pro; the rest are here because they were measured and rejected. See
// reports/week2/inference-latency.md.
// Resampler check (no model): hy-worker --self-test
//
// Prompt, turn schedule and ST instructions follow Tencent's SimulInfer (Apache-2.0,
// https://github.com/Tencent-Hunyuan/Hy-SimulMT) and `run_turn_stream` in the fork's tools/mtmd/mtmd-cli.cpp.

#include "llama.h"
#include "mtmd.h"
#include "mtmd-helper.h"
#include <nlohmann/json.hpp>

#ifdef _WIN32
#define _USE_MATH_DEFINES
#include <io.h>
#include <fcntl.h>
#define read _read
#define write _write
#define dup _dup
#define dup2 _dup2
#else
#include <unistd.h>
#endif

#include <algorithm>
#include <cerrno>
#include <cstdarg>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

using json = nlohmann::ordered_json;

namespace {

constexpr int kRate = 16000;
constexpr double kPi = 3.14159265358979323846;
constexpr int kFirstTurn = kRate * 1600 / 1000;
constexpr int kTurn = kRate * 800 / 1000;
constexpr llama_token kEOT = 120008;  // <｜hy_EOT｜>: end of one answer
constexpr llama_token kEOS = 120001;  // <｜hy_end▁of▁sentence｜>

const char * kPre = "<｜hy_begin▁of▁sentence｜><｜reasoning_mode｜>reasoning_effort:no_think<｜hy_User｜>";
const char * kUser = "<｜hy_User｜>";
const char * kAssistant = "<｜hy_Assistant｜><think></think>";

const char * instruction(const std::string & dir) {
    return dir == "en-zh"
        ? "请完成下面的同声传译任务，将陆续收到的语音翻译成中文。请结合上下文边听边译，输出能够确定的译文，不必等待整句结束。"
        : "请完成下面的同声传译任务，将陆续收到的语音翻译成英文。请结合上下文边听边译，输出能够确定的译文，不必等待整句结束。";
}

const char * st_instruction(const std::string & dir) {
    return dir == "en-zh" ? "把这段语音翻译成中文，要全部输出，不要分块。" : "把这段语音翻译成英文，要全部输出，不要分块。";
}

double now_s() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

#ifndef _WIN32
void say(const char * fmt, ...) __attribute__((format(printf, 1, 2)));
#endif
void say(const char * fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    fflush(stderr);
}

float rms(const float * x, size_t n) {
    if (n == 0) return 0.0f;
    double s = 0;
    for (size_t i = 0; i < n; i++) s += double(x[i]) * x[i];
    return float(std::sqrt(s / n));
}

constexpr int kTaps = 127, kHalf = kTaps / 2;

// Windowed-sinc (Blackman) low-pass at 0.45 of the target rate, normalized to unit gain.
std::vector<double> lowpass(int rate) {
    const double cutoff = 0.45 * kRate / rate;
    std::vector<double> h(kTaps);
    double sum = 0;
    for (int i = 0; i < kTaps; i++) {
        const double t = i - kHalf;
        const double sinc = t == 0 ? 1.0 : std::sin(2 * kPi * cutoff * t) / (2 * kPi * cutoff * t);
        const double w = 0.42 - 0.5 * std::cos(2 * kPi * i / (kTaps - 1)) + 0.08 * std::cos(4 * kPi * i / (kTaps - 1));
        h[i] = 2 * cutoff * sinc * w;
        sum += h[i];
    }
    for (double & v : h) v /= sum;
    return h;
}

// Low-pass, then linear interpolation. One span at a time; Decimator streams the integer-ratio case.
std::vector<float> to_16k(const float * x, size_t n, int rate) {
    if (rate == kRate || n == 0) return std::vector<float>(x, x + n);
    std::vector<float> src(x, x + n);
    if (rate > kRate) {
        const int taps = kTaps, half = kHalf;
        const std::vector<double> h = lowpass(rate);
        std::vector<float> y(n);
        for (size_t i = 0; i < n; i++) {
            double acc = 0;
            for (int k = 0; k < taps; k++) {
                const long j = long(i) + k - half;
                if (j >= 0 && j < long(n)) acc += h[k] * x[j];
            }
            y[i] = float(acc);
        }
        src.swap(y);
    }
    const size_t m = size_t(double(n) * kRate / rate);
    std::vector<float> out(m);
    const double step = double(rate) / kRate;
    for (size_t i = 0; i < m; i++) {
        const double p = i * step;
        const size_t a = size_t(p);
        const double f = p - a;
        const float va = src[std::min(a, n - 1)], vb = src[std::min(a + 1, n - 1)];
        out[i] = float(va + (vb - va) * f);
    }
    return out;
}

// to_16k for one span delivered in pieces, for rates that are an integer multiple of 16 kHz (the host
// sends 48 kHz). The same filter, taps and summation order, evaluated only at the kept samples: the
// concatenated output equals to_16k over the whole span, bit for bit, while each 16 kHz sample is ready
// as soon as its filter's right half has arrived. finish() zero-pads past the span end as to_16k does.
class Decimator {
public:
    static bool streams(int rate) { return rate > kRate && rate % kRate == 0; }

    int rate() const { return rate_; }
    bool idle() const { return total == 0; }

    void start(int rate) {
        if (rate != rate_) h = lowpass(rate);
        rate_ = rate;
        ratio = rate / kRate;
        clear();
    }

    void clear() {
        in.clear();
        base = next = total = 0;
    }

    void push(const float * x, size_t n, std::vector<float> & out) {
        in.insert(in.end(), x, x + n);
        total += n;
        while (next * ratio + kHalf < total) out.push_back(sample(next++));
        // Keep the left half of the next sample's filter.
        const size_t keep_from = next * ratio > size_t(kHalf) ? next * ratio - kHalf : 0;
        if (keep_from > base) {
            in.erase(in.begin(), in.begin() + long(keep_from - base));
            base = keep_from;
        }
    }

    void finish(std::vector<float> & out) {
        const size_t m = size_t(double(total) * kRate / rate_);
        while (next < m) out.push_back(sample(next++));
        clear();
    }

private:
    std::vector<double> h;
    std::vector<float> in;  // span samples from index `base`
    int rate_ = 0;
    size_t ratio = 1, base = 0, next = 0, total = 0;

    float sample(size_t i) const {
        const long c = long(i * ratio);
        double acc = 0;
        for (int k = 0; k < kTaps; k++) {
            const long j = c + k - kHalf;
            if (j >= 0 && j < long(total)) acc += h[k] * in[size_t(j) - base];
        }
        return float(acc);
    }
};

// --- model ---------------------------------------------------------------------------------------

struct Options {
    std::string model;
    int ctx = 4096;
    int turn_tokens = 128;
    int st_tokens = 160;
    double window = 30.0;
    double reset_gap = 15.0;
    double min_turn = 0.2;
    float silence_rms = 0.002f;
    bool text = false;
    bool verbose = false;  // per-turn timings, library logs and mtmd timings on stderr
    // Runtime tuning. Each default is the value measured best on the reference Mac; the flags exist so a
    // run can re-measure it (see scripts/bench_hy_local.sh) rather than guess. Nothing here changes the
    // prompt, the turn schedule or the sampler, so the greedy answer for a given config is reproducible.
    int n_batch = 512;      // logical maximum tokens per llama_decode
    int n_ubatch = 512;     // physical maximum: prefill of a whole span's audio embeddings in one pass
    int threads = 0;        // 0 = llama default; used for the CPU part of a step
    int threads_batch = 0;  // 0 = llama default; used for prefill
    int tower_threads = 0;  // 0 = mtmd default (4) for the audio tower
    std::string flash = "auto";  // auto | on | off, for the mixed text+embedding batch especially
    std::string kv = "f16";  // KV cache type: f16 or q8_0
    bool kv_unified = true;  // one cache across both sequences; see --no-kv-unified below
    bool merge_prompt = true;  // one llama_decode per turn instead of one per prompt chunk
    bool mlock = false;      // pin the weights instead of letting the OS page them from the file
    bool perf = false;       // llama's own prompt/decode timing report at exit
};

class Hy {
public:
    explicit Hy(const Options & o) : opt(o) {}

    ~Hy() {
        if (turn_batch) llama_batch_ext_free(turn_batch);
        if (smpl) llama_sampler_free(smpl);
        if (mctx) mtmd_free(mctx);
        if (lctx) llama_free(lctx);
        if (model) llama_model_free(model);
    }

    bool load(std::string & err) {
        // Checked before the 18 GB load, not after it.
        if (opt.flash != "auto" && opt.flash != "on" && opt.flash != "off") { err = "--flash-attn wants auto, on or off"; return false; }
        if (opt.kv != "f16" && opt.kv != "q8_0") { err = "--kv wants f16 or q8_0"; return false; }
        llama_model_params mp = llama_model_default_params();
        mp.n_gpu_layers = 999;
        if (opt.mlock) mp.load_mode = LLAMA_LOAD_MODE_MMAP_MLOCK;  // pages stay resident under memory pressure
        model = llama_model_load_from_file(opt.model.c_str(), mp);
        if (!model) { err = "model failed to load"; return false; }
        vocab = llama_model_get_vocab(model);
        llama_context_params cp = llama_context_default_params();
        cp.n_ctx = opt.ctx;
        cp.n_batch = opt.n_batch;
        cp.n_ubatch = std::min(opt.n_ubatch, opt.n_batch);
        cp.n_seq_max = 2;      // seq 0: the conversation, seq 1: ST fallback
        // Unified cells let the two sequences share the primed prefix. Separate cells scan only their own
        // sequence, which llama.cpp documents as faster when the sequences share little; ST shares only its
        // own prompt, so both are worth measuring (`--no-kv-unified`).
        cp.kv_unified = opt.kv_unified;
        cp.no_perf = !opt.perf;
        if (opt.threads > 0) cp.n_threads = opt.threads;
        if (opt.threads_batch > 0) cp.n_threads_batch = opt.threads_batch;
        if (opt.kv == "q8_0") { cp.type_k = GGML_TYPE_Q8_0; cp.type_v = GGML_TYPE_Q8_0; }
        cp.flash_attn_type = opt.flash == "on" ? LLAMA_FLASH_ATTN_TYPE_ENABLED
                             : opt.flash == "off" ? LLAMA_FLASH_ATTN_TYPE_DISABLED
                                                  : LLAMA_FLASH_ATTN_TYPE_AUTO;
        lctx = llama_init_from_model(model, cp);
        if (!lctx) { err = "context failed to initialize"; return false; }
        mtmd_context_params mcp = mtmd_context_params_default();
        mcp.use_gpu = true;
        mcp.print_timings = opt.verbose;
        mcp.warmup = true;
        if (opt.tower_threads > 0) mcp.n_threads = opt.tower_threads;  // default 4 starves a 12-core Mac
        mctx = mtmd_init_from_file(opt.model.c_str(), model, mcp);  // the published GGUFs carry their own audio tower
        if (!mctx || !mtmd_support_audio(mctx)) { err = "audio tower failed to load"; return false; }
        if (mtmd_get_audio_sample_rate(mctx) != kRate) { err = "unexpected audio sample rate"; return false; }
        llama_sampler_chain_params sp = llama_sampler_chain_default_params();
        smpl = llama_sampler_chain_init(sp);
        llama_sampler_chain_add(smpl, llama_sampler_init_greedy());
        if (single_token(kUser) == LLAMA_TOKEN_NULL || single_token("<｜hy_EOT｜>") != kEOT) {
            err = "unexpected vocabulary";
            return false;
        }
        say("config: ctx %d, batch %d/%d, threads %d/%d (llama default %d/%d), tower threads %d, kv %s, mlock %d, "
            "flash attn %s, layers %d",
            llama_n_ctx(lctx), llama_n_batch(lctx), llama_n_ubatch(lctx), cp.n_threads, cp.n_threads_batch,
            llama_context_default_params().n_threads, llama_context_default_params().n_threads_batch,
            opt.tower_threads > 0 ? opt.tower_threads : 4, cp.type_k == GGML_TYPE_Q8_0 ? "q8_0" : "f16",
            int(opt.mlock), llama_flash_attn_type_name(cp.flash_attn_type), llama_model_n_layer(model));
        return true;
    }

    // Feeds 16 kHz audio; returns text committed by complete turns. A span may arrive in several appends
    // while it is still being spoken; the conversation's reset rules apply where a span starts, as when
    // the span arrived whole.
    std::vector<std::string> append(const std::string & dir, const std::vector<float> & pcm) {
        std::vector<std::string> out;
        if (!span_open) {
            begin(dir);
            span_open = true;
        }
        buffer.insert(buffer.end(), pcm.begin(), pcm.end());
        for (;;) {
            const size_t size = turns == 0 ? kFirstTurn : kTurn;
            if (buffer.size() < size) break;
            std::vector<float> chunk(buffer.begin(), buffer.begin() + size);
            buffer.erase(buffer.begin(), buffer.begin() + size);
            std::string text = feed(chunk);
            if (!text.empty()) out.push_back(text);
        }
        return out;
    }

    // Feeds the remainder and translates unanswered speech; returns the text it committed.
    std::vector<std::string> flush() {
        std::vector<std::string> out;
        if (!buffer.empty()) {
            std::vector<float> rest;
            rest.swap(buffer);
            if (rest.size() < size_t(opt.min_turn * kRate) && !pending.empty()) {
                pending.insert(pending.end(), rest.begin(), rest.end());  // a sliver: let ST hear it
                pending_voiced |= rms(rest.data(), rest.size()) >= opt.silence_rms;
            } else {
                std::string text = feed(rest);
                if (!text.empty()) out.push_back(text);
            }
        }
        if (pending_voiced && !pending.empty()) {
            std::string text = sentence(pending);
            span_st = true;
            if (!text.empty()) {
                replace_last_reply(text);
                out.push_back(text);
            }
        }
        pending.clear();
        pending_voiced = false;
        span_open = false;
        last_end = now_s();
        if (needs_reset) reset("runaway turn");
        return out;
    }

    // Forgets a span that was streamed but never closed (the host's segmenter dropped it). Turns already
    // fed stay in the conversation; unfed and unanswered audio is not translated.
    void discard() {
        buffer.clear();
        pending.clear();
        pending_voiced = false;
        span_open = false;
    }

    // Ends the conversation. The resident task prompts survive unless `keep_prompts` is false (after a
    // failed decode the cache is not trusted) or the cache cannot drop just the conversation.
    void reset(const char * why, bool keep_prompts = true) {
        if (conv_started) say("conversation reset (%s) after %.1f s, %d turns", why, audio_s, turns);
        if (!(keep_prompts && !primed.empty() && llama_memory_seq_rm(llama_get_memory(lctx), 0, conv_prompt, -1))) {
            llama_memory_clear(llama_get_memory(lctx), true);
            primed.clear();
            conv_prompt = st_prompt = 0;
        }
        n_past = conv_prompt;
        turns = 0;
        audio_s = 0;
        conv_started = false;
        needs_reset = false;
        eot_pending = false;
        prefix_pending = false;
        first_turn = false;
        last_reply_start = -1;
        buffer.clear();
        pending.clear();
        pending_voiced = false;
        span_open = false;
    }

    int span_turns = 0;
    bool span_st = false;

    // Text-only prefill + greedy decode, with no audio and no conversation: the way to attribute a step's
    // cost when comparing runtime configs. The words are arbitrary; only the timing and the token count mean
    // anything. `turn_like` prefills one turn's worth of tokens (what a real turn submits), `span` a whole
    // span's audio-plus-text.
    bool bench(int tokens, bool turn_like, double & prefill_ms, double & per_token_ms) {
        reset("bench", false);
        const std::string text = turn_like
            ? std::string(kUser) + kAssistant
            : std::string(kPre) + instruction("zh-en") + "今天我们要讨论下个季度的计划，请大家先介绍一下自己的进展，然后我们讨论预算。" + kAssistant;
        const auto toks = tokenize(text);
        llama_pos pos = 0;
        const double p0 = now_s();
        if (!decode(toks, pos, 0, true)) { fail("bench prefill"); return false; }
        prefill_ms = (now_s() - p0) * 1000;
        // One sampled token costs a full pass over the weights: read the logits, then feed the token back.
        double sum = 0;
        for (int n = 0; n < tokens; n++) {
            const double t0 = now_s();
            const llama_token t = llama_sampler_sample(smpl, lctx, -1);
            llama_sampler_accept(smpl, t);
            if (!decode({t}, pos, 0, true)) { fail("bench decode"); return false; }
            sum += now_s() - t0;
        }
        per_token_ms = sum * 1000 / std::max(1, tokens);
        return true;
    }

private:
    const Options opt;
    llama_model * model = nullptr;
    llama_context * lctx = nullptr;
    mtmd_context * mctx = nullptr;
    llama_sampler * smpl = nullptr;
    const llama_vocab * vocab = nullptr;

    std::string direction;
    llama_pos n_past = 0;
    int turns = 0;
    double audio_s = 0;
    double last_end = 0;
    bool conv_started = false;
    bool needs_reset = false;
    bool eot_pending = false;     // the last answer's end-of-turn token, decoded with the next turn's prompt
    bool prefix_pending = false;  // the task prompt, decoded with the conversation's first audible turn
    bool first_turn = false;      // the conversation's first audible turn is still to come (no user tag)
    // Both task prompts stay decoded for the current direction, the conversation's in seq 0 and ST's in
    // seq 1, so neither a new conversation nor an ST fallback prefills its prompt again (~200 ms each).
    std::string primed;           // direction of the resident prompts, "" for none
    llama_pos conv_prompt = 0;    // seq 0 positions [0, conv_prompt)
    llama_pos st_prompt = 0;      // seq 1 positions [0, st_prompt)
    bool span_open = false;       // audio of the current span has arrived; it ends at flush or discard
    llama_pos last_reply_start = -1;
    std::vector<float> buffer;   // audio not yet fed as a turn
    std::vector<float> pending;  // fed audio with no committed text yet
    bool pending_voiced = false;
    llama_batch_ext * turn_batch = nullptr;  // reused mixed batch of the merged prompt

    std::vector<llama_token> tokenize(const std::string & text) {
        std::vector<llama_token> toks(text.size() + 8);
        int n = llama_tokenize(vocab, text.c_str(), int(text.size()), toks.data(), int(toks.size()), false, true);
        if (n < 0) {
            toks.resize(-n);
            n = llama_tokenize(vocab, text.c_str(), int(text.size()), toks.data(), int(toks.size()), false, true);
        }
        toks.resize(std::max(n, 0));
        return toks;
    }

    llama_token single_token(const char * text) {
        auto t = tokenize(text);
        return t.size() == 1 ? t[0] : LLAMA_TOKEN_NULL;
    }

    std::string piece(llama_token t) {
        char buf[256];
        const int n = llama_token_to_piece(vocab, t, buf, sizeof buf, 0, false);
        return n > 0 ? std::string(buf, n) : std::string();
    }

    bool decode(const std::vector<llama_token> & toks, llama_pos & pos, llama_seq_id seq, bool logits_last) {
        if (toks.empty()) return true;
        llama_batch b = llama_batch_init(int(toks.size()), 0, 1);
        for (size_t i = 0; i < toks.size(); i++) {
            b.token[i] = toks[i];
            b.pos[i] = pos + llama_pos(i);
            b.n_seq_id[i] = 1;
            b.seq_id[i][0] = seq;
            b.logits[i] = logits_last && i + 1 == toks.size();
        }
        b.n_tokens = int(toks.size());
        const int rc = llama_decode(lctx, b);
        llama_batch_free(b);
        if (rc != 0) return false;
        pos += llama_pos(toks.size());
        return true;
    }

    // One user turn as a single prompt: `before`, the audio (mtmd adds its begin/end markers) and the
    // assistant header. Every llama_decode reads the whole active weight set (about 2 GB for this model,
    // ~15 ms at the M3 Pro's bandwidth), so the number of submits matters as much as the token count.
    // mtmd's helper evaluates each input chunk as its own batch — text, audio, text — which is three full
    // passes for one turn. `merge_prompt` puts them in one mixed token/embedding batch instead, keeping
    // the same positions, the same causal mask and the logits on the header's last token, so the first
    // sampled token needs no extra pass. Anything this cannot express (M-RoPE, a non-causal audio chunk,
    // a batch over `n_batch`) still goes through the helper. One pass instead of three is a different tiling,
    // so a near-tie greedy sample can flip to the other side ("customers" for "guests"): content holds, the
    // exact tokens are not a bit-for-bit contract. reports/week2/inference-latency.md has the measurements.
    // `--no-merge-prompt` restores the old tokens when a run must reproduce them.
    bool eval_turn(const std::string & before, const std::vector<float> & pcm, llama_pos & pos, llama_seq_id seq) {
        const std::string prompt = before + mtmd_default_marker() + kAssistant;
        const double t0 = now_s();
        mtmd_bitmap * bmp = mtmd_bitmap_init_from_audio(pcm.size(), pcm.data());
        mtmd_input_chunks * chunks = mtmd_input_chunks_init();
        const mtmd_input_text text = {prompt.c_str(), prompt.size(), false, true};
        const mtmd_bitmap * bitmaps[] = {bmp};
        bool ok = mtmd_tokenize(mctx, chunks, &text, bitmaps, 1) == 0;
        const double t1 = now_s();  // mel front end and prompt tokenization
        llama_pos next = pos;
        bool merged = false;
        if (ok) {
            // A positive code means the merged shape did not apply and nothing was decoded, so the helper
            // still can be. A negative one means a partial turn, which no second pass can fix: the caller
            // resets the conversation, as it does for any failed turn.
            const int32_t rc = opt.merge_prompt ? eval_chunks_merged(chunks, next, seq) : 1;
            if (rc == 0) merged = true, ok = true;
            else ok = rc < 0 ? false : eval_chunks_helper(chunks, next, seq) == 0;
        }
        const double t2 = now_s();  // audio tower + prefill of the turn
        mtmd_input_chunks_free(chunks);
        mtmd_bitmap_free(bmp);
        if (ok) pos = next;
        if (opt.verbose) say("  eval: mel+tokenize %.0f ms, tower+prefill %.0f ms, %s", (t1 - t0) * 1000,
                             (t2 - t1) * 1000, merged ? "one batch" : "per-chunk batches");
        return ok;
    }

    int32_t eval_chunks_helper(const mtmd_input_chunks * chunks, llama_pos & pos, llama_seq_id seq) {
        llama_pos next = pos;
        const int32_t rc = mtmd_helper_eval_chunks(mctx, lctx, chunks, pos, seq, opt.n_batch, true, &next);
        if (rc == 0) pos = next;
        return rc;
    }

    // The whole turn in one llama_decode: text rows carry token ids, audio rows carry tower embeddings.
    // 0 on success, >0 when the turn cannot be merged (nothing decoded), <0 when a decode failed midway.
    int32_t eval_chunks_merged(const mtmd_input_chunks * chunks, llama_pos & pos, llama_seq_id seq) {
        const size_t n = mtmd_input_chunks_size(chunks);
        const int n_embd = llama_model_n_embd_inp(model);
        // Positions and the causal mask must be uniform across the turn.
        int rows = 0;
        for (size_t i = 0; i < n; i++) {
            const mtmd_input_chunk * c = mtmd_input_chunks_get(chunks, i);
            if (mtmd_decode_use_non_causal(mctx, c) || mtmd_decode_use_mrope(mctx)) return 1;
            const int n_tok = int(mtmd_input_chunk_get_n_tokens(c));
            if (int(mtmd_input_chunk_get_n_pos(c)) != n_tok) return 1;
            rows += n_tok;
        }
        if (rows <= 0 || rows > opt.n_batch) return 1;
        if (!turn_batch) turn_batch = llama_batch_ext_init(lctx);
        if (!turn_batch) return 1;
        llama_batch_ext_clear(turn_batch);
        llama_pos p = pos;
        int32_t last = -1;
        for (size_t i = 0; i < n; i++) {
            const mtmd_input_chunk * c = mtmd_input_chunks_get(chunks, i);
            const int n_tok = int(mtmd_input_chunk_get_n_tokens(c));
            if (mtmd_input_chunk_get_type(c) == MTMD_INPUT_CHUNK_TYPE_TEXT) {
                size_t n_text = 0;
                const llama_token * toks = mtmd_input_chunk_get_tokens_text(c, &n_text);
                for (size_t k = 0; k < n_text; k++) {
                    last = llama_batch_ext_add_token(turn_batch, seq, toks[k]);
                    if (last < 0) return 1;
                    llama_batch_ext_set_pos(turn_batch, last, &p);
                    p++;
                }
            } else {
                if (mtmd_encode_chunk(mctx, c) != 0) return 1;
                const float * embd = mtmd_get_output_embd(mctx);
                if (!embd || n_tok <= 0) return 1;
                for (int k = 0; k < n_tok; k++) {
                    const llama_embd row{embd + size_t(k) * n_embd, 1, size_t(n_embd)};
                    last = llama_batch_ext_add_embd(turn_batch, seq, row);
                    if (last < 0) return 1;
                    llama_batch_ext_set_pos(turn_batch, last, &p);
                    p++;
                }
            }
        }
        if (last < 0) return 1;
        llama_batch_ext_set_output_logits(turn_batch, last, true);
        if (llama_process(lctx, LLAMA_PROCESS_TYPE_DECODE, turn_batch) != 0) return -1;
        pos = p;
        return 0;
    }

    // Greedy answer after the assistant header. The end-of-turn token is not decoded here: the next turn's
    // prompt starts with it, so the model still sees a finished assistant turn, one decode call later.
    // Returns false on a decode failure.
    bool generate(llama_pos & pos, llama_seq_id seq, int limit, std::string & text, bool & ended_turn, bool & eos) {
        ended_turn = eos = false;
        for (int n = 0; n < limit; n++) {
            const llama_token t = llama_sampler_sample(smpl, lctx, -1);
            llama_sampler_accept(smpl, t);
            if (t == kEOT || llama_vocab_is_eog(vocab, t)) {
                ended_turn = true;
                eos = t == kEOS;
                return true;
            }
            if (!llama_vocab_is_control(vocab, t)) text += piece(t);
            if (!decode({t}, pos, seq, true)) return false;
        }
        return true;  // hit the limit: a runaway answer
    }

    // Decodes both task prompts for `dir` into an empty cache. On failure the prompts go with each turn as before.
    bool prime(const std::string & dir) {
        llama_memory_clear(llama_get_memory(lctx), true);
        primed.clear();
        conv_prompt = st_prompt = 0;
        llama_pos a = 0, b = 0;
        if (!decode(tokenize(std::string(kPre) + instruction(dir)), a, 0, false) ||
            !decode(tokenize(std::string(kPre) + st_instruction(dir)), b, 1, false)) {
            llama_memory_clear(llama_get_memory(lctx), true);
            say("task prompts could not be kept resident");
            return false;
        }
        primed = dir;
        conv_prompt = a;
        st_prompt = b;
        return true;
    }

    void begin(const std::string & dir) {
        const double t = now_s();
        const char * why = nullptr;
        if (conv_started) {
            if (dir != direction) why = "direction";
            else if (audio_s >= opt.window && buffer.empty()) why = "window";
            else if (last_end > 0 && t - last_end >= opt.reset_gap && buffer.empty()) why = "gap";
            else if (n_past + 3 * opt.turn_tokens >= llama_pos(llama_n_ctx(lctx)) - opt.st_tokens - 256) why = "context";
        }
        if (why) reset(why);
        if (!conv_started) {
            if (primed != dir) prime(dir);  // first use, a direction change, or after a failed decode
            direction = dir;
            conv_started = true;
            n_past = primed == dir ? conv_prompt : 0;
            prefix_pending = primed != dir;
            first_turn = true;
        }
    }

    std::string clean(std::string s) {
        const auto a = s.find_first_not_of(" \t\r\n");
        if (a == std::string::npos) return "";
        const auto b = s.find_last_not_of(" \t\r\n");
        return s.substr(a, b - a + 1);
    }

    // One turn of the conversation (run_turn_stream); returns committed text or "".
    std::string feed(const std::vector<float> & chunk) {
        const float level = rms(chunk.data(), chunk.size());
        if (level < opt.silence_rms) return "";  // nothing audible: not a turn, no filler
        if (!conv_started) begin(direction);     // an earlier turn in this span reset the conversation
        const double t0 = now_s();
        std::string before = eot_pending ? "<｜hy_EOT｜>" : "";
        if (prefix_pending) before += std::string(kPre) + instruction(direction);
        else if (!first_turn) before += kUser;  // the first turn follows the task prompt directly
        if (!eval_turn(before, chunk, n_past, 0)) return fail("audio");
        eot_pending = prefix_pending = first_turn = false;
        const double t1 = now_s();
        const llama_pos start = n_past;
        std::string text;
        bool ended = false, eos = false;
        if (!generate(n_past, 0, opt.turn_tokens, text, ended, eos)) return fail("generate");
        eot_pending = ended && !eos;
        turns++;
        span_turns++;
        audio_s += double(chunk.size()) / kRate;
        if (!ended || eos) needs_reset = true;
        last_reply_start = start;
        text = clean(text);
        if (text.empty()) {
            pending.insert(pending.end(), chunk.begin(), chunk.end());
            pending_voiced = true;
        } else {
            pending.clear();
            pending_voiced = false;
        }
        if (opt.verbose) say("turn %d: %.2f s audio, encode+prefill %.0f ms, answer %.0f ms, %s", turns,
             double(chunk.size()) / kRate, (t1 - t0) * 1000, (now_s() - t1) * 1000, text.empty() ? "wait" : "text");
        return text;
    }

    std::string fail(const char * where) {
        say("turn failed (%s): conversation reset", where);
        reset(where, false);
        return "";
    }

    // Sentence translation of unanswered speech on seq 1, without disturbing the conversation in seq 0.
    std::string sentence(const std::vector<float> & pcm) {
        const double t0 = now_s();
        const bool resident = primed == direction && st_prompt > 0;
        llama_pos pos = resident ? st_prompt : 0;
        std::string text;
        bool ended = false, eos = false;
        const bool ok = eval_turn(resident ? std::string() : std::string(kPre) + st_instruction(direction), pcm, pos, 1) &&
                        generate(pos, 1, opt.st_tokens, text, ended, eos);
        llama_memory_seq_rm(llama_get_memory(lctx), 1, resident ? st_prompt : -1, -1);
        if (!ok) say("ST failed");
        if (opt.verbose) say("ST: %.2f s audio, %.0f ms", double(pcm.size()) / kRate, (now_s() - t0) * 1000);
        return ok ? clean(text) : "";
    }

    // The conversation records the tail as answered at its last turn, as SimulInfer does.
    void replace_last_reply(const std::string & text) {
        if (last_reply_start < 0) return;
        if (!llama_memory_seq_rm(llama_get_memory(lctx), 0, last_reply_start, -1)) {
            needs_reset = true;  // a partial removal this cache cannot do: start over rather than continue corrupt
            return;
        }
        n_past = last_reply_start;
        if (!decode(tokenize(text), n_past, 0, false)) needs_reset = true;  // its EOT stays pending
    }
};

// --- protocol ------------------------------------------------------------------------------------

int g_out = -1;

bool read_exact(int fd, void * buf, size_t n) {
    auto * p = static_cast<unsigned char *>(buf);
    while (n > 0) {
        const auto r = read(fd, p, n);
        if (r < 0 && errno == EINTR) continue;
        if (r <= 0) return false;
        p += r;
        n -= size_t(r);
    }
    return true;
}

void send(const json & header) {
    const std::string h = header.dump();
    const uint32_t hl = uint32_t(h.size()), pl = 0;
    std::string frame(8 + h.size(), '\0');
    memcpy(&frame[0], &hl, 4);  // Apple silicon is little-endian
    memcpy(&frame[4], &pl, 4);
    memcpy(&frame[8], h.data(), h.size());
    size_t off = 0;
    while (off < frame.size()) {
        const auto w = write(g_out, frame.data() + off, frame.size() - off);
        if (w < 0 && errno == EINTR) continue;
        if (w <= 0) _exit(0);  // host gone
        off += size_t(w);
    }
}

std::string join(const std::vector<std::string> & parts, const std::string & dir) {
    std::string s;
    for (const auto & p : parts) s += (s.empty() ? "" : (dir == "en-zh" ? "" : " ")) + p;
    return s;
}

bool g_verbose = false;

void silence_logs(enum ggml_log_level level, const char * text, void *) {
    if (g_verbose || level >= GGML_LOG_LEVEL_WARN) fputs(text, stderr);
}

// One span at a time, as the host sends it: audio at the host's rate in one or more appends (it may stream
// a span while it is being spoken), answered at flush. Serve and test mode both go through here.
class Session {
public:
    explicit Session(Hy & hy) : hy(hy) {}

    struct Answer {
        std::string text;  // everything committed for the span
        int turns = 0;
        bool st = false;
    };

    std::string dir = "zh-en";

    // Returns the text of each turn this audio completed.
    std::vector<std::string> append(const float * x, size_t n, int rate) {
        std::vector<float> pcm;
        if (Decimator::streams(rate)) {
            if (dec.rate() != rate) {
                if (!dec.idle()) dec.finish(pcm);
                dec.start(rate);
            }
            dec.push(x, n, pcm);
        } else {
            if (!dec.idle()) dec.finish(pcm);
            const auto low = to_16k(x, n, rate);
            pcm.insert(pcm.end(), low.begin(), low.end());
        }
        return keep(pcm.empty() ? std::vector<std::string>() : hy.append(dir, pcm));
    }

    // Closes the span: returns the text of each turn it completed, and the span's whole answer.
    std::vector<std::string> flush(Answer & answer) {
        std::vector<float> rest;
        if (!dec.idle()) dec.finish(rest);
        auto texts = keep(rest.empty() ? std::vector<std::string>() : hy.append(dir, rest));
        for (auto & t : keep(hy.flush())) texts.push_back(t);
        answer = {join(collected, dir), hy.span_turns, hy.span_st};
        collected.clear();
        hy.span_turns = 0;
        hy.span_st = false;
        return texts;
    }

    // A streamed span the host dropped: none of its text is answered.
    void discard() {
        dec.clear();
        hy.discard();
        collected.clear();
        hy.span_turns = 0;
        hy.span_st = false;
    }

    void reset() {
        dec.clear();
        hy.reset("host");
        collected.clear();
    }

private:
    Hy & hy;
    Decimator dec;
    std::vector<std::string> collected;  // text committed since the previous flush

    std::vector<std::string> keep(std::vector<std::string> texts) {
        collected.insert(collected.end(), texts.begin(), texts.end());
        return texts;
    }
};

int serve(Hy & hy, const Options & opt) {
    Session session(hy);
    double span_t0 = 0;
    auto deltas = [&](const json & id, const std::vector<std::string> & texts) {
        for (auto & t : texts) {
            if (opt.text) say("text: %s", t.c_str());
            send({{"ev", "delta"}, {"id", id}, {"text", t}});
        }
    };
    for (;;) {
        uint32_t lens[2];
        if (!read_exact(0, lens, 8)) return 0;  // stdin EOF: the host is done
        if (lens[0] > (1u << 20) || lens[1] > (64u << 20) || lens[1] % 4) return 2;
        std::string header(lens[0], '\0');
        std::vector<float> payload(lens[1] / 4);
        if (!read_exact(0, header.data(), lens[0]) || (lens[1] && !read_exact(0, payload.data(), lens[1]))) return 0;
        json req = json::parse(header, nullptr, false);
        if (req.is_discarded() || !req.contains("op")) return 2;
        const std::string op = req["op"];
        const json id = req.value("id", json());
        if (op == "reset") {
            session.reset();
            span_t0 = 0;
        } else if (op == "discard") {
            session.discard();
            span_t0 = 0;
        } else if (op == "append") {
            if (span_t0 == 0) span_t0 = now_s();
            const std::string dir = req.value("direction", std::string("zh-en"));
            if (dir != "zh-en" && dir != "en-zh") {
                send({{"ev", "error"}, {"id", id}, {"message", "direction must be zh-en or en-zh"}});
                continue;
            }
            const int rate = req.value("rate", kRate);
            if (rate <= 0) {
                send({{"ev", "error"}, {"id", id}, {"message", "invalid rate"}});
                continue;
            }
            session.dir = dir;
            deltas(id, session.append(payload.data(), payload.size(), rate));
        } else if (op == "flush") {
            const double f0 = now_s();
            if (span_t0 == 0) span_t0 = f0;
            Session::Answer answer;
            deltas(id, session.flush(answer));
            const double t = now_s();
            const double ms = (t - span_t0) * 1000, flush_ms = (t - f0) * 1000;
            if (opt.verbose) say("span: %d turns%s, %.0f ms, %.0f ms after flush", answer.turns, answer.st ? " + ST" : "", ms, flush_ms);
            // ms: from the span's first audio; flush_ms: what the host waited once the span was closed.
            send({{"ev", "done"}, {"id", id}, {"text", answer.text}, {"turns", answer.turns},
                  {"st", answer.st}, {"ms", int(ms)}, {"flush_ms", int(flush_ms)}});
            span_t0 = 0;
        } else {
            send({{"ev", "error"}, {"id", id}, {"message", "unknown op"}});
        }
    }
}

// Model-free check that streamed resampling equals whole-span resampling, bit for bit.
int self_test() {
    uint32_t seed = 7;
    auto rnd = [&]() { seed = seed * 1664525u + 1013904223u; return seed >> 8; };
    for (int rate : {32000, 48000, 96000}) {
        for (int trial = 0; trial < 20; trial++) {
            std::vector<float> x(1 + rnd() % (rate * 2));
            for (auto & v : x) v = float(int(rnd() % 20001) - 10000) / 10000.0f;
            const auto whole = to_16k(x.data(), x.size(), rate);
            Decimator dec;
            dec.start(rate);
            std::vector<float> parts;
            for (size_t p = 0; p < x.size();) {
                const size_t n = std::min<size_t>(x.size() - p, 1 + rnd() % 3000);
                dec.push(x.data() + p, n, parts);
                p += n;
            }
            dec.finish(parts);
            if (parts.size() != whole.size() || memcmp(parts.data(), whole.data(), whole.size() * sizeof(float)) != 0) {
                say("self-test failed: rate %d, %zu samples", rate, x.size());
                return 1;
            }
        }
    }
    say("self-test passed");
    return 0;
}

}  // namespace

// Test mode: a minimal WAV reader (PCM16/24/32, float32), mixed to mono at the file's rate.
static std::vector<float> read_wav(const std::string & path, int & out_rate) {
    FILE * f = fopen(path.c_str(), "rb");
    if (!f) return {};
    std::vector<unsigned char> d;
    unsigned char tmp[1 << 16];
    size_t n;
    while ((n = fread(tmp, 1, sizeof tmp, f)) > 0) d.insert(d.end(), tmp, tmp + n);
    fclose(f);
    if (d.size() < 12 || memcmp(d.data(), "RIFF", 4) || memcmp(d.data() + 8, "WAVE", 4)) return {};
    size_t pos = 12;
    uint16_t code = 0, ch = 0, bits = 0;
    uint32_t rate = 0;
    const unsigned char * data = nullptr;
    size_t len = 0;
    while (pos + 8 <= d.size()) {
        uint32_t size;
        memcpy(&size, &d[pos + 4], 4);
        if (!memcmp(&d[pos], "fmt ", 4) && size >= 16) {
            memcpy(&code, &d[pos + 8], 2);
            memcpy(&ch, &d[pos + 10], 2);
            memcpy(&rate, &d[pos + 12], 4);
            memcpy(&bits, &d[pos + 22], 2);
            if (code == 0xFFFE && size >= 26) memcpy(&code, &d[pos + 32], 2);
        } else if (!memcmp(&d[pos], "data", 4)) {
            data = &d[pos + 8];
            len = std::min<size_t>(size, d.size() - pos - 8);
        }
        pos += 8 + size + (size & 1);
    }
    if (!data || !ch || !rate) return {};
    std::vector<float> mono;
    const size_t frame = size_t(bits / 8) * ch;
    for (size_t i = 0; i + frame <= len; i += frame) {
        double s = 0;
        for (int c = 0; c < ch; c++) {
            const unsigned char * p = data + i + size_t(c) * (bits / 8);
            if (code == 3 && bits == 32) { float v; memcpy(&v, p, 4); s += v; }
            else if (code == 1 && bits == 16) { int16_t v; memcpy(&v, p, 2); s += v / 32768.0; }
            else if (code == 1 && bits == 32) { int32_t v; memcpy(&v, p, 4); s += v / 2147483648.0; }
            else if (code == 1 && bits == 24) { int32_t v = p[0] | (p[1] << 8) | (p[2] << 16); v = (v << 8) >> 8; s += v / 8388608.0; }
            else return {};
        }
        mono.push_back(float(s / ch));
    }
    out_rate = int(rate);
    return mono;
}

// FNV-1a: lets runs be compared for identical text without printing it.
static uint64_t text_hash(const std::string & s) {
    uint64_t h = 1469598103934665603ull;
    for (unsigned char c : s) h = (h ^ c) * 1099511628211ull;
    return h;
}

int main(int argc, char ** argv) {
    // The protocol owns the original stdout; anything a library prints goes to stderr.
    g_out = dup(1);
    dup2(2, 1);
#ifdef _WIN32
    // Framed PCM/JSON must not see Ctrl-Z or CRLF translation on Windows.
    _setmode(0, _O_BINARY);
    _setmode(g_out, _O_BINARY);
#endif
    Options opt;
    std::string test_wav, spans_arg, direction = "zh-en";
    double cuts = 0, from = 0, to = -1, stream_ms = 0;
    int bench_tokens = 0;
    for (int i = 1; i < argc; i++) {
        const std::string a = argv[i];
        auto next = [&]() -> std::string { return i + 1 < argc ? argv[++i] : ""; };
        if (a == "--model") opt.model = next();
        else if (a == "--ctx") opt.ctx = std::stoi(next());
        else if (a == "--window") opt.window = std::stod(next());
        else if (a == "--reset-gap") opt.reset_gap = std::stod(next());
        else if (a == "--silence-rms") opt.silence_rms = std::stof(next());
        else if (a == "--text") opt.text = true;
        else if (a == "--verbose") opt.verbose = g_verbose = true;
        else if (a == "--test") test_wav = next();
        else if (a == "--bench") bench_tokens = std::stoi(next());
        else if (a == "--spans") spans_arg = next();
        else if (a == "--cuts") cuts = std::stod(next());
        else if (a == "--from") from = std::stod(next());
        else if (a == "--to") to = std::stod(next());
        else if (a == "--direction") direction = next();
        else if (a == "--stream-ms") stream_ms = std::stod(next());
        else if (a == "--batch") opt.n_batch = std::stoi(next());
        else if (a == "--ubatch") opt.n_ubatch = std::stoi(next());
        else if (a == "--threads") opt.threads = std::stoi(next());
        else if (a == "--threads-batch") opt.threads_batch = std::stoi(next());
        else if (a == "--tower-threads") opt.tower_threads = std::stoi(next());
        else if (a == "--kv") opt.kv = next();
        else if (a == "--no-kv-unified") opt.kv_unified = false;
        else if (a == "--flash-attn") opt.flash = next();
        else if (a == "--no-merge-prompt") opt.merge_prompt = false;
        else if (a == "--mlock") opt.mlock = true;
        else if (a == "--perf") opt.perf = true;
        else if (a == "--self-test") return self_test();
        else { say("unknown argument %s", a.c_str()); return 2; }
    }
    if (opt.model.empty()) { say("usage: hy-worker --model GGUF [--test WAV ...]"); return 2; }
    llama_log_set(silence_logs, nullptr);
    mtmd_helper_log_set(silence_logs, nullptr);
    llama_backend_init();

    Hy hy(opt);
    const double t0 = now_s();
    std::string err;
    if (!hy.load(err)) {
        send({{"ev", "error"}, {"message", err}});
        say("load failed: %s", err.c_str());
        return 1;
    }
    const double load_s = now_s() - t0;
    std::vector<float> noise(kFirstTurn + kTurn);
    uint32_t seed = 1;
    for (auto & v : noise) { seed = seed * 1664525u + 1013904223u; v = (int(seed >> 9) / 4194304.0f - 1.0f) * 0.02f; }
    const double t1 = now_s();
    hy.append("zh-en", noise);
    hy.flush();
    hy.reset("warm-up");
    hy.span_turns = 0;
    hy.span_st = false;
    const double warm_s = now_s() - t1;
    say("ready: load %.1f s, warm-up %.1f s", load_s, warm_s);

    if (bench_tokens > 0) {
        for (int pass = 0; pass < 2; pass++) {  // a turn's prompt, then a whole span's
            double prefill = 0, per_token = 0;
            if (!hy.bench(bench_tokens, pass == 0, prefill, per_token)) return 1;
            say("bench (%s prompt): prefill %.0f ms, decode %.2f ms/token (%d tokens)", pass == 0 ? "turn" : "span",
                prefill, per_token, bench_tokens);
        }
        return 0;
    }

    if (test_wav.empty()) {
        const std::string name = opt.model.substr(opt.model.find_last_of('/') + 1);
        // protocol 2: spans may be streamed in several appends, and `discard` exists.
        send({{"ev", "ready"}, {"model", name}, {"load_s", load_s}, {"warmup_s", warm_s}, {"streaming", true}, {"protocol", 2}});
        return serve(hy, opt);
    }

    // Test mode: spans one after another, as the pipeline would send them: each span at the file's rate,
    // whole (as a closed segment) or with --stream-ms N in N ms appends (as streamed while it is spoken).
    // Both must answer the same text; the hash shows it without --text.
    int rate = 0;
    auto audio = read_wav(test_wav, rate);
    if (audio.empty() || rate <= 0) { say("cannot read %s", test_wav.c_str()); return 1; }
    std::vector<std::pair<double, double>> spans;
    if (cuts > 0) {
        const double end = to > 0 ? to : double(audio.size()) / rate;
        for (double t = from; t < end - 0.05; t += cuts) spans.push_back({t, std::min(t + cuts, end)});
    } else {
        size_t p = 0;
        while (p < spans_arg.size()) {
            size_t q = spans_arg.find(',', p);
            if (q == std::string::npos) q = spans_arg.size();
            const std::string s = spans_arg.substr(p, q - p);
            const size_t c = s.find(':');
            if (c != std::string::npos) spans.push_back({std::stod(s.substr(0, c)), std::stod(s.substr(c + 1))});
            p = q + 1;
        }
    }
    Session session(hy);
    session.dir = direction;
    std::vector<double> times, flush_times;
    uint64_t all = 1469598103934665603ull;
    for (auto [a, b] : spans) {
        const size_t i0 = std::min(audio.size(), size_t(a * rate)), i1 = std::min(audio.size(), size_t(b * rate));
        const size_t piece = stream_ms > 0 ? std::max<size_t>(1, size_t(stream_ms * rate / 1000)) : std::max<size_t>(1, i1 - i0);
        const double s0 = now_s();
        for (size_t p = i0; p < i1; p += piece) session.append(audio.data() + p, std::min(piece, i1 - p), rate);
        const double f0 = now_s();
        Session::Answer answer;
        session.flush(answer);
        const double t = now_s();
        times.push_back((t - s0) * 1000);
        flush_times.push_back((t - f0) * 1000);
        const uint64_t h = text_hash(answer.text);
        all = (all ^ h) * 1099511628211ull;
        fprintf(stderr, "%7.1f-%7.1f  %5.0f ms (%5.0f after flush)  %d turns%s  %016llx%s%s\n", a, b, times.back(),
                flush_times.back(), answer.turns, answer.st ? " + ST" : "", (unsigned long long) h,
                opt.text ? "  | " : "", opt.text ? answer.text.c_str() : "");
    }
    std::sort(times.begin(), times.end());
    std::sort(flush_times.begin(), flush_times.end());
    if (!times.empty()) say("spans %zu, median %.0f ms (%.0f after flush), max %.0f ms, text %016llx", times.size(),
                            times[times.size() / 2], flush_times[flush_times.size() / 2], times.back(), (unsigned long long) all);
    return 0;
}
