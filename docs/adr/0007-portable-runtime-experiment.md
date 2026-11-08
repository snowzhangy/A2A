# ADR-0007 — Portable runtime experiment

Status: experimental, 27 October 2026; evidence and decisions added 28 October 2026.
Related: [ADR-0001](0001-language-boundary.md), [ADR-0002](0002-process-topology.md),
[ADR-0003](0003-single-aggregate-ioproc.md), [ADR-0004](0004-translation-only-playback.md).

The macOS Swift application remains the native Mac path. A separate Python runtime under
`runtime/` ports the pure reducer and uses the same C ring and mixer sources. Its Windows
host uses WASAPI process loopback and a render endpoint. Golden traces and deterministic
audio tests check part of the port's behavior; they do not establish production equivalence.
The Python runtime is an experiment and does not supersede the native, isolated inference
architecture in ADR-0002.

The portable host starts separate local processes for faster-whisper, Qwen or Hy-SimulMT
translation, RNNoise separation, Whisper language identification, and experimental voice cloning.
Model paths come from enabled, verified manifest entries;
setup is explicit and inference must not fetch weights. Recognition runs in CTranslate2
on CUDA or CPU. Translation runs in CTranslate2 on CUDA or CPU when the local derived
conversion exists, otherwise in PyTorch (CUDA, MPS, or CPU). OmniVoice's PyTorch backend
uses CUDA, MPS, or CPU. Actual concurrency, throughput, memory use, model quality, and
device support need measurement for each hardware configuration. Model provenance alone
does not qualify a model for product use or speaker identity.

On Windows, session volume is ducked and loopback gain restored rather than relying on
mute. Session-volume persistence, crash recovery, clipping, other applications changing
volume, output-device changes, and the resulting audible behavior require interactive
qualification before takeover is relied upon. The guard and journal are recovery aids,
not evidence that every failure mode is safe. Until that work passes, use this path only
for controlled development experiments. On macOS, live capture remains in the Swift app.

The portable policy is a second implementation: every policy change must keep the Swift
and Python traces aligned. Worker queues are bounded, speech and translation are never
written to default diagnostics, and failures are propagated to the host. Full parity
requires a native Windows app, packaging, hardware and lifecycle tests, and evaluation
against consented or synthetic speech. Offline agent-directed dubbing and per-line QA remain
separate capabilities. Experimental delayed picture, RNNoise separation, and ADR-0018
passthrough were added on 6 November with different, unqualified backend quality profiles.

## Decisions and evidence

**Lockstep port.** `runtime/a2a_runtime/core/` is a line-for-line port of `A2ACore`.
Swift's `.rounded()` is ported as round-half-away-from-zero, UInt64 subtraction wraps, and
double formatting matches Swift's. The five golden traces in
`apple/Tests/A2ACoreTests/Golden/` replay byte-identical in both. A change to one side's
policy lands with the same change to the other and the regenerated goldens. Golden files
and shell scripts are LF in every checkout (`.gitattributes`), because a CRLF checkout
broke the byte comparison.

**One real-time thread.** As in ADR-0003, one MMCSS "Pro Audio" thread services the
capture and render clients and calls `a2a_engine_process` (the C shim built as
`a2a_rt.dll`). The shim's Apple-only parts (the IOProc, `mach_absolute_time`) are behind
`__APPLE__`. Windows uses QPC time and `_aligned_malloc`. The callback rules in AGENTS.md
apply unchanged.

**Duck, not mute.** Measured with `a2a-wintap mute-probe`: muting an audio session also
silences its process-loopback capture. `volume-probe` showed that the capture carries the
session volume as an exact linear gain. The source is therefore ducked to −60 dB and the
capture multiplied by 1000 (clamped to ±1). Windows persists per-app volume, so
every duck is journaled to `.local/state/silenced-sessions.json` before it is applied. A
detached guard process, and `restore_stranded` at every start, repair what a crashed host
left behind. Newly opened streams of the source are ducked within 100 ms.

**Device changes.** Without `--output-device`, the host follows the default render device.
Both clients restart on the same engine and the reducer receives a `Discontinuity`, so
in-flight units are cancelled and translation resumes with the next phrase rather than the
session ending.

**Derived conversions.** Under WDDM a kernel launch costs about 25 µs, so eager PyTorch
decoding of Qwen3-4B was launch-bound: 112 ms per token for 12.7 ms of GPU work. The
manifest's `derived:` section pins a conversion: source entry, tool, options. `python -m
a2a_runtime.models.derive` builds it offline from SHA-verified source files and stamps the
output (`.a2a-derived.json`: source revision, tool version, options, output hashes).
Loaders refuse a stamp that does not match the manifest. CTranslate2 bf16 translates a
clause in 0.11–0.26 s on an RTX 4090.

**CUDA graphs for the voice.** OmniVoice decodes by masked diffusion: every step of a phrase
passes the same tensors, updated in place. The worker runs the first step eagerly,
captures the second, and replays the rest. One phrase went from 889 ms to about 150 ms, with
bit-identical audio. Any capture failure falls back to eager for the session.

**Memory under WDDM.** Oversubscribed GPU memory pages to system RAM instead of failing.
That shows up as stalls. Recognition loads first (int8_float16), and voice preparation runs
in inference mode. With autograd on, a 9.5 s reference peaked 1.81 GB above the weights
instead of 0.17 GB, and in a live run recognition and synthesis stalled together for 6.5 s.

**Recognition quality.** Partials and finals decode identically (beam 5, temperature 0) so
committed text does not diverge from what was shown. Mandarin gets a simplified-Chinese
initial prompt. Segments that look hallucinated are dropped: high no-speech probability
with low log-probability, a high compression ratio, or a bare stock phrase. An utterance
longer than 20 s is split at the quietest 100 ms of its last 3 s. When finals queue up, the oldest is
dropped and counted, instead of the worker being killed.

**System voice.** `--tts system` uses WinRT `Windows.Media.SpeechSynthesis` in one resident
PowerShell process. SAPI COM rendered the OneCore voices Kangkang and Yaoyao byte-identically
(same digest), so it cannot give two speakers distinct voices. Casting by pitch and native
speed-up up to 1.3× follow `SystemVoiceSynthesizer` and `VoiceCasting` in Swift.

**Direct audio on Windows.** `runtime/a2a_runtime/workers/hy.py` speaks the native worker's
protocol 2 over bounded queues: append, close, discard, and correlated answers work for streamed
or whole spans. `scripts/build_hy_local.py` builds the pinned llama.cpp fork and
`research/hy_worker` with CUDA on Windows or Metal on macOS. The worker's binary stdio is portable
and its no-model resampler self-test passes on Windows. Real Hy inference is unmeasured there
because the 19.7 GB GGUF is not installed.

**Delayed Windows picture.** Windows Graphics Capture timestamps frames on the same host clock as
WASAPI, compresses them as independent JPEG keyframes into the ADR-0012 bounded GOP ring, and
presents them in a Tk window. Frames remain in memory only. This is a CPU JPEG path, not the Mac's
hardware HEVC path; a synthetic-window capture/decode smoke passes, while production sync and
protected/minimized-window behavior remain interactive gates.

**Replaceable separation and passthrough.** An isolated RNNoise worker supplies 10 ms voice and
residual-bed frames. The host removes cold-start latency, uses isolated voice for ADR-0016
references and ADR-0017 sentence units, and delays the bed on source time through capture pauses.
The existing verified faster-whisper artifact identifies language without a new model entry. With
cloning and at least 4 s of delay, ADR-0018 adds the actor's voice into the still-held bed for
same-language, failed, or unanswered lines. RNNoise is not AUSoundIsolation and Whisper LID is not
ECAPA; no soundtrack-quality or LID-equivalence claim is made.

**Recovery identity.** Volume journals carry native audio-session identifiers and process creation
time, with targeted setters instead of PID-wide restore. Read/modify/write cycles use a Windows
file lock and unique atomic temporaries. Missing sessions stay pending, a reused PID is not
restored, and a relaunched app is repaired only while its session remains at the recorded duck
level. These are fake-OS regression checks, not crash-acceptance evidence.

**Live polling off the driver thread.** Ducking new source sessions needs a process snapshot
and an enumeration of every endpoint's audio sessions, about 70 ms together. Polled on the
driver thread every 100 ms, it once halved the driver's rate until the 4 s capture ring
overflowed. A watcher thread now does all OS polling, and the driver reads only its results
and the engine's counters.

**Measured live runs.** On Windows 11 with an RTX 4090 (other applications holding 7.5 GB),
the two-speaker synthetic demo was translated live from mpv via process loopback. With
cloned voices, every sentence after voice collection played, with a source-to-translation
onset median of 4.2–4.3 s and a max of 4.9 s. With the system voice, all six sentences
played (median 2.7 s, max 4.5 s). Every run had no errors or trace drops, a capture/mixer
correlation of 1.0, and a back-transcript WER of 0.07–0.14. Details are in
[runtime/README.md](../../runtime/README.md). This is plumbing and latency evidence with
synthetic speech on one machine, not quality or identity evidence.

**Not verified.** The Swift-side portability guards (A2ARTShim headers and sources) and the
`research/` worker changes have not been built or run on a Mac since they were made;
`scripts/check.sh` on the reference Mac remains the gate. The five existing goldens still omit
slots/dialogue and ADR-0015/0018, so the ported unit suites—not new goldens—cover those until
Swift regenerates expanded traces. Real Hy inference, live A/V sync, soundtrack quality, other
GPUs, packaging, and crash/device acceptance remain open.
