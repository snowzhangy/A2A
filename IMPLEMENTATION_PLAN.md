# A2A Implementation Plan v2.1 (execution baseline)

Prepared 23 October 2026 from a review of `ARCHITECTURE_PLAN.md` (v1); final technical pass completed 24 October 2026.

v1 remains the architecture rationale; this document does not restate it. This document is the execution plan: frozen decisions, contracts, the first two weeks as tickets, the week-2 go/no-go gate, epics through week 8, risks, and compliance. Where this document and v1 disagree, this document wins until the item is re-decided in an ADR (`docs/adr/`).

Items marked **[VERIFY]** are unconfirmed facts. Verify alpha-critical items before enabling their adapters; explicitly deferred items belong to their stated later phase. A candidate's presence in the table does not qualify it for use.

Start with T0.1, T0.3, A1, and H1 below. Swift-first and the audio/inference process boundary are recorded in [ADR-0001](docs/adr/0001-language-boundary.md) and [ADR-0002](docs/adr/0002-process-topology.md). The verification evidence and remaining device checks are in [the final review](reports/planning/FINAL_REVIEW.md). This pass finalized the plan. Implementation status per ticket is tracked in [reports/week2/status.md](reports/week2/status.md); model benchmarking has not started.

---

## 1. Review summary: what changes from v1 and why

v1 is a sound architecture proposal. Its stage separation, bounded queue with fallback to original audio, per-speaker profile versioning, "synthesize only committed text", replay harness before UI, and gated phases are all correct and are kept. It is not yet executable: it names no meeting app, freezes no interfaces, has no trace format, assigns no work, and leaves several model and platform facts that recent sources contradict.

| # | v1 position | Change | Evidence |
| --- | --- | --- | --- |
| C1 | Median ≤2 s end-to-end is the launch target | Retain the ≤2 s product target. The week-2 feasibility gate allows ≤2.5 s, with any gap reported explicitly. Seed LiveInterpret 2.0 reports about 3 s average cloned-speech latency under its own evaluation; this is a research comparison, not a directly comparable bound or a claim about the strongest available system. | [Seed LiveInterpret 2.0](https://arxiv.org/abs/2507.17527) |
| C2 | C++ engine with C ABI shared across a SwiftUI and a WinUI shell, from day 1 | Select Swift-first for the Mac alpha, with an I/O-free core and golden traces for a later Windows port. Existing Swift ports reduce the initial work; streaming semantics, quality, and runtime compatibility still need native tests. | [speech-swift](https://github.com/soniqo/speech-swift), ADR-0001 |
| C3 | TranslateGemma 4B is the shared translator, used "with preceding context" | Use it as a closed-segment baseline. Evaluate a non-thinking Qwen instruct model for contextual translation. Version 0 commits complete source units and translates each once. Incremental target continuation is an optional experiment: assistant prefill alone does not establish prefix enforcement or semantic stability. | [TranslateGemma model card](https://huggingface.co/google/translategemma-4b-it), section 4.3 |
| C4 | Apple Translation is the macOS translator | Keep it as a local closed-segment baseline. Language assets may require downloading during setup. The installed SDK marks the basic `TranslationSession(installedSource:target:)` initializer available from macOS 26.0; its `preferredStrategy:` overload starts at 26.4. Runtime installation and zh→en behavior remain day-1 checks. | [TranslationSession](https://developer.apple.com/documentation/translation/translationsession), local SDK evidence in the final review |
| C5 | Not in v1 | Tencent HY-MT1.5 (1.8B/7B) supports context and terminology prompts and is strong on zh→en, but its license excludes the EU, UK, and South Korea and forbids using its outputs to improve other models. Benchmark only. It must not be a product dependency or a distillation teacher. | [HY-MT License.txt](https://github.com/Tencent-Hunyuan/HY-MT/blob/main/License.txt) |
| C6 | Qwen3-ASR is a challenger with "streaming via vLLM" | The documented upstream streaming path is vLLM-only, without timestamps or batching. Verify the Swift/MLX port separately. Use offline forced alignment as a timing reference with audited uncertainty, not infallible ground truth. | [Qwen3-ASR README](https://github.com/QwenLM/Qwen3-ASR) |
| C7 | Qwen3-TTS published latency | The 97 ms figure was measured on an A100 with FlashAttention-2. It is not a Mac number. Base-model cloning expects `ref_text` by default; we supply it from our own ASR of the reference clip. The README documents cross-lingual evaluation but no cross-lingual API. | [Qwen3-TTS README](https://github.com/QwenLM/Qwen3-TTS) |
| C8 | CosyVoice repository and checkpoint | Follow the upstream repository redirect and pin its resolved commit. Compare `Fun-CosyVoice3-0.5B-2512` and the matching Swift conversion; record both upstream and converted-weight licenses. Upstream bi-streaming does not automatically establish the port's behavior. | [CosyVoice](https://github.com/FunAudioLLM/CosyVoice) |
| C9 | Stage budgets assume stages overlap | ASR, MT, and TTS all compete for one unified-memory GPU on Apple silicon, so per-stage benchmarks run alone overstate speed. All speed gates are measured with every stage running at once, while a real meeting app is active. This is the most likely place the budget fails. | Engineering judgment |
| C10 | Windows runtime table | Add constraints: VitisAI (AMD NPU) has both a minimum and a **maximum** supported driver version, so qualification needs a pinned driver window. MIGraphX "is not supported for GenAI scenarios today". DirectML is "legacy". NvTensorRtRtx needs RTX 30xx or newer. All on-demand providers need Windows 11 24H2 (build 26100). Process loopback needs build 20348 or later. | [Windows ML execution providers](https://learn.microsoft.com/en-us/windows/ai/new-windows-ml/supported-execution-providers), [Application loopback sample](https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/) |
| C11 | Mute through the tap lifecycle; switch between original and translated audio | Simplification: mute the source app once at session start, and render **everything the user hears** through our own mixer, including the original, which is re-rendered from the captured stream. Translate, monitor, and fall back become gain crossfades inside our mixer, so the tap mute state never toggles mid-session. Cost: the original audio gains our capture→output delay (target ≤60 ms). | Engineering judgment |
| C12 | Shared-memory audio buffers to an inference worker | Start with bounded XPC messages: 16 kHz mono float input is 64 KB/s and 24 kHz mono float output is 96 KB/s. Measure 20–40 ms batching under load, cap queued messages, and acknowledge consumed sequence numbers. References originate from the full-band capture branch. Keep rings within each process; add shared memory if measured overhead warrants it. | Engineering hypothesis, ADR-0002 |
| C13 | Not in v1 | A compliance workstream (section 11). It blocks distribution, not development. | EU AI Act Art. 50 |
| C14 | Not in v1 | End-to-end simultaneous S2ST models are tracked as research references only. Hibiki-Zero has no Mandarin input and an NC license; Seamless has an NC license; Seed LiveInterpret is closed. | [Hibiki-Zero, arXiv 2602.11072](https://arxiv.org/abs/2602.11072) |

---

## 2. Frozen decisions for the macOS alpha

Change these only through an ADR.

| ID | Decision | Default | Owner to confirm by |
| --- | --- | --- | --- |
| D1 | Language pair | Mandarin (zh-CN, Simplified) → English (en-US) | Fixed |
| D2 | Live source app | Zoom desktop (`us.zoom.xos`). Alternate: Chrome + Google Meet, which means resolving Chrome's audio-service helper process; schedule that for week 7. The harness uses our own `FixturePlayer` app as the tap target, which keeps tests independent of Zoom. | Product, day 2 |
| D3 | Output device | Wired headphones for meeting acceptance tests. Built-in output is a playback test; acoustic leakage into Zoom's microphone needs separate qualification. Bluetooth is measured but not gated in alpha. | Fixed |
| D4 | OS | macOS 26.0 minimum, Apple silicon. Guard optional 26.4+ APIs. macOS 15 is out of scope for alpha; macOS 26 compatibility still needs a real test machine. | Fixed |
| D5 | Reference hardware | One Apple silicon Mac with 24–32 GB unified memory (M3 Pro, M4 Pro, or newer) per engineer, plus one 16 GB Mac as a low-memory probe. One dedicated Mac mini or Studio as the self-hosted CI and benchmark runner. On-demand cloud CUDA instance access for PyTorch parity runs. | Eng lead, week 1 |
| D6 | Distribution for alpha | Developer ID signed, notarized, hardened runtime, not sandboxed. Mac App Store sandbox compatibility with process taps is **[VERIFY]**, deferred. | Fixed |
| D7 | Speakers in alpha | One active speaker. A detected speaker change without a ready profile falls back to the original voice plus captions. | Fixed |
| D8 | Network | Inference runs locally after explicit model/language-asset setup. Only the setup path initiates downloads; disable runtime downloads and telemetry in adapters. Prove offline behavior with networking disabled. Framework linkage alone cannot enforce this. | Fixed |
| D9 | Voice profiles | In memory, session-scoped, never intentionally persisted by the product. No audio, transcripts, or reference material in default logs/crash attachments. Research fixtures and generated evaluation audio follow their own documented retention rules. Save/load is post-alpha. | Fixed |

---

## 3. Architecture decisions and week-1 preflights

### ADR-0001: Swift-first Mac alpha (selected)

| Option | Summary | Pros | Cons |
| --- | --- | --- | --- |
| **A. Swift-first (selected)** | Swift application and orchestration; model adapters for Swift ports, Apple frameworks, and sherpa-onnx. `A2ACore` contains I/O-free types and policies with injected clocks and golden traces. Use a narrow C/C++ shim for rings/callback operations if required to meet real-time constraints. | Uses native Mac integrations and limits early binding work. | Windows needs a native port of policies and separate runtime/audio adapters. Golden traces establish behavior; they do not eliminate that work. |
| B. v1 as written | C++ core with C ABI; Swift shell; models through C++ (MLX C++ API, ONNX Runtime). | Shares the core with Windows from day 1. | Weeks of binding work before the first demo. speech-swift becomes unusable or needs re-wrapping. Highest schedule risk. |
| C. Python sidecar | Swift shell; Python inference process (PyTorch MPS or mlx-audio). | Fastest research iteration. | Hard to ship (bundle size, notarization of hundreds of dylibs, cold start). Throwaway for product. |

Decision: **A** for the product path, with **C kept for research and the harness only**. Pin and compile the actual model dependency subset in week 1. Revisit the shared-core language in week 9 using measured porting needs; record a superseding ADR if the boundary changes.

### ADR-0002: Audio host and isolated inference (selected; preflight in week 1)

```
A2A.app (Swift, SwiftUI)
 ├─ Audio engine (in-process, real-time threads)
 │   ├─ Core Audio process tap + private aggregate device  (capture, mute)
 │   ├─ SPSC ring buffers (lock-free, preallocated)
 │   └─ Output mixer: original bus (re-rendered) + translated bus → device
 ├─ Session controller (A2ACore state machine, non-RT thread)
 └─ XPC connection ─────────────► A2AInference.xpc (separate process)
                                    VAD → speaker → ASR → commit → MT → TTS
                                    models resident, warmed at session arm
```

Rationale: isolate inference failures from the host's audio path. The host owns device control, epochs, the original bus, and final playout. Inference workers never render audio. On XPC failure, the host requests fallback on its control thread and begins a 20–50 ms gain ramp on the next render cycle. Measure detection-to-original-audible recovery separately; initial target ≤250 ms. Process isolation does not protect against system-wide memory pressure, so apply a resident-memory cap.

Weeks 1–2 pipeline spikes may run in-process as CLIs, but week 1 includes a minimal bundled XPC/MLX probe. **[VERIFY]** model loading, Metal resource availability, signing/entitlements, cancellation, and worker restart in that topology. If bundled XPC cannot run the selected model, use a supervised bundled helper with the same message contract and record the change before week 3. Shared memory remains a profiling decision.

Messages carry a protocol version, session epoch, playout generation, sequence number, format, and bounded frame count. Validate lengths and voice-reference sizes, cap in-flight batches, and reject stale responses. Restrict connections to the bundled host/service identity using platform-supported code-signing checks. Cancellation must release model streams and pending message payloads.

### Real-time rules (review checklist to live in `AGENTS.md`)

The capture IOProc copies into a preallocated ring. The output callback drains buffers and performs bounded, preallocated mixing, gain ramps, and resampling. Each callback can append a fixed-size timing record to its own SPSC trace ring. Neither callback allocates, takes locks, formats logs, calls XPC/Swift async, or touches UI. Avoid ARC traffic, Objective-C bridging, and destruction of reference-counted objects on these threads; use explicit buffer lifetimes or a C/C++ shim where necessary. JSON serialization and other work run on ordinary threads.

---

## 4. Contracts (build the harness and runtime against these in parallel)

### 4.1 Time base

- **Source time** is a canonical 48 kHz sample index since the current epoch starts (`src_sample`). Native capture may use a different rate. Preserve native sample/host-time pairs and map them through the resampler, including its group delay. Every stage carries canonical half-open source spans forward.
- **Event time** is `t_ns`, host monotonic time (`mach_absolute_time` converted to ns), recorded when the event occurs.
- **Output time** is the output sample index at the device rate since session start (`out_sample`).
- Capture/output timing records pair sample indices with valid `AudioTimeStamp.mHostTime` converted to ns. Record device rate, timestamp flags, latency/safety-offset metadata, and resampler delay. Invalid or discontinuous timestamps trigger recovery rather than fabricated latency data.
- `play.scheduled` is a render scheduling event. `play.start` estimates hardware presentation from the relevant timestamp/device metadata; a callback invocation time is insufficient. Validate this mapping with the physical loopback test and report its uncertainty. Do not double-count latency already included in the hardware timestamp.
- Clock drift between capture and output devices is measured as the ratio of sample-index deltas to `t_ns` deltas, logged in `clock.drift`, and corrected by adaptive resampling in the mixer (week 4).

### 4.2 Stage interfaces (Swift sketch; the semantics are the contract)

```swift
struct SourceSpan { let start: Int64; let end: Int64 }          // src_sample, half-open

protocol VoiceActivityDetector {
    func process(_ frame: AudioFrame16k) -> [VADEvent]           // .speechStart(src), .speechEnd(src)
}

protocol SpeakerTracker {
    func process(_ frame: AudioFrame16k, vad: [VADEvent]) -> [SpeakerEvent]  // .assign(spk, span), .change(span)
}

protocol StreamingRecognizer {
    func accept(_ frame: AudioFrame16k)
    func hypothesis() -> ASRHypothesis   // tokens with optional SourceSpan each, isFinal flag
    func reset()
}

protocol IncrementalTranslator {
    /// Optional experimental continuation capability, verified by adapter contract tests.
    var supportsPrefix: Bool { get }
    func translate(_ req: TranslationRequest) async throws -> TranslationCandidate
}
struct TranslationRequest {
    let unitID: UnitID
    let context: [CommittedPair]         // last N committed source/target pairs
    let glossary: [String: String]
    let uncommittedSource: String
    let committedTargetPrefix: String    // prefix within THIS unit; empty for v0 closed-unit mode
}

protocol VoiceSynthesizer {
    func prepare(_ profile: VoiceProfile) async throws -> PreparedVoice   // cache conditioning once
    func stream(_ text: AsyncStream<TextChunk>, voice: PreparedVoice) -> AsyncThrowingStream<PCMChunk, Error>
}

struct VoiceProfile {
    let speakerID: SpeakerID
    let version: Int
    let referencePCM: [Float]            // 3–10 s voiced, resampled from the 48 kHz capture branch
                                         // to the model's native rate (never upsampled from the 16 kHz ASR branch)
    let referenceText: String            // from our own ASR of the reference clip
    let sampleRate: Int
    let channelCount: Int
    let quality: ReferenceQuality        // duration, SNR estimate, clipping, overlap flag
}
```

Every `PCMChunk` carries `unitID`, `speakerID`, `profileVersion`, `sessionEpoch`, `playoutGeneration`, its model format, and the translated `SourceSpan`. Accept a chunk only when epoch AND generation equal the host's current values and the unit has not been canceled. Increment the epoch on source/device discontinuity or restart; increment the playout generation on fallback or speaker handoff. Bound adapter queues and PCM buffers. ASR/MT can revise tentative results; canceled or already played units are immutable.

Synthesis capabilities distinguish **audio-output streaming**, **incremental text input**, and **continuation across units**. Verify each independently. An audio-output-only adapter accepts a complete committed unit and emits PCM chunks; v0 supports that path. Join units with short ramps and evaluate prosody. A model promising streaming in its paper does not establish these runtime capabilities.

### 4.3 Commit policy v0 (the starting point the simulator tunes)

1. **Source stability:** compare two consecutive hypotheses for the same utterance after consistent text normalization. Commit only new material beyond a monotonic source frontier. A VAD end asks the recognizer to finalize; it does not make its last partial final. Reset comparison state at utterance/epoch boundaries. Hold unfinished number/date expressions, negation, and incomplete phrases for more context.
2. **Closed units for v0:** close a stable source unit at a meaningful clause boundary or a finalized short utterance. The 900 ms window requests reconsideration; it must not force a split in an unsafe phrase. A 250 ms pause or a conjunction is evidence to evaluate, not sufficient proof of a complete clause. Track extra semantic waiting in the trace and allow it to miss the latency target rather than change meaning.
3. **Translate once:** translate the closed unit, optionally supplying preceding committed source/target pairs and a glossary when the adapter supports them. Request only the new unit's target text. The prefix is empty in this mode. Apple Translation and TranslateGemma remain valid segment baselines. Validate completion, language, duplication, and model errors before scheduling; cap generation length and cancel stale work.
4. **Synthesize committed text:** the baseline sends a complete translated unit to an audio-streaming adapter. Use incremental text input only after its behavior is verified. Empty/one-word translations and short finalized acknowledgments must not wait indefinitely for a minimum word count. Retain conditioning per speaker and measure boundary naturalness.
5. **Optional target continuation experiment:** within a bounded open unit, compare successive target candidates at the same source frontier. Require the exact previously committed token prefix and emit only the suffix; reject incompatible results. Any advertised prefix capability needs decoder/adapter tests. Prompt instructions or assistant prefill alone are insufficient evidence. Record irreversible errors separately.
6. **Captions** distinguish tentative and committed text. Spoken text is immutable; source/target units cannot be emitted twice.

Compare closed clauses, optional continuation with LocalAgreement-2, wait-k (k = 3, 5 source characters as exploratory settings), and sentence-end translation as a higher-context reference. Report the quality/lag tradeoff; sentence-end output is not assumed to be a quality ceiling.

### 4.4 Session state machine

```
Idle ──arm──► Arming (permissions, models loaded, warm-up inference)
Arming ──ok──► Listening (original bus 1.0, captions on)
Listening ──speech from speaker without a profile──► AcquiringVoice(spk)   [user hears original + captions]
AcquiringVoice ──profile ready + fresh unit PCM buffered + safe handoff──► Translating(spk)
Translating ──debt > 3.0 s / source lag > 4.0 s / underrun / service lost──► Degraded
Degraded ──healthy worker + fresh profile/unit + PCM ready + safe boundary──► Translating
Translating ──speaker change, no ready profile──► AcquiringVoice(new spk)
any ──stop / error──► Idle (tap destroyed; the app's own sound is expected to return, verified by A3)
```

`playout debt` is scheduled-but-unplayed translated audio plus estimated duration for committed-but-unsynthesized text. Also track the age of the oldest uncovered source unit: blocked recognition/translation can produce large lag with a small audio queue. The thresholds are initial parameters, frozen before each gate run.

Warm models before taking over the source. Listening/acquisition continue original playback. A profile-ready event alone cannot mute the original bus: require playable PCM (initial prebuffer 80–120 ms, counted in latency), a current generation, and a documented handoff boundary. Use gain ramps; record any source interval repeated or left untranslated as the listener switches between live original and delayed translation. Startup audio capture must already be healthy before the tap assumes playback control.

On degradation, ramp back to the live original, invalidate pending generations, cancel synthesis, and record every abandoned unit. Captions continue only while their recognizer is healthy. An inference crash shows translation/captions unavailable rather than stale text. A flushed queue alone cannot trigger recovery: require a healthy worker, a fresh unit produced after fallback, and at least a two-second stable interval before retry. Resume at a new safe boundary; do not replay stale backlog. Unknown speakers and suspected overlap return to original/captions until an appropriate profile and unit are ready.

### 4.5 Trace format v0 (`harness/schema/trace.schema.json`)

JSON Lines, one event per line. Separate SPSC rings serve capture and render callbacks; the non-RT logger drains and serializes them. Other producers use bounded non-RT queues. Count dropped trace records and invalidate affected gate measurements. Text-bearing examples below are research-mode records; the product's default diagnostics omit text and audio.

```json
{"v":0,"t_ns":3100000000,"session":"a1f3","epoch":1,"gen":1,"ev":"asr.partial","spk":"S1","span":[96000,141120],"text":"我们下周开会","stable":2}
{"v":0,"t_ns":3400000000,"session":"a1f3","epoch":1,"gen":1,"ev":"commit.target","unit":"u17","spk":"S1","prof":2,"span":[96000,141120],"text":"We will meet next week."}
{"v":0,"t_ns":3800000000,"session":"a1f3","epoch":1,"gen":1,"ev":"play.start","unit":"u17","out_sample":182400}
```

Required event types: `session.start`, `buf.capture`, `buf.output`, `state.change`, `vad.start`, `vad.end`, `spk.assign`, `spk.change`, `profile.ready`, `asr.partial`, `asr.final`, `commit.source`, `mt.request`, `mt.result`, `commit.target`, `tts.request`, `tts.first_chunk`, `tts.chunk`, `play.scheduled`, `play.start`, `play.end`, `unit.cancel`, `source.disposition`, `queue.depth` (every 100 ms, including oldest-source age), `clock.drift`, `resource.sample` (RSS and available runtime memory counters every 1 s), `fallback.enter`, `fallback.exit`, `trace.drop`, `error`.

Each report records device/chip, RAM, OS/build, SDK/toolchain, power mode, audio route, model and runtime hashes, quantization, decoding settings, warm/cold status, random seeds, and trace schema version. Distinguish the host, worker, and any system inference service in memory measurements; unified GPU memory must not be counted twice. Keep an immutable raw run and generate all metrics from that run.

A converter `harness/trace2perfetto.py` emits Chrome trace JSON for visual inspection in Perfetto.

### 4.6 Metrics (computed by the harness from the trace and output audio)

| Metric | Definition | Use |
| --- | --- | --- |
| `lag_onset(u)` | Estimated hardware presentation time of the first translated audio for unit `u` minus the independently aligned onset of its first source character. Map samples to monotonic time using section 4.1 and report alignment/presentation uncertainty. | **Gate metric** (v1's phrase-onset delay). Report median/p95, unit lengths, and source-character-weighted summaries so segmentation choices remain visible. |
| `lag_live(u)` | Same calculation using the live `span.start(u)` | Diagnostic only; compare approximate live timing with the reference alignment. |
| `lag_end(u)` | `play.start(u)` time minus independently aligned offset of the last source character in `u` | Diagnostic only; can be negative for incremental translation. |
| Translation coverage | Unique aligned reference characters assigned to translated units that actually finished playing ÷ reference characters in the predefined warm evaluation interval. Count canceled, unfinished, unmatched, and never-committed content as uncovered; never count the same character twice. | Gate for delivery coverage; semantic completeness still requires the omission check. |
| Source disposition | Separate counts for translated, original-only, uncertain/overlap, repeated during handoff, and inaudible source spans; report acquisition and fallback intervals explicitly | Prevents original fallback or a truncated synthesized unit from being counted as successful translation. |
| Omissions inside spoken units | LLM critical-error judge on each (source unit, spoken target) pair | Reported separately; humans decide at gates. |
| Playout debt | from `queue.depth` | Must not trend upward over 60 minutes. |
| ASR CER | against reference transcripts | Per ASR candidate. |
| MT quality | COMET (wmt22-comet-da) against references, plus an LLM-judge pass flagging critical errors (numbers, negation, named entities) | Automated nightly signal. Humans decide at gates. |
| TTS intelligibility | WER of Whisper-large-v3 transcription of our English output against the committed target text | Catches mumbling and skipped words. |
| Speaker similarity | cosine similarity between source reference and output using an embedding model **different from** the one the pipeline uses for tracking (e.g. a WavLM-based verifier) | Avoids grading ourselves with our own model. Thresholds are calibrated against human judgments, never universal. |
| Naturalness | UTMOS as a proxy; human MOS at gates | |

Reference timing comes from an offline aligner against human-checked transcripts, independently of the live recognizer. Qualify Qwen3-ForcedAligner on Mandarin and audit a fixed sample of boundaries manually; use a Mandarin alignment alternative if necessary. Save alignment uncertainty and normalization rules. Edit-distance mapping links source units to reference characters; inspect repeated phrases and unmatched regions rather than silently choosing favorable matches. Fix the warm interval before the run, starting after model/profile readiness; all later fallback intervals remain in its denominator. Also publish the complete cold-start/acquisition report.

When live ASR lacks timestamps, retain conservative spans bounded by speech onset and the latest accepted audio frame. Mark timing as estimated. An unfinished VAD segment has no known future endpoint; interpolation is an optional after-finalization diagnostic. Choose voice-reference frames by capture timestamps and speaker assignment, never by estimated character timing. Gate latency uses the independent fixture alignment.

**Speaker-identity listening protocol ("Match-5"), used everywhere in this plan:** the listener hears synthesized English and five Mandarin source clips, then selects the speaker. Use four licensed/consented distractors with comparable pitch and vocal presentation; the distractor pool may extend beyond the eight synthesis-study speakers. Source choices use the same scripted content across candidates, and that content differs from both the synthesis prompt and translated test sentence, so listeners cannot solve the task by meaning or recording artifacts. Randomize order and hide model labels. Chance is 20%. Report per-speaker scores and a confidence interval clustered by speaker; thresholds are provisional at eight speakers. Week 2 uses ≥70%; release uses the v1 ≥90% target with held-out speakers.

---

## 5. Model shortlist (pinned in `models/manifest.yaml`)

Every enabled manifest entry records upstream and converted-weight repos, exact revision hashes, file SHA-256s, tokenizer/config hashes, licenses, evidence/verification date, size, native sample format, runtime path, streaming capabilities, and expected compute backend. Distinguish declared candidate, verified/downloadable, and qualified/shippable status. Convert locally from verified upstream weights if a third-party artifact lacks provenance.

| Stage | Candidate | License | Mac runtime path | Streaming | Open question |
| --- | --- | --- | --- | --- | --- |
| VAD | Silero VAD | MIT | speech-swift (Core ML/MLX) or sherpa-onnx | Yes | False triggers on music |
| Speaker embedding | CAM++ (3D-Speaker) | Apache-2.0 **[VERIFY checkpoint license]** | speech-swift or sherpa-onnx | Per window | Change-detection threshold on meeting audio |
| ASR 1 | Apple SpeechTranscriber (SpeechAnalyzer) | System | Apple framework | Yes (volatile + final results, time ranges) | **[VERIFY]** zh-CN appears in `supportedLocales` on the reference Mac (day 1) |
| ASR 2 | sherpa-onnx streaming zipformer, bilingual zh-en | Runtime Apache-2.0; **[VERIFY]** selected checkpoint separately | sherpa-onnx C API | Verify token timing semantics in adapter | Accuracy on spontaneous meeting speech |
| ASR 3 | Qwen3-ASR-0.6B | Apache-2.0 | speech-swift (MLX) | Upstream: vLLM only, no timestamps. **[VERIFY]** whether the MLX port streams. | Accuracy gain versus GPU contention |
| MT 1 (primary candidate) | Qwen3-4B-Instruct-2507, 4-bit MLX conversion | Upstream Apache-2.0; verify converted artifact provenance | MLX (mlx-swift-lm) | Closed units first; continuation capability tested separately | Clause adequacy and complete-unit latency while TTS runs |
| MT 2 (baseline) | TranslateGemma 4B, 4-bit | Gemma Terms of Use (use restrictions must be passed on) | MLX | Single segment, no context | **[VERIFY]** zh is in the template's accepted codes |
| MT 3 (baseline) | Apple Translation | System | `TranslationSession` | Per string | Latency per clause; first-run language download flow |
| MT ref (disabled) | HY-MT1.5-1.8B / 7B | Tencent HY Community; territorial and output-use restrictions | Optional research only | — | Excluded from default downloads, CI, release, and training. Any benchmark use needs a recorded license/territory determination. |
| TTS 1 | Fun-CosyVoice3-0.5B-2512 | Apache-2.0 | speech-swift (MLX) | Text-in and audio-out upstream. **[VERIFY]** text-in streaming in the MLX port. | Cross-lingual identity; first audio under load |
| TTS 2 | Qwen3-TTS-12Hz-0.6B-Base (1.7B-Base as quality reference) | Upstream Apache-2.0; verify conversion | speech-swift or mlx-audio (MLX) | Verify audio-out and any incremental-text support separately | Mandarin reference → English identity, pronunciation, and `ref_text` quality |
| Diarization (week 7+) | Streaming Sortformer 4-speaker | **[VERIFY]**: v2 reportedly CC-BY-4.0, v2.1 NVIDIA Open Model License | speech-swift | Yes (480 ms updates) | Phase-2 only |

Parity check: for each speech-swift model used, run the same 20 clips through the upstream PyTorch reference on a CUDA box or with CPU fallback, and compare outputs (CER, speaker similarity, MOS proxy). A third-party port that loses quality is a finding, not a reason to drop the model.

Pin speech-swift and transitive dependencies locally; retain its notices and cache the selected source revision. Creating a hosted fork is optional and unnecessary for day-1 progress. Test the actual product subset rather than linking every available model. Maintain fallback adapters because this third-party integration is on the critical path.

---

## 6. Repository layout (created by T0.1)

```
A2A/
  ARCHITECTURE_PLAN.md          # v1 rationale
  IMPLEMENTATION_PLAN.md        # this file
  AGENTS.md                     # canonical build/test commands and RT/data rules
  CLAUDE.md                     # short pointer to AGENTS.md for that client
  docs/adr/                     # 0001-language-boundary.md, 0002-process-topology.md, ...
  apple/
    Package.swift               # Swift package: libraries, CLIs, and deterministic tests
    A2A.xcodeproj/              # signed app/FixturePlayer/XPC targets when bundling begins
    Sources/
      A2ACore/                  # pure: types, commit policy, scheduler, state machine; no I/O
      A2AAudio/                 # process taps, aggregate device, rings, mixer, playout
      A2AModels/                # adapters for SpeechAnalyzer, sherpa-onnx, speech-swift, MLX LM, Apple Translation
      A2ATrace/                 # lock-free trace logger
    Tools/
      a2a-tapctl/               # CLI: list processes, capture, mute, replay, lifecycle tests
      a2a-pipeline/             # CLI: WAV in → translated WAV + trace out (offline and real-time pacing)
    Apps/
      FixturePlayer/            # tiny app that plays fixtures; the harness tap target
      A2A/                      # SwiftUI app (from week 4)
      A2AInference/             # XPC service (from week 3)
    Tests/
      A2ACoreTests/             # golden-trace tests for the commit policy and state machine
  research/                     # Python (uv): model benches, PyTorch parity runs, notebooks
  harness/                      # Python (uv): replay runner, metrics, reports
    schema/trace.schema.json
    trace2perfetto.py
  models/manifest.yaml
  scripts/fetch_models.py
  fixtures/                     # manifests; tiny licensed/synthetic contract fixtures may be in git
  .local/fixtures/              # ignored local cache for consented/licensed evaluation audio
  reports/                      # weekly benchmark and gate reports (markdown + CSV)
```

An access-controlled bucket is optional infrastructure. Local fixture manifests and a `.gitignore`-protected cache are sufficient to start. Reports in git contain aggregate metrics and fixture IDs; raw participant audio, transcripts, voice prompts, and full text traces stay in the restricted research store. Use stable bundle IDs/signatures for audio tools, since permission identity is part of reproducibility. Actual model/audio-device tests run on a provisioned interactive Mac with capture permission; hosted CI runs deterministic tests and compilation.

---

## 7. Team and roles

The plan assumes three engineers, as v1 does.

- **AUD**: native audio and macOS (taps, devices, mixer, lifecycle, XPC, packaging).
- **ML**: speech and model systems (benchmarks, model adapters, voice profiles, commit policy, quantization).
- **INT**: integration and test (repo, CI, harness, metrics, commit-policy simulator, corpus, UI from week 4).

AUD/ML/INT identify workstreams, not a requirement to have three people or to spawn coding agents. With one implementer: T0.1 → minimal H1 → A1/A2 → A3 → first model pair/M1 → M2/M4 → M3/E0. Keep the dependency order and extend elapsed time. The two-week estimate assumes the stated staffing and available devices/listeners.

---

## 8. Weeks 1–2: feasibility tickets

Each ticket produces a reproducible artifact in `reports/week2/`. Dependencies: T0.1 unblocks the harness/audio scaffold; T0.3 resolves adapter feasibility; M1 and H1 unblock M4; A1/A2/A3 plus M2/M3/M4 feed E0 and the gate. Keep restricted raw evidence outside git. Human listener scheduling and held-out evaluation can run alongside technical work; unavailable listeners make identity evidence pending, not automatically passed.

### Day 1–2 (everyone)

**T0.1 Repo bootstrap (INT).** Create the minimal section-6 package layout, `uv` harness project, `AGENTS.md`, and `scripts/check.sh`. Capture toolchain/SDK versions. Begin with dependency-light core, trace schema, and fixture replay before adding heavyweight model packages. Configure hosted compile/unit checks when a repository/runner is available; local progress does not depend on creating a remote repository.
DoD: `swift build --package-path apple`, `swift test --package-path apple`, and `uv run --project harness pytest harness/tests` run through the check script. Include meaningful initial contract checks for stale-generation rejection and trace/fixture validation; an empty test suite is not a pass. Provision the signed FixturePlayer and interactive hardware runner separately. The current checkout has Command Line Tools selected; full Xcode/app packaging and signing availability remain preflight items.

**T0.2 Model manifest and fetch (ML).** Declare the shortlist, then enable only the first ASR/MT/TTS/VAD combination and evaluation dependencies needed for M1. Pin revisions, checksums, and license evidence. `scripts/fetch_models.py` is resumable, explicit about selected models, and verifies hashes before atomic promotion into the model cache. Runtime auto-downloads are disabled.
DoD: a clean cache reproduces the selected combination; corrupt/incomplete artifacts are rejected. Every enabled row records provenance, license evidence, verifier, and date. Disabled/restricted candidates are never fetched automatically; system language assets are tracked separately from bundled weights.

**T0.3 Day-1/2 preflights (ML + AUD).** Start these checks immediately, record pending work explicitly, and write evidence in `reports/week2/verifications.md`. Complete each critical check before its adapter enters the live path:
- `SpeechTranscriber.supportedLocales` includes a zh-CN equivalent on the reference Mac.
- `TranslationSession(installedSource:target:)` runtime behavior for installed/missing zh→en assets; basic initializer availability is already confirmed as 26.0 in the local SDK.
- Runtime tap/mute lifecycle; SDK cases `CATapUnmuted`, `CATapMuted`, and `CATapMutedWhenTapped` are already confirmed.
- TranslateGemma accepts `zh` as `source_lang_code`.
- Selected speech-swift revision: distinguish genuine streaming ASR, repeated-window recognition, audio-output streaming, and incremental text input using test clips. Record unsupported capabilities and use closed-unit adapters where needed.
- Minimal MLX load/inference and cancellation inside a signed bundled XPC service; helper fallback per ADR-0002.
- Native capture rate/channel layouts, device presentation timestamps, available signing/toolchain, reference hardware, and actual ANE/GPU placement. System on-device inference is not proof of ANE use.

**T0.4 Corpus and consent kickoff (INT, longest lead time; start day 1).** Draft the consent form, covering recording, voice cloning for internal evaluation, retention, and withdrawal, and have it reviewed. Write a recording protocol: scripted sentences with numbers, dates, negation, and names, plus 5 minutes of spontaneous meeting-style speech per speaker, recorded both clean and through a real Zoom call. Book the first 8 speakers for week 2 and the remaining 12 by week 5. In parallel, verify the licenses of public sets for internal evaluation: AliMeeting, AISHELL-3, the CoVoST 2 zh→en split, BSTC. Record which are usable for evaluation only and which for nothing.
DoD: consent form approved; 8 speakers scheduled; license table committed.

### Track AUD

**A1 Process-tap capture CLI.** `a2a-tapctl list` prints audio-producing processes and their IDs. `capture --bundle <fixture-bundle-id> --out x.wav` first targets FixturePlayer, then Zoom. Resolve helper audio processes through audited app ownership/path/process relationships; helpers need not share the main bundle ID. Exclude our processes, refresh membership on relaunch, and reject ambiguous selections. IOProc → SPSC ring → writer thread keeps file writes off real-time threads.
DoD: waveform fidelity test against a known FixturePlayer file: after alignment, correlation >0.99, sample/frame accounting, and no dropouts over 30 minutes. Report capture rate, source membership, timestamps, and latency uncertainty.

**A2 Mute and re-render path.** Add tap mute (mute-when-tapped or the equivalent case from T0.3), and re-render captured audio to the default output through our mixer with a configurable delay (0–3 s) and a gain per bus.
DoD: no audible double playback. Capture→output added latency is measured with a loopback cable or a click-track test; target ≤60 ms. Gain crossfade without clicks is verified.

**A3 Lifecycle tests.** On FixturePlayer, automate 50 stop/start and host/worker crash cycles, including `kill -9` while muted, source relaunch, and ring starvation. Separately record at least five supervised output-device/sleep-wake cycles and permission denied/revoked/regranted cases. Hardware and TCC tests belong on the interactive runner; avoid pretending they are headless hosted-CI checks.
DoD: a results table with injected-fault times and original-audio recovery. **Any stranded mute blocks playback takeover** until fixed. Verify what the OS does when the host dies; a watchdog running in that same dead host cannot restore sound.

### Track ML

**M1 Isolated model benchmarks.** For every ASR, MT, and TTS candidate, run the 20-clip dev set (public data plus the first internal recordings) on the reference Mac. Measure quality (section 4.6), first-token or first-audio latency, real-time factor, and peak memory. In weeks 1–2, run the PyTorch parity check only for the two TTS models and the leading ASR; the rest wait until a model reaches the critical path.
DoD: `reports/week2/models.csv` and a one-page summary naming the top two per stage.

**M2 Voice reference study.** For both TTS models, screen 8 speakers × 3 reference lengths (3/6/10 s) × 3 disjoint target sentences. Then evaluate the leading model's 6 s condition on 10 sentences per speaker with three blinded listeners. Use the Match-5 distractor/content controls in section 4.6 and an independent verifier. If internal recordings slip, use a license-cleared public set and mark the identity result provisional until rerun on the intended meeting population. Do not reuse evaluation utterances as clone prompts.
DoD: similarity and Match-5 table per model and reference length. Recommend a reference length and a reference-selection heuristic (minimum segment length, SNR estimate, clipping, overlap rejection).

**M3 Concurrency benchmark.** Run the selected ASR, MT, and TTS **together**, consuming recorded input at real-time pace, with a controlled internal Zoom call active on the same machine. Measure latency inflation, GPU contention, memory, and backlog after warm-up. Separately run the fixture pipeline with networking disabled to prove local operation; a live Zoom load test necessarily uses its own network connection.
DoD: distributions under load, including units/minute, full TTS real-time factor (generation wall time ÷ generated audio duration), first audio, complete-unit latency, queue age, and observed dropouts. This result decides the week-2 speed gate.

### Track INT

**M4 Commit-policy simulator (INT; ML supplies logged ASR partials from M1 and reviews the policy).** Replay logged hypotheses at their original event times through `A2ACore` and the MT adapters. Track committed units, semantic waiting, caption churn, and source disposition. Simulated commitment timing is a diagnostic; final `lag_onset` requires E0's actual synthesis/playout events.
DoD: golden traces for duplicate hypotheses, finalization after VAD, short acknowledgments, negation/numbers, canceled units, and speaker handoffs, plus comparison of closed clauses, optional continuation, wait-k, and sentence-end.

**H1 Replay harness.** `harness/run.py --fixture clip.yaml --pipeline a2a-pipeline --mode paced` feeds only elapsed audio frames at real-time pace. An explicit `--mode tap` plays through FixturePlayer and native capture. Future samples remain inaccessible to the streaming pipeline. Collect trace/output plus separate compute-only benchmarks.
DoD: paced deterministic tests run in ordinary CI; tap and Metal/model tests run on the provisioned Mac with its required login, devices, signatures, and permissions. One command plus pinned artifacts reproduces each report; hardware load results include an uncertainty envelope rather than identical timing promises.

**H2 Metrics package.** CER, COMET, back-transcription WER, independent speaker similarity, UTMOS, and an optional LLM critical-error judge. The judge runs only on corpus audio whose consent covers sending transcripts to an external API. Otherwise, use a local model.
DoD: unit tests on known-answer samples.

### End of week 2: integration (everyone)

**E0 End-to-end streaming demo.** Mandatory: ten minutes of paced Mandarin input, a prepared 6 s voice profile, English PCM emitted while source ingestion continues, and a complete trace with no future-audio access, entirely local. Also run a cold/acquisition case. Export a WAV for listening, but whole-file offline throughput cannot pass this gate. Native capture/replay is independently proven by A1–A3; combining the model pipeline with tap input is a week-2 stretch and E1's first requirement.

---

## 9. Week-2 go/no-go gate

Held at the end of week 2. The thresholds below were proposed before measurement; the ML lead may adjust them in writing **before** week 2 starts, never after the results are in.

| Area | GO if | Source |
| --- | --- | --- |
| Audio lifecycle | Zero stranded mutes in A3; added re-render latency ≤60 ms; 30-minute capture without dropouts | A1–A3 |
| Voice identity | At least one TTS model with a 6 s reference: Match-5 accuracy ≥70% across the 8 speakers (chance 20%), and no speaker below 50% | M2 |
| Speed under load | All stages concurrent and Zoom active: sustained input/output with bounded backlog and no unaccounted starvation. Diagnostic budgets: TTS first audio p95 ≤400 ms and RTF ≤0.5; complete MT-unit latency p95 ≤450 ms; ASR finalization measured from requested input frontier. End-to-end evidence decides whether a stage-budget miss matters. | M3 |
| Streaming end-to-end | On a predefined warm dev interval at real-time pace: `lag_onset` median ≤2.5 s and p95 ≤4 s, translation coverage ≥95%, flat debt AND oldest-source age over ten minutes. Publish unit lengths, cold acquisition, fallback frequency, and alignment uncertainty. | M3/M4 + E0 |
| Meaning and intelligibility | Blinded bilingual review of a fixed 40-unit sample averages ≥4/5 adequacy, with no unresolved material reversals of negation, numbers, or named entities. Any identity-only success with unintelligible or inaccurate speech fails this row. | M2/M4 + E0 |
| Memory | All resident models plus runtime ≤10 GB peak on the 24–32 GB reference Mac | M1/M3 |

Outcomes:
- **GO**: all rows pass. Begin E1. A result between 2 and 2.5 s passes feasibility while remaining short of the ≤2 s product target; report that gap explicitly.
- **GO-with-focus**: one non-lifecycle row misses, a time-bounded fix is documented, and the unchanged gate will be rerun. Continue only work independent of that gap. Missing listener evidence is pending, not a technical pass. Any public-data-only identity result remains provisional.
- **NO-GO**: stranded mute, or identity/meaning/sustained speed fails without a credible near-term fix. Keep safe capture/harness work available and use the next iteration on the bottleneck. A generic or merely similar narrator does not satisfy the original-speaker requirement.

Deliverable: `reports/week2/decision.md` names the exact model/runtime combination, measured gaps, quality evidence, fallback frequency, memory scope, outstanding porting work, and decision. Thresholds and product requirements are not relaxed after seeing results; a proposed product change is recorded separately.

---

## 10. Weeks 3–8: epics

| Weeks | Epic | Key tasks | Exit evidence |
| --- | --- | --- | --- |
| 3–4 | **E1 Live engine** | Verified XPC/helper inference; live tap → VAD → ASR → closed-unit MT → TTS → mixer; state machine; epoch/generation cancellation; bounded transport; warm-up at arm | 30-minute live FixturePlayer session, including worker crash/slowdown and original-audio recovery; trace-derived metrics within week-2 numbers |
| 3–5 | **E2 Voice acquisition** | Reference collector with quality checks, profile versioning, `ref_text` from ASR final, speaker-change detection with fallback | Voice readiness: median and p95 clean seconds and elapsed seconds per speaker, reported |
| 3–6 | **E3 Harness in CI** | Nightly replay of the dev set on the self-hosted Mac; regression thresholds; Perfetto artifacts | Nightly report; a regression fails the build |
| 4–6 | **E4 App shell** | Source picker (Zoom first), permission onboarding (`NSAudioCaptureUsageDescription`), model download manager with checksums, captions (tentative and committed), translate toggle, original-monitor gain, visible states (Preparing voice, Translating, Falling behind, Translation unavailable) | Usability walk-through with 3 internal users |
| 5–6 | **E5 Alpha hardening** | 60-minute soak with debt/source-age monitoring; device/sleep recovery; networking disabled after setup; drift correction; ANE/GPU placement and power report | **Week-6 alpha gate**: median ≤2 s/p95 ≤4 s warm latency, ≥95% translation coverage, no growing backlog/stranded mute, offline processing. Publish gaps as unmet targets; retain the v1 identity/naturalness release goals. |
| 7–8 | **E6 Multi-speaker beta and packaging** | Online speaker clustering (Sortformer evaluated); overlap fallback to original; Chrome + Meet source; Developer ID signing, notarization, hardened runtime; held-out quality report with 20 speakers and bilingual listeners | v1 week 7–8 evidence; human-rated adequacy, identity, and naturalness gates from v1 section 9 |

Weeks 9–20 (Windows, movie separation, AMD NPU) remain as v1 section 10 describes, with C10 applied, and re-planned at week 8 from real data. Week 9 begins with the ADR-0001 revisit and a Windows virtual-endpoint spike, because driver signing and distribution are the long pole.

---

## 11. Compliance gate (blocks external distribution, not development)

Resolve the applicable distribution requirements for the intended markets before external release. This work does not delay scaffolding, synthetic-fixture tests, or evaluations on material already cleared for that use.

- **Synthetic-output transparency:** assess Article 50 scope, exceptions, provider/deployer roles, and the chosen markets. The Commission describes machine-readable marking obligations with exceptions; it does not establish that this app universally requires one particular audio-watermark implementation. If marking applies, select a detectable mechanism and measure its effect on quality. Keep a clear synthetic-translation indicator as a product decision. [European Commission guidance](https://digital-strategy.ec.europa.eu/en/factpages/quick-facts-transparency-rules-ai-systems).
- **Voice data:** determine the applicable basis for collection/processing and evaluation consent. GDPR Article 9 concerns biometric processing for unique identification; Illinois law includes voiceprints. In-memory/session-scoped storage reduces retained data but does not itself resolve obligations concerning collection or use. [GDPR](https://eur-lex.europa.eu/eli/reg/2016/679/oj/eng/), [Illinois definitions](https://ilga.gov/legislation/ilcs/fulltext?DocName=074000140K10).
- **Model licenses:** pass on the Gemma use restrictions if TranslateGemma ships. HY-MT is excluded from shipping. Produce a third-party notices file automatically from `models/manifest.yaml`.
- **Misuse:** the product re-voices audio the user is already receiving and returns nothing to other participants. Keep it that way in alpha. Sending translated audio back into a meeting (v1 "separate feature") requires its own review.

---

## 12. Risk register

| Risk | Likelihood | Impact | Early signal | Mitigation | Owner |
| --- | --- | --- | --- | --- | --- |
| GPU contention breaks the latency budget | High | High | M3 inflation >1.5× | Profile actual placement; qualify an ANE/Core ML encoder or CPU ASR path; smaller MT; bounded/deadline-aware inference scheduling. SpeechAnalyzer does not promise a specific accelerator. | ML |
| Cross-lingual identity is not recognizable | Medium | High | M2 below the ≥70% feasibility gate | Longer/cleaner references; 1.7B comparison; alternate synthesis adapter; repeat the identity gate while retaining the original-speaker requirement | ML |
| zh→en word order forces long waits | High | Medium | M4 `lag_onset` p95 >4 s | Improve semantic boundary rules and model efficiency; report the remaining target gap. Condensed interpretation is a separate future product mode. | ML |
| English output is longer than the Mandarin source, so debt grows | Medium | Medium | Rising debt in 10-minute runs | Pacing ±10%; fallback rules; measure per speaker | ML |
| Stranded mute or a crash leaves the user without sound | Unknown until A3 | High | A3 failures | Qualify OS tap teardown; host-side original bus for worker failure; explicit teardown/device recovery. Any persistent mute blocks takeover. | AUD |
| speech-swift port has bugs, falls behind upstream, or is abandoned | Medium | Medium | Parity check gaps | Fork and pin; PyTorch parity tests; budget in-house fixes | ML |
| Corpus and consent arrive late | Medium | High | Speakers not booked by day 5 | Start day 1; use public sets for dev in the meantime | INT |
| Apple framework zh support or availability differs from expectations | Medium | Low | T0.3 | sherpa-onnx and MLX paths already in the plan | ML |
| Windows virtual audio driver cost | High | High (phase 3) | — | Spike in week 9; evaluate licensing an existing driver | AUD |
| License surprises in weights or exports | Medium | High | T0.2 | Manifest license verification with source links; CI check that blocks unlicensed rows | INT |

---

## 13. Working with AI coding agents on this repo

T0.1 establishes the following contributor rules for implementation.

- `AGENTS.md` is the canonical source for build/test commands, real-time rules, module boundaries, and research-data handling. `CLAUDE.md` points to it. These files must reflect the user's current authorization and must not create extra approval steps for routine local implementation.
- **Agent-friendly work:** `A2ACore` (commit policy, state machine, scheduler) driven by golden-trace tests; harness metrics; trace tooling; model adapters with recorded fixtures; the manifest and license tooling; UI views. Each ticket states its DoD as a runnable command, so the agent can verify its own work.
- **Priority review before external release:** callback safety, tap/device lifecycle, XPC identity checks/entitlements, signing, and license/data decisions. Coding agents can implement and test authorized local changes in these areas; review priority does not prohibit that work.
- **Data rule:** agents and external LLM judges never receive corpus audio or transcripts whose consent does not cover it. Fixtures in tests are synthetic or consented.
- **Harness as oracle:** commit-policy tuning is an optimization loop against `harness` metrics on the dev set. Agents may iterate on it, but the held-out test set is never used for tuning.

---

## 14. Sources

- Seed LiveInterpret 2.0: https://arxiv.org/abs/2507.17527
- Hibiki-Zero: https://arxiv.org/abs/2602.11072
- speech-swift: https://github.com/soniqo/speech-swift
- Qwen3-ASR: https://github.com/QwenLM/Qwen3-ASR
- Qwen3-TTS: https://github.com/QwenLM/Qwen3-TTS (report: https://arxiv.org/abs/2601.15621)
- CosyVoice: https://github.com/FunAudioLLM/CosyVoice
- TranslateGemma 4B: https://huggingface.co/google/translategemma-4b-it
- HY-MT1.5 license: https://github.com/Tencent-Hunyuan/HY-MT/blob/main/License.txt
- Apple TranslationSession: https://developer.apple.com/documentation/translation/translationsession
- Apple SpeechTranscriber: https://developer.apple.com/documentation/speech/speechtranscriber
- Apple Core Audio taps: https://developer.apple.com/documentation/coreaudio/capturing-system-audio-with-core-audio-taps
- Windows ML execution providers: https://learn.microsoft.com/en-us/windows/ai/new-windows-ml/supported-execution-providers
- WASAPI application loopback sample: https://learn.microsoft.com/en-us/samples/microsoft/windows-classic-samples/applicationloopbackaudio-sample/
- Streaming Sortformer: https://huggingface.co/nvidia/diar_streaming_sortformer_4spk-v2.1
- EU AI Act: https://eur-lex.europa.eu/eli/reg/2024/1689/oj
