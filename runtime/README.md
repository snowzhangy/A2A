# Portable runtime (experimental)

The Windows desktop preview uses WASAPI process loopback, the shared C mixer, and a Python
port of the policy core. The macOS app remains the supported development path in the root README.
The portable CLI also supports WAV replay on macOS, but live Mac capture uses the Swift app.
The Windows runtime is not yet a release-ready replacement: source-volume recovery and
the full live pipeline have not been qualified on interactive devices.
See [ADR-0007](../docs/adr/0007-portable-runtime-experiment.md) for the design and evidence.

GitHub prereleases also provide a Windows 11 x64 archive with the source, desktop
launcher and compiled native audio library. See the
[archive installation instructions](../docs/releases/windows-development-build.md).
Models and Python dependencies remain separate explicit setup steps.

## Setup on Windows

Install Python 3.12 or 3.13, `uv`, and Visual Studio 2022 with the C++ desktop workload
(its clang-cl and a Windows SDK 10.0.20348 or newer, for process loopback). An NVIDIA GPU
with a current driver is recommended; everything also runs on the CPU, much more slowly.
From the repository root:

```powershell
uv sync --project runtime --locked --extra gpu --extra video
uv sync --project runtime/separator --locked
uv run --project runtime python runtime/native/build.py
python scripts/check.py
```

`scripts/check.py` is the deterministic check: the manifest gate, the native build, the
runtime tests (including the golden traces shared with Swift), and the harness against the
portable pipeline. It needs no devices, models, or network.

Install the explicitly named, enabled model artifacts, then build the local CTranslate2
conversion of the translation model. Fetching needs many gigabytes; startup never downloads:

```powershell
uv run --project harness python scripts/fetch_models.py --models faster-whisper-large-v3,qwen3-4b-instruct-2507,omnivoice-0.6b-torch
uv run --project runtime python -m a2a_runtime.models.derive qwen3-4b-instruct-2507-ct2
uv run --project runtime python -m a2a_runtime.models.derive qwen3-4b-instruct-2507-ct2-int8
```

The second conversion is optional (4.0 GB, about a minute). When other applications leave too
little GPU memory for bfloat16 translation, the worker runs int8. With this conversion it loads
stored int8 weights in about 5 s. Without it, CTranslate2 quantizes the bfloat16 weights at every
start: 31–37 s on an RTX 4090 PC, with identical translations on the test sentences.

For the direct-audio Hy-SimulMT path, also fetch its research-only GGUF and build the native
worker (CUDA on Windows, Metal on macOS):

```powershell
uv run --project harness python scripts/fetch_models.py --models hy-simulmt-30b-a3b-q4km-gguf
python scripts/build_hy_local.py
```

The conversion is offline. It re-verifies every source file's SHA-256 against the
manifest, converts with the options the manifest pins, and stamps the output with the
source revision, converter version, and output hashes. The translator refuses a stamp that
does not match. Without the conversion, translation falls back to PyTorch and says so at
startup. Under Windows that fallback decodes at 112 ms per token, several times slower
(see GPU notes). `--check` re-hashes an
existing conversion. To smoke-test the installed models, opt in with
`$env:A2A_RUN_MODEL_TESTS='1'` and run `uv run --project runtime pytest runtime/tests/test_model_runtime.py`.

## Desktop preview

After setup, double-click `A2A-Windows.bat` in the repository root. The session matches
the Mac app's:

- **Audio source**: **System audio** (the default, as on the Mac) translates everything this
  PC plays except A2A itself and ducks every other app while it runs. Choosing an
  application session instead (press **Refresh** after starting playback) translates and
  ducks only that app.
- **Voice**: **Original voices** speaks each detected speaker in their own cloned voice
  (OmniVoice, research-only). **System voice** casts an installed Windows voice per speaker
  (no model). **Captions only** leaves the source at its normal volume.
- **Prioritize complete phrases** is the Mac app's toggle (`--quality-first`): whole phrases,
  a 20 s debt and lag budget. Without it, sessions use the Mac app's adaptive prebuffer and
  8 s debt limit.
- **Keep music and effects** enables the experimental RNNoise background bed.
- **Match picture to translated speech** captures a selected window with Windows Graphics Capture,
  keeps compressed frames only in memory, and presents them on the audio host clock 4–12 seconds late.

The listen selector switches between original and translated audio while a voice session
is running. **Stop** ends the session, restores every ducked app, and clears captions.
Desktop playback sends full validated translations to the voice worker, with a longer
bounded speech budget; a long line can finish late or cause later lines to be skipped.

For a first trial, start playback in the source app, select that application after pressing
**Refresh**, and choose **System voice** or **Captions only**. System audio captures and
ducks other applications too. If a source remains quiet after an interrupted run, run
`uv run --project runtime a2a-wintap restore` and check its volume in Windows Volume mixer.
Ambiguous or missing sessions remain in the recovery journal for manual inspection rather
than being restored by guessing. Crash and device-change recovery still need qualification.

The interface starts models locally and has no download button. If a model is missing,
it reports the explicit fetch command above. The GPU setting can be Automatic, NVIDIA GPU,
or CPU; the CLI below also supports per-stage choices.

## Live translation from the CLI

List audio sessions and note the PID of the application that is actually playing speech
(`a2a-wintap devices` lists output endpoints):

```powershell
uv run --project runtime a2a-wintap list
```

Captions only: captures that process tree without ducking its audio. Captions appear in
the terminal:

```powershell
uv run --project runtime a2a-pipeline-portable --tap-pid 1234 --captions-only --events-stdout --asr whisper --mt llm --tts tone --trace .local/captions-trace.jsonl
```

Translated audio in each speaker's cloned voice, for a source and speakers you have
permission to process. The source is ducked to −60 dB while A2A renders. Process loopback
captures the session volume as an exact gain, so a 1000× capture gain restores the source
level; muting the session would also silence the capture. Ctrl-C stops the session and
restores the source volume:

```powershell
uv run --project runtime a2a-pipeline-portable --tap-pid 1234 --asr whisper --mt llm --tts clone --fluent --translation-only --speakers auto --trace .local/live-trace.jsonl
```

`--tap-global` replaces `--tap-pid`/`--tap-exe` with the whole output mix, as the Mac
app's System audio source: WASAPI process loopback in exclude mode captures every process
except this runtime's own tree (its playback and its workers), and every other audio
session is ducked, journaled and guarded exactly like a single app. A mix with nothing
playing is not a capture fault; the session waits for audio. The recording then holds every
sound the machine makes: consent has to cover that, and `--source-out` is restricted data.

`--tts system` speaks with the installed Windows voices instead (WinRT
`Windows.Media.SpeechSynthesis`, no models). Each detected speaker is cast a distinct
installed voice for their pitch, as on macOS. It is plumbing only: it does not preserve the
speaker. `--output-device NAME` renders to a specific endpoint. Without it, the session
follows the default output device: when headphones connect or Bluetooth reconnects, both
WASAPI clients restart on the new device, in-flight units are cancelled, and translation
resumes from the next phrase.

Models load before capture starts, so leave the source running during startup. The
recognition, translation, and voice workers start together, so their imports and weight
checks overlap. Each still claims GPU memory in turn: translation sizes itself once
recognition is resident, and the voice loads its weights once translation has sized itself.
Every duck
is journaled to `.local/state/silenced-sessions.json`, and a detached guard restores it if
the pipeline dies. If a source is ever left quiet, run
`uv run --project runtime a2a-wintap restore` and check the Windows volume mixer.

## File mode

A controlled experiment with installed models and an appropriately consented source WAV.
Put all output under `.local/` or restricted storage:

```powershell
uv run --project runtime a2a-pipeline-portable --input .local/input.wav --clock host --pace realtime --asr whisper --mt llm --tts clone --fluent --tail-seconds 90 --trace .local/trace.jsonl --out .local/translated.wav
```

`--device auto` puts each stage on CUDA, then MPS (translation and voice only;
faster-whisper has no MPS backend), then the CPU. Recognition and translation check free
GPU memory (`nvidia-smi`) for the weights plus a reserve and fall back to the CPU when it
is short. That is an estimate, not a reservation. Override one stage with
`--asr-device`, `--mt-device`, or `--tts-device` (for example `--asr-device cpu` when other
applications hold most of the GPU). The bounded tail lets in-flight recognition and speech
finish after input ends. If recognition is still pending at the limit, the process reports
failure.

## Shared multiplatform modules

| Concern | Shared authority | Windows adapter |
| --- | --- | --- |
| Session decisions | `apple/Sources/A2ACore` and golden traces | `runtime/a2a_runtime/core` mirror and ported tests |
| Real-time rings/mixer | `apple/Sources/A2ARTShim` C sources | `runtime/native/a2a_wasapi.cpp`, one MMCSS thread |
| Driver effects | `a2a-pipeline` event/effect contract | `runtime/a2a_runtime/driver.py` |
| Model processes | Length-prefixed worker protocol | Hy/CUDA, faster-whisper, Qwen, RNNoise, OmniVoice, WinRT |
| Picture | ADR-0012 bounded frame/slot policy | Windows Graphics Capture + Tk presentation |
| Separation/LID | ADR-0016–0018 policy | RNNoise and faster-whisper, explicitly different quality profiles |

There is intentionally no third policy core and no policy in the UI, workers, or audio callbacks.

## Parity with the Mac pipeline

The policy core is a line-for-line port of `A2ACore`, and the driver follows
`apple/Tools/a2a-pipeline`. Both hosts make the same decisions from the same inputs:
`runtime/tests` replays the five Swift golden traces byte for byte and ports the Swift unit
suites, and the harness runs its pipeline tests against either binary. Ported on 2 November
from the Mac's last week:

- Clause commits: a unit that already covers 4 s of speech (`--max-unit-seconds`) closes at
  its last safe boundary, late punctuation re-aligns the commit frontier, and cuts stay off
  Chinese words. The Mac injects NaturalLanguage's segmenter; Windows uses the system's
  `Windows.Data.Text.WordsSegmenter` in-process (`--no-word-cuts` turns it off).
- Speakers: each cloned speaker keeps their first voice for the session, a line waits for its
  own speaker's label instead of borrowing the last actor's voice, and short unlabeled pieces
  continuing a turn inherit it (`--speaker-carry`, `--speaker-carry-pause`, `--speaker-carry-piece`).
- Targets: an over-long translation is cut to the time its source took rather than dropped
  (`--no-shorten`), and a filler-only answer is silence, not a failure (`--no-filler-silence`).
- Playout: `--quality-first`, `--adaptive-prebuffer`, picture slots (`--presentation-delay-ms`,
  ADR-0012) and turn fill (`--no-turn-fill`, ADR-0013), with `tts.done` pacing in the trace.
- Text: `--glossary terms.json` before translation, `--pronounce words.json` for the voice only.
- Voices: `--dual-clone` (two OmniVoice workers when the GPU has room, else one),
  `--voice-memory DIR`, `--clone-delivery line` (ADR-0015), and `--tts system`.
- Direct audio: `--asr audio --mt hy-local` streams open spans to the same native worker protocol
  as the Mac, and `--mt hy --hy-server URL` explicitly sends spans to the selected server.
  `python scripts/build_hy_local.py` builds the pinned worker with CUDA on Windows or Metal on macOS.
- Picture and soundtrack: Windows Graphics Capture presents a selected window on the audio clock;
  delayed slots synthesize ahead, sentence units cut at pauses or quiet dips, and the shared C
  background bus plays an experimental RNNoise bed (`--background`).
- Passthrough: isolated references reject mostly non-voice audio (ADR-0016), and with
  `--background --tts clone --presentation-delay-ms 4000` or longer, Whisper language
  identification and ADR-0018 restore target-language or unanswered lines into the held bed.
  Use `--no-reference-isolation`, `--no-sentence-units`, or `--no-passthrough` for A/B runs.

Still not on Windows: `--mt context` and Qwen LiveTranslate (`--qwen-cloud`). RNNoise is not
AUSoundIsolation and Whisper LID is not the Mac's ECAPA model: their quality profiles are separate
and unqualified. JPEG picture buffering is CPU-based, not the Mac's hardware HEVC path.

## Measuring a run

The live demo plays the synthetic Mandarin clip in mpv, translates it live, back-transcribes
what was played, and checks every sentence (the counterpart of `scripts/live_demo.sh`):

```powershell
uv run --project harness python -m a2a_harness.zh_demo --out .local/fixtures/zh-demo --lead 4
uv run --project runtime python -m a2a_runtime.tools.live_demo --out .local/runs/live-1 --output-device SPDIF
```

The player starts idle, so the tap exists before the first sample, and the clip is loaded
over mpv's IPC pipe once the pipeline is live. `--output-device` sends both the player and
the translation to an endpoint nobody listens to, for an unattended run. On Windows the demo
clip alternates the Huihui and Kangkang voices through WinRT, giving two distinct speakers.
`python -m a2a_runtime.tools.transcribe --input x.wav` is the portable `a2a-transcribe`.

## Measured on the reference Windows machine

Windows 11 26200, RTX 4090 24 GB, with other applications holding 7.5 GB of GPU memory.
Live runs of the six-sentence two-speaker demo clip (synthetic WinRT speech), with mpv
tapped by process loopback and `--fluent --translation-only --speakers auto`, 28 October 2026:

| Measure | Cloned voice (2 runs) | System voice (1 run) |
| --- | --- | --- |
| Sentences translated and played | 4 of 6 (sentences 1–2 are spoken while each voice is collected, and are skipped by design) | 6 of 6 |
| Source onset to translated onset | median 4.2–4.3 s, max 4.6–4.9 s | median 2.7 s, max 4.5 s |
| Commit to playback | 0.47–1.16 s per unit | |
| Translation (CTranslate2 bf16) | 0.11–0.22 s per clause | |
| Voice first audio | 0.30–0.48 s per phrase (OmniVoice fp16, 16 steps, CUDA graphs) | about 15 ms (WinRT) |
| Voice preparation | 0.66–1.11 s for a 4.7–8.6 s reference | |
| GPU memory | recognition 2.3 GB, translation 9.2 GB, voice 2.6 GB | recognition and translation as left |
| Errors, trace drops, capture/mixer correlation | 0, 0, 1.0 | 0, 0, 1.0 |
| Back-transcript WER against the played text | 0.10–0.14 | 0.07 |

2 November 2026, after the Mac-parity port, same clip and machine (other applications held
9–11 GB of GPU memory), each run with the Mac app's session settings for that voice:

| Measure | Cloned voice, live | System voice, live | Cloned voice, 8 s picture slots |
| --- | --- | --- | --- |
| Sentences played | 6 of 6 | 6 of 6 | 4 of 6 (the first two expire while the voices are learned; slot lag limit 2 s) |
| Source onset to translated onset | median 8.3 s, max 17.8 s (the first lines wait for their speaker's voice) | median 3.0 s, max 4.6 s | 7.6–7.8 s: every phrase 0–20 ms after its slot |
| Translation per clause | 163–820 ms (bf16; other apps held about 11 GB just before) | 140–285 ms | 98–300 ms (int8, GPU reserve) |
| Voice first audio | 0.50–1.68 s | 19–78 ms | 0.28–0.92 s, median 0.30 s |
| Back-transcript WER | 0.32 (one line played out of order, see below) | 0.065 | 0.0 |
| Errors, trace drops, capture/mixer correlation | 0, 0, 1.0 | 0, 0, 1.0 | 0, 0, 1.0 |

The cloned-voice live run played the second speaker's first line after the first speaker's
next one: it waited for its own voice, and the reducer (identical on both hosts) let a
later line whose voice was ready go first. The live check measures WER in source order, so
it fails that run although the back-transcript matches what was played.

These numbers are plumbing and latency evidence on one machine with synthetic speech. They
do not qualify translation quality, voice quality, or speaker identity.

6 November 2026, multiplatform parity slice on the same machine: `python scripts/check.py`
passed 265 runtime tests (including five byte-identical Swift goldens and the ADR-0015–0018
ports) and 146 harness tests. `python scripts/check.py --models` also passed all four installed-model
tests. The native CUDA Hy worker built at the pinned revision and passed
`hy-worker.exe --self-test`; streamed and whole-span replay passed through the shared fake worker.
Windows Graphics Capture returned and decoded a synthetic window with no frame files, and the
installed Whisper LID model distinguished synthetic English from synthetic Mandarin. An RNNoise
file run produced a delayed background bed without errors. These are functional checks, not
real-Hy inference, A/V sync, soundtrack-quality, or cross-hardware qualification.

## GPU notes (Windows)

Under WDDM a CUDA kernel launch costs about 25 µs of CPU time, so eager PyTorch is
launch-bound. One Qwen3-4B token needs ~2,600 launches, which comes to 112 ms per token for
12.7 ms of GPU work. That is why translation runs on CTranslate2's fused kernels. An
OmniVoice phrase issued ~29,500 launches (889 ms for 177 ms of GPU work). The voice worker
replays each phrase's decoding step as one CUDA graph, at about 150 ms with bit-identical
output. `A2A_TTS_CUDA_GRAPHS=0` disables the graphs.

WDDM also pages a process's GPU memory out to system RAM when the device is
oversubscribed. That causes stalls, not out-of-memory errors. Recognition loads first and
uses int8_float16. Translation leaves 4.5 GiB for each GPU voice worker that loads after it,
so on a shared card it quantizes to int8 (+50–150 ms per clause) rather than leave the voice
1.7 GB: on 2 November that left a voice warm-up running at 100% GPU for nine minutes without
finishing, and a bf16 session stalled recognition and synthesis together for 17 s. With the
reserve the same session ran with 5 GB to spare and was faster end to end. Voice preparation
runs in inference mode: with autograd on, encoding a
9.5 s reference peaked 1.81 GB above the weights instead of 0.17 GB. In a live run that
spike paged the other workers out, and recognition and synthesis stalled together for
6.5 s. Keep at least 2 GB of the GPU free for the session, and watch `nvidia-smi`.

## On macOS

The portable runtime runs file mode and the deterministic tests on macOS (`python3
scripts/check.py` runs `scripts/check.sh` first). Translation and the voice can use MPS
(`--clone-backend torch`); the native MLX voice backend stays in `research/` and the Swift
app. Live capture on macOS uses the Swift `a2a-pipeline --tap-bundle` and the A2A app.

## Limitations

There is no Silero VAD adapter yet (the manifest does not pin a revision), and the desktop
preview has no saved-voice controls (the CLI has `--voice-memory`). The voice clone and the
RNNoise separator are research-only and unqualified for identity or soundtrack quality. Hy-SimulMT's
19.7 GB model is not installed on the current reference machine, so its real Windows inference
has not been measured; the native worker passes its no-model resampler self-test and all protocol
tests use the shared fake worker. Translated playback ducks the source application's session
volume (every other application's, with System audio). Its journal, guard, and device-change handling have been exercised but have not
passed crash and device-change acceptance. Do not treat them as a verified safety guarantee.

Dub Anything is an offline, agent-directed video dubbing workflow with line-level
directing and QA; this runtime is a live interpreter. Similar voice, translation, and
delivery quality cannot be inferred from using related models. A comparison requires
consented evaluation audio, independent transcription and speaker-similarity metrics,
listening tests, and measured latency under concurrent load.
