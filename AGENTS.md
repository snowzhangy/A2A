# AGENTS.md — A2A contributor rules

A2A captures another app's Mandarin audio on macOS and plays English in the original speaker's voice, all locally. Start with [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) (execution baseline, v2.1), then the ADRs in [docs/adr/](docs/adr/). For what works today and what is still pending, see [reports/week2/status.md](reports/week2/status.md).

These rules apply to people and coding agents. Routine local implementation and testing need no extra approval. Review priority (below) is about what gets a careful second look before external release, not a ban on working in those areas.

## Toolchain

- Use full Xcode. On the reference Mac, `xcode-select` points at Command Line Tools, and that SwiftPM install cannot link package manifests. `scripts/env.sh` exports `DEVELOPER_DIR` to an installed Xcode (currently `/Applications/Xcode-27.0.0.app`) without touching `xcode-select`. Every script sources it. For manual commands, run `source scripts/env.sh` first.
- The deployment target is macOS 26.0 on Apple silicon. Guard 26.4+ APIs such as the `preferredStrategy:` overload of `TranslationSession`.
- Executables must record the SDK they were built with. Swift Build stamps the deployment target (`sdk 26.0`) into `LC_BUILD_VERSION`, and Translation gates on that value: a 26.0-stamped binary sees installed zh-Hans→en languages as `supported` and throws `notInstalled`. `sdkVersionStamp()` in `apple/Package.swift` writes `A2A_SDK_VERSION` (exported by `scripts/env.sh`), and `scripts/check.sh` fails on a mismatch.
- Python tooling uses `uv`. `harness/` is a uv project. The heavy evaluation models (COMET, Whisper, UTMOS, speaker verifiers) belong in `research/`, so ordinary checks never resolve torch.

## Commands

| What | Command |
| --- | --- |
| Everything deterministic | `scripts/check.sh` (`--quick` skips the harness↔pipeline integration) |
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
| Tap tools (A1–A3) | `a2a-tapctl permission\|list\|devices\|resolve\|taps\|destroy-tap\|capture\|monitor\|fidelity\|lifecycle` (see the header of `apple/Tools/a2a-tapctl/main.swift`) |
| Fidelity reference | `FixturePlayer --probe --seconds N --write-reference ref.wav` saves exactly what it plays, for `a2a-tapctl fidelity --reference ref.wav --capture x.wav` |
| Live translation of an app | `a2a-pipeline --tap-bundle ID\|--tap-pid N --asr apple --mt apple --tts system [--seconds S] --trace t.jsonl --source-out s.wav --translated-out t.wav` (Ctrl-C stops and restores the app's audio) |
| Cloned-voice translation (research-only model) | add `--tts clone` to any real-adapter run: the resident `research/tts_worker.py` streams Qwen3-TTS in a voice learned from the source's first finalized utterances (`--voice-ref ref.wav --voice-ref-text ref.txt` for a fixed reference; `--clone-mode xvector` skips the transcript). Needs `uv sync --project research --locked` and the fetched `qwen3-tts-12hz-0.6b-base`. Runs report `tts_identity_qualified: false` and never count as identity or gate evidence. |
| Live demo + check (interactive) | `scripts/live_demo.sh [--max-debt-seconds 6 --max-source-lag-seconds 8]`: ffplay plays a Mandarin video, the pipeline translates it live, then `a2a_harness.live_check` checks every sentence and the back-transcript |
| Mandarin demo clip | `uv run --project harness python -m a2a_harness.zh_demo --out .local/fixtures/zh-demo --lead 4` (system zh_CN voice, synthetic) |
| Back-transcription | `a2a-transcribe --input x.wav [--locale en-US]` (installed SpeechTranscriber locale) |
| FixturePlayer app | `scripts/build_fixture_player.sh`, which produces `.local/apps/FixturePlayer.app` (`dev.a2a.FixturePlayer`) |
| Model manifest | `uv run --project harness python scripts/fetch_models.py --check\|--list\|--models ID\|--notices` |

Every ticket states its DoD as a runnable command. Run it before you call the work done, and report failures with their output.

## Module boundaries

| Module | Owns | Must not |
| --- | --- | --- |
| `A2ACore` | Types, commit policy, state machine, playout gate, `SessionPipeline` reducer, trace event values | Do I/O, import Apple media frameworks, or call models. Clocks and events are injected. |
| `A2ARTShim` (C) | SPSC rings, timing records, the IOProc, the two-bus mixer | Allocate, lock, log, or call Swift/ObjC on the callback path |
| `A2ATrace` | JSONL writer, draining RT records into trace events | Run on an RT thread |
| `A2AAudio` | Process taps, the private aggregate device, source resolution, `TapSession` | Make policy decisions (those belong in `A2ACore`) |
| `A2AMedia` | WAV, resampling, signal measurements | Run on an RT thread |
| `A2AModels` | Adapters: fixture fakes and Apple frameworks (third-party models are added per selected combination) | Download at runtime (D8) |
| `Tools/*`, `Apps/*` | Drivers that execute `SessionPipeline` effects | Duplicate policy logic |

The reducer pattern is the contract. Drivers feed inputs with their event time into `SessionPipeline.handle`, then execute the returned effects. Golden tests, replay, and the live host therefore exercise identical decisions.

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
- Agents and external LLM judges never receive corpus audio or transcripts whose consent does not cover it. Test fixtures are synthetic (`a2a_harness.synth`) or consented.
- Commit-policy tuning is an optimization loop against harness metrics on the **dev** set. The held-out set is never used for tuning.

## Models and licenses

- `models/manifest.yaml` is the only source of model provenance. Never invent revisions, hashes, or license verdicts; leave them `null` until a named verifier records evidence.
- Only enabled rows named explicitly are fetched. Fetches resume, verify size and SHA-256, and promote atomically. HY-MT stays restricted (no product use, no CI download, no distillation).
- Runtime downloads are disabled. Apple speech and translation assets are installed only by the setup path (`a2a-preflight --install-speech-assets` is the explicit speech step).
- Speech assets are reserved per app (at most 5 locales each). A tool that finds a locale `supported` reserves it, which attaches assets the setup path already installed without downloading; if a download would still be needed it releases the reservation and refuses (`AppleSpeechRecognizer.checkInstalled`).
- Multi-speaker sessions (`--speakers auto`, ADR-0005) track speakers with DSP voiceprints (no model). Voiceprints stay in memory; traces carry speaker IDs and spans only. A unit is never voiced with another speaker's voice: it waits for its own speaker's, then is skipped.
- Plumbing adapters (`ToneSynthesizer`, `SystemVoiceSynthesizer`) set `preservesSpeakerIdentity = false`. Reports mark such runs **plumbing only**, and they never count as identity or gate evidence.
- `CloneVoiceSynthesizer` sets `preservesSpeakerIdentity = true` because it conditions on the speaker, but its model is `research_only` and unqualified: summaries carry `tts_identity_qualified: false`, and `harness` refuses `gate_valid` without it. Reference audio and transcripts stay in process memory by default; the worker never writes them and prints no text unless `--research-text` is set. The user's explicit "remember voices" request adds opt-in `--voice-memory DIR` / **Remember voices for this source**: the Swift host stores reference PCM, transcript, and voiceprint in a private `.local/voice-profiles/` source directory (0700; files 0600), with at most 16 identities and two references each. Reuse requires a conservative voice match; session IDs never select a saved voice. **Forget saved voices** deletes that source's store. These references remain restricted research data, never diagnostics or git content. Cloning a live meeting participant requires their consent (release review).

## Priority review before external release

Callback safety, tap/device lifecycle, XPC identity checks and entitlements, signing and notarization, private SPI use (the TCC preflight in `A2AAudio/CapturePermission.swift`), and license/data decisions.
