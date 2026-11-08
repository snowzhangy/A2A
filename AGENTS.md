# AGENTS.md — A2A contributor rules

A2A captures another app's Mandarin audio on macOS and plays English in the original speaker's voice, all locally. An experimental portable runtime in `runtime/` does the same on Windows ([ADR-0007](docs/adr/0007-portable-runtime-experiment.md), [runtime/README.md](runtime/README.md)). Start with [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) (execution baseline, v2.1), then the ADRs in [docs/adr/](docs/adr/). For what works today and what is still pending, see [reports/week2/status.md](reports/week2/status.md).

These rules apply to people and coding agents. Routine local implementation and testing need no extra approval. Review priority (below) is about what gets a careful second look before external release, not a ban on working in those areas.

## Toolchain

- Use full Xcode. On the reference Mac, `xcode-select` points at Command Line Tools, and that SwiftPM install cannot link package manifests. `scripts/env.sh` exports `DEVELOPER_DIR` to an installed Xcode (currently `/Applications/Xcode-27.0.0.app`) without touching `xcode-select`. Every script sources it. For manual commands, run `source scripts/env.sh` first.
- The deployment target is macOS 26.0 on Apple silicon. Guard 26.4+ APIs such as the `preferredStrategy:` overload of `TranslationSession`.
- Executables must record the SDK they were built with. Swift Build stamps the deployment target (`sdk 26.0`) into `LC_BUILD_VERSION`, and Translation gates on that value: a 26.0-stamped binary sees installed zh-Hans→en languages as `supported` and throws `notInstalled`. `sdkVersionStamp()` in `apple/Package.swift` writes `A2A_SDK_VERSION` (exported by `scripts/env.sh`), and `scripts/check.sh` fails on a mismatch.
- Windows: the portable runtime is a uv project (Python 3.12–3.13) whose native shim builds with Visual Studio 2022's clang-cl (`runtime/native/build.py`). `python scripts/check.py` is the deterministic check on every platform; on macOS it runs `scripts/check.sh` first.
- Python tooling uses `uv`. `harness/` is a uv project. The heavy evaluation models (COMET, Whisper, UTMOS, speaker verifiers) belong in `research/`, so ordinary checks never resolve torch.

## Commands

| What | Command |
| --- | --- |
| Everything deterministic | `scripts/check.sh` (`--quick` skips the harness↔pipeline integration) |
| Everything deterministic, any platform | `python scripts/check.py` (`--quick`; `--models` adds the installed-model tests) |
| Portable pipeline (Windows) | `uv sync --project runtime --locked --extra gpu --extra video`, `uv sync --project runtime/separator --locked`, `uv run --project runtime python runtime/native/build.py`, then `a2a-pipeline-portable` with `a2a-pipeline`'s flags (`--tap-global` is the whole output mix, as in the Mac app) plus `--tap-pid\|--tap-exe`, `--output-device`, `--device` (see [runtime/README.md](runtime/README.md)); `A2A-Windows.bat` is the desktop app |
| Derived model conversions | `uv run --project runtime python -m a2a_runtime.models.derive [ID] [--check]` (offline, from SHA-verified manifest entries; stamps `.a2a-derived.json`) |
| Windows tap tools | `a2a-wintap list\|devices\|capture\|fidelity\|mute-probe\|volume-probe\|monitor\|restore` |
| Windows delayed-picture smoke (interactive) | `uv run --project runtime --extra video python -m a2a_runtime.tools.video_smoke` — captures only its own synthetic Tk window |
| Windows live demo + check (interactive) | `uv run --project runtime python -m a2a_runtime.tools.live_demo --out .local/runs/NAME [--output-device DEV] [--tts clone\|system]` |
| Portable back-transcription | `uv run --project runtime python -m a2a_runtime.tools.transcribe --input x.wav [--locale en-US]` |
| Local development app | `scripts/build_app.sh` (optimized; `--debug` for a debuggable build), then `open .local/apps/A2A.app` (Original / Translated playback switch; Translated is the default) |
| Isolated model stage | `scripts/build_stage_probe.sh`, then `a2a-stage-probe asr\|mt\|tts\|resample\|speakers --input FILE --out .local/probe/STAGE` (installed models; no capture or audio-device playback; `speakers` writes tracker labels only) |
| Native window startup, synthetic display | `scripts/smoke_app.sh` after building; no capture or inference (`--screenshot` additionally needs screen capture access) |
| Apple model runtime tests (interactive) | `A2A_RUN_APPLE_TESTS=1 swift test --package-path apple --filter AppleAdapterContractTests` (source `scripts/env.sh` first) |
| Build / test Swift | `swift build --package-path apple`, `swift test --package-path apple` |
| Regenerate golden traces | `A2A_UPDATE_GOLDEN=1 swift test --package-path apple --filter GoldenTraceTests`, then review the diff line by line |
| Harness tests | `uv run --project harness pytest harness/tests` |
| Synthetic fixture | `uv run --project harness python -m a2a_harness.synth --out .local/fixtures/synthetic --sentences 10` (add `--minutes 10` for E0-length input) |
| Replay run | `uv run --project harness python harness/run.py --fixture <yaml> --pipeline "$(swift build --package-path apple --show-bin-path)/a2a-pipeline" --mode paced\|offline` |
| Perfetto view | `uv run --project harness python harness/trace2perfetto.py <run>/trace.jsonl`, then open it in ui.perfetto.dev |
| Runtime preflight (T0.3) | `$(swift build --package-path apple --show-bin-path)/a2a-preflight --json reports/week2/raw/preflight.json --translate-probe` |
| Tap tools (A1–A3) | `a2a-tapctl permission\|list\|devices\|resolve\|taps\|destroy-tap\|capture\|monitor\|fidelity\|lifecycle`, with `--global` for the whole output mix (see the header of `apple/Tools/a2a-tapctl/main.swift`) |
| Global-tap feedback gate (interactive) | `$(swift build --package-path apple --show-bin-path)/a2a-loopprobe --json reports/week2/raw/global-loopprobe.json` — proves our own playback stays out of the system-audio mix ([report](reports/week2/system-audio-source.md)) |
| Fidelity reference | `FixturePlayer --probe --seconds N --write-reference ref.wav` saves exactly what it plays, for `a2a-tapctl fidelity --reference ref.wav --capture x.wav` |
| Picture-matched translation (ADR-0012) | add `--fluent --presentation-delay-ms D` to a live run: each phrase starts at its source start + D, the original bus and `--background` bed are delayed by D, and lag limits count from the slot; the app's **Match picture to translated speech** shows the chosen window D late (`A2AVideo`). In slots only, a take shorter than its speech is
  slowed so the line ends with the actor (ADR-0013 turn fill; `--no-turn-fill` disables it, `tts.done.pace`
  below 1, harness `turn_dead_air_s`/`turn_overrun_s`). Summary `slot` and trace `play.scheduled.late_ms`
  report slot timing |
| Live translation | `a2a-pipeline --tap-global (or --tap-bundle ID / --tap-pid N) --asr audio --mt hy --hy-server http://HOST:8082 --tts system [--seconds S] --trace t.jsonl --source-out s.wav --translated-out t.wav` (Ctrl-C stops and restores the app's audio) |
| Local Hy-SimulMT (research-only model) | `scripts/build_hy_local.sh` on macOS or `python scripts/build_hy_local.py` on Windows (llama.cpp fork for PR #31198 with Metal/CUDA, then `.local/hy-simulmt/bin/hy-worker`), `uv run --project harness python scripts/fetch_models.py --models hy-simulmt-30b-a3b-q4km-gguf` (19.7 GB). Run it with `--mt hy-local [--hy-worker PATH] [--hy-model GGUF]` in place of `--mt hy --hy-server URL` (both paths default to the repository's `.local/`, see `A2AModels.HyLocalSetup`); the app's Hy-SimulMT picker "On this Mac" passes them explicitly. `A2AModels.HyLocalTranslator` starts the native worker per session like the clone worker (same length-prefixed stdin/stdout frames; stdin EOF ends it). The pipeline loads it alongside the voice workers and before any tap, so a failed load never mutes the source; stderr says `Hy-SimulMT worker ready: …, load X s, warm-up Y s` (about 3 s with the GGUF in page cache, 7–8 s cold) or `Hy-SimulMT worker failed: …`. One rolling Hy conversation in the worker's KV cache (1600 then 800 ms turns, ST fallback for the unanswered tail; both task prompts stay resident). With `--asr audio` each span streams to the worker while it is spoken (`AudioStreamFeed`, worker protocol 2; `--no-hy-stream` sends spans only once they close). With a picture delay, direct-audio spans are sentence units (ADR-0017): they close at pauses that an `EnergyVAD` finds on the separator's isolated voice (with `--background`), and are capped at D − 3.5 s (3–6 s), where a unit that reaches the cap is cut at its quietest frame. Both `--dual-clone` workers synthesize ahead of their slots. `--no-sentence-units` restores 3 s cuts on the mix; the run summary's `audio_units` counts pause and cap closes. Same-language passthrough (ADR-0018, `--no-passthrough` to turn off) needs `--background` and a delay of at least 4 s. `research/lid_worker.py` identifies each unit's language with the fetched `ecapa-voxlingua107-lid-mlx`, found under `.local/models` beside the clone worker or given with `--lid-model`. A line already in the target language, or one Hy answers with nothing or a filler, plays the actor's own isolated voice on the delayed picture, because the bed is held min(6 s, D − 1.5 s) before it is written. Each line's length budget only runs into the silence after it, plus 0.2 s. The run summary's `passthrough` counts same-language lines and restores. Standalone check: `hy-worker --model GGUF --test WAV --cuts 3 --from A --to B [--stream-ms N] [--text]`; `hy-worker --self-test` checks that streamed resampling equals whole-span resampling (no model). Logs carry timings only unless `--text`. Latency A/B: `scripts/bench_hy_local.sh [--load]` and TTS timing: `uv run --project research python research/tts_bench.py --reference REF.wav` ([reports/week2/inference-latency.md](reports/week2/inference-latency.md)). |
| Cloned-voice translation (research-only model) | add `--tts clone --clone-mode xvector` to a direct-audio run: the resident `research/tts_worker.py` streams Qwen3-TTS in a voice learned from the source's first audio segments (`--voice-ref ref.wav --voice-ref-text ref.txt` for a fixed reference; `--clone-mode xvector` skips the transcript). With `--asr audio --clone-mode xvector`, each acquired reference segment is the voice isolated by AUSoundIsolation, and a segment whose isolated voice is more than 10 dB below the segment is rejected as `not_voice`. `--no-reference-isolation` turns this off (ADR-0016). Needs `uv sync --project research --locked` and the fetched `qwen3-tts-12hz-0.6b-base`. Runs report `tts_identity_qualified: false` and never count as identity or gate evidence. For per-line delivery conditioning (ADR-0015) add `--clone-delivery line`: each unit's own source audio is appended to its speaker's reference for that take only, the worker reports the verdict (`line`, `too_quiet`, `reference_bound`, `needs_source_text`, `failed`) in `tts.done.delivery`, and it costs about 30–50 ms to first audio. Score delivery with `uv run --project research python research/dub_delivery.py --from-run RUN` (numpy only; `--source/--takes/--units` for explicit files, one units file per variant when the runs differ in timing). |
| Live demo + check (interactive) | `scripts/live_demo.sh [--max-debt-seconds 6 --max-source-lag-seconds 8]`: ffplay plays a Mandarin video, the pipeline translates it live, then `a2a_harness.live_check` checks every sentence and the back-transcript |
| Mandarin demo clip | `uv run --project harness python -m a2a_harness.zh_demo --out .local/fixtures/zh-demo --lead 4` (system zh_CN voice, synthetic) |
| Back-transcription | `a2a-transcribe --input x.wav [--locale en-US]` (installed SpeechTranscriber locale) |
| FixturePlayer app | `scripts/build_fixture_player.sh`, which produces `.local/apps/FixturePlayer.app` (`dev.a2a.FixturePlayer`) |
| Model manifest | `uv run --project harness python scripts/fetch_models.py --check\|--list\|--models ID\|--notices` |
| Portable runtime (ADR-0007, experimental) | `uv run --project runtime python runtime/native/build.py`, then `uv run --project runtime python -m pytest runtime/tests` on the host it builds for (Windows). On macOS the pure-policy tests also run as `PYTHONPATH=runtime uv run --project harness python -m pytest runtime/tests` (not re-run on a Mac since the 2 November port; Windows: 283 passed, 5 opt-in model tests skipped). [runtime/README.md](runtime/README.md) owns setup and limits |

Every ticket states its DoD as a runnable command. Run it before you call the work done, and report failures with their output.

## Module boundaries

| Module | Owns | Must not |
| --- | --- | --- |
| `A2ACore` | Types, commit policy, state machine, playout gate, `SessionPipeline` reducer, trace event values | Do I/O, import Apple media frameworks, or call models. Clocks and events are injected. |
| `A2ARTShim` (C) | SPSC rings, timing records, the IOProc, the original/translated mixer and the optional background bus | Allocate, lock, log, or call Swift/ObjC on the callback path |
| `A2ATrace` | JSONL writer, draining RT records into trace events | Run on an RT thread |
| `A2AAudio` | Process taps, the private aggregate device, source resolution, `TapSession` | Make policy decisions (those belong in `A2ACore`) |
| `A2AMedia` | WAV, resampling, signal measurements | Run on an RT thread |
| `A2AModels` | Adapters: fixture fakes and Apple frameworks (third-party models are added per selected combination) | Download at runtime (D8) |
| `A2AVideo` | Delayed picture of the chosen source window (ADR-0012): ScreenCaptureKit capture, hardware encode into a bounded memory ring, presentation on a host-clock timebase | Make playout decisions (slots belong in `A2ACore`), touch audio, or write frames anywhere |
| `runtime/` (ADR-0007, experimental) | The portable Python port of the reducer, the same C rings and mixer built for WASAPI, and the Windows host | Be imported by the Mac product, or claim production equivalence: its own golden-parity test decides whether the port still follows `A2ACore` |
| `Tools/*`, `Apps/*` | Drivers that execute `SessionPipeline` effects | Duplicate policy logic |
| `runtime/native` | The WASAPI host: one MMCSS thread calls `a2a_engine_process` from the same shim sources | Break the real-time rules below |
| `runtime/a2a_runtime/workers`, `research/tts_worker.py` | Resident model processes (ASR, MT, voice) behind bounded queues | Download, or print text without `--research-text` |

The reducer pattern is the contract. Drivers feed inputs with their event time into `SessionPipeline.handle`, then execute the returned effects. Golden tests, replay, and the live host therefore exercise identical decisions.

Two hosts build from one set of C sources (ADR-0007): the Apple IOProc here and a WASAPI engine under `runtime/native`, so a change in `A2ARTShim` is a change on both. A change in `A2ACore` has to be mirrored in `runtime/a2a_runtime/core`, with its Swift tests ported to `runtime/tests`: `runtime/tests/test_golden.py` compares the Swift golden traces byte for byte, but none of the five goldens is a slot or dialogue session, so the ported unit suites (picture delay, turn fill, speakers, target budget) are what catch a divergence there. The driver mirrors `apple/Tools/a2a-pipeline`, and `python scripts/check.py` runs the harness against it.

## Real-time rules (review checklist)

- The capture IOProc copies into a preallocated ring. The output path drains buffers and does bounded, preallocated mixing, gain ramps, and resampling.
- Each callback may append fixed-size timing records to its own SPSC ring.
- Callbacks never allocate, take locks, format logs, call XPC or Swift async, or touch UI.
- Avoid ARC traffic, Objective-C bridging, and destruction of reference-counted objects on these threads. The IOProc is the C function `a2a_engine_ioproc`, and its client data is a raw engine pointer.
- Allocate RT memory with `a2a_prefaulted_calloc`. Plain `calloc` maps pages lazily, so the callback would page-fault the first time it writes each page.
- Control→RT commands use single atomics (gains, delay) or request/acknowledge counters (flush). The RT side never waits.
- The translated bus is mono and never steps between signal and silence: a flush fades out the head of what it discards, a bus that runs dry ramps its last sample to zero, and resumed audio fades in, each over `fade_frames` (5 ms). A flush discards only audio queued before the call.
- Invalid or discontinuous timestamps are reported (`error` with `host time invalid`), never fabricated.
- JSON serialization, WAV writing, and resampling for inference run on ordinary threads.

## Portability rules

- Every text `open()`, `read_text`/`write_text`, and text-mode `subprocess` call names `encoding="utf-8"`; binary opens name none. Both pytest configs turn `EncodingWarning` into an error under `PYTHONWARNDEFAULTENCODING=1` (`scripts/check.py` sets it). A UTF-8 code page on a developer machine hides these bugs.
- Text files are LF in every checkout (`.gitattributes`); golden traces are compared byte for byte.
- No hard-coded interpreter or tool paths (`/usr/bin/python3`): use `sys.executable` or `shutil.which`. Host clocks come from `perf_counter_ns` where `mach_absolute_time` does not exist.
- Under Windows (WDDM) a CUDA kernel launch costs ~25 µs, so eager PyTorch decoding is launch-bound: prefer CTranslate2 or CUDA graphs and measure launches with the profiler. Oversubscribed GPU memory pages to system RAM and stalls every worker instead of failing, so run inference under `torch.inference_mode()`, return transient blocks (`empty_cache`), and watch `nvidia-smi` during live runs.

## Audio, permissions, and test tiers

- **Hosted / ordinary CI (deterministic):** `scripts/check.sh`. This covers pure contracts, golden traces, RT-shim unit tests, harness metrics, and offline plus accelerated paced replay with fixture adapters.
- **Interactive Mac (provisioned runner):** capture, mute, lifecycle (`a2a-tapctl`), Apple speech/translation runtime, model inference, and loopback latency. These need a logged-in session, devices, and permissions. Never pretend they are headless checks.
- Capture permission ("System Audio Recording") attaches to the *responsible* process, normally the terminal app. `AudioDeviceStart` on a tap aggregate blocks in coreaudiod until the user answers the consent prompt. Never start a tap on the main/UI thread before checking permission. `a2a-tapctl permission` reports granted/denied/undetermined, and `a2a-tapctl` refuses to start a tap when the answer is denied and prints a hint after 5 s of waiting. The check uses the private `TCCAccessPreflight` SPI through `dlopen`, so it belongs only in dev tools and is on the release-review list.
- Taps the product creates stay private (`TapSessionOptions.privateTap = true`). A **public** tap outlives a `kill -9`'d creator: coreaudiod keeps it registered until another process calls `AudioHardwareDestroyProcessTap` (A3, `reports/week2/raw/a3*.json`). Tests that need a public tap destroy what they leave behind (`lifecycle` does this itself; `a2a-tapctl destroy-tap --uid` for manual cleanup).
- Tools embed an `Info.plist` with stable bundle IDs (`dev.a2a.tapctl`, `dev.a2a.preflight`, `dev.a2a.pipeline`). FixturePlayer is `dev.a2a.FixturePlayer`. Permission identity is part of reproducibility.
- Meeting acceptance uses wired headphones (D3). Built-in speakers are only a playback test.

## Data handling

- Default product diagnostics contain no text or audio. `--research-text` adds text to traces for research runs only (D9).
- Raw participant audio, transcripts, voice prompts, and full text traces stay in the restricted research store or `.local/` (gitignored). Reports in git hold aggregate metrics and fixture IDs.
- The default capture is the **whole output mix** (`--tap-global`), so a recording holds every sound the machine makes, not one app's. Consent must cover that; `--source-out` audio is restricted research data, and a background voice in the mix is still a participant ([report](reports/week2/system-audio-source.md)).
- The delayed picture (ADR-0012) holds encoded frames of the chosen window in memory only, for at most the delay; nothing is written, traced or sent. Screen content is participant data like the audio mix.
- Agents and external LLM judges never receive corpus audio or transcripts whose consent does not cover it. Test fixtures are synthetic (`a2a_harness.synth`) or consented.
- Commit-policy tuning is an optimization loop against harness metrics on the **dev** set. The held-out set is never used for tuning.

## Models and licenses

- `models/manifest.yaml` is the only source of model provenance. Never invent revisions, hashes, or license verdicts; leave them `null` until a named verifier records evidence.
- Only enabled rows named explicitly are fetched. Fetches resume, verify size and SHA-256, and promote atomically. HY-MT stays restricted (no product use, no CI download, no distillation).
- Runtime downloads are disabled. Apple speech and translation assets are installed only by the setup path (`a2a-preflight --install-speech-assets` is the explicit speech step).
- Speech assets are reserved per app (at most 5 locales each). A tool that finds a locale `supported` reserves it, which attaches assets the setup path already installed without downloading; if a download would still be needed it releases the reservation and refuses (`AppleSpeechRecognizer.checkInstalled`).
- Multi-speaker sessions (`--speakers auto`, ADR-0005) track speakers with DSP voiceprints (no model). Voiceprints stay in memory; traces carry speaker IDs and spans only. A unit is not voiced with another speaker's voice: it waits for its own speaker's, then is skipped — with one measured exception. Unlabeled speech that *continues* the previous actor's turn inherits them: no pause since the last label (`--speaker-carry`, 6 s), or a piece of at most `--speaker-carry-piece` (1 s) starting within `--speaker-carry-pause` (3 s) after it. Carries never run backwards, and `speaker_carried` marks them in the trace (reports/week2/live-session-recovery.md).
- Plumbing adapters (`ToneSynthesizer`, `SystemVoiceSynthesizer`) set `preservesSpeakerIdentity = false`. Reports mark such runs **plumbing only**, and they never count as identity or gate evidence.
- `CloneVoiceSynthesizer` sets `preservesSpeakerIdentity = true` because it conditions on the speaker, but its model is `research_only` and unqualified: summaries carry `tts_identity_qualified: false`, and `harness` refuses `gate_valid` without it. Reference audio and transcripts stay in process memory by default; the worker never writes them and prints no text unless `--research-text` is set. The user's explicit "remember voices" request adds opt-in `--voice-memory DIR` / **Remember voices for this source**: the Swift host stores reference PCM, transcript, and voiceprint in a private `.local/voice-profiles/` source directory (0700; files 0600), with at most 16 identities and two references each. Reuse requires a conservative voice match; session IDs never select a saved voice. **Forget saved voices** deletes that source's store. These references remain restricted research data, never diagnostics or git content. Cloning a live meeting participant requires their consent (release review).

## Priority review before external release

Callback safety, tap/device lifecycle, XPC identity checks and entitlements, signing and notarization, private SPI use (the TCC preflight in `A2AAudio/CapturePermission.swift`), and license/data decisions.
