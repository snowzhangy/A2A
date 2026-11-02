# A2A

The native application below targets macOS. An experimental Windows desktop preview is
under `runtime/`; it has not passed live-audio lifecycle or voice-quality acceptance.
See [the portable runtime guide](runtime/README.md) for setup and current limitations.

Local Mandarin ↔ English translation on Apple silicon, targeting macOS 26 or later.

The development app provides translated audio and captions from another app. Choose
**Mandarin → English** or **English → Mandarin** before starting a session.
Use **Listen to → Original / Translated** to switch audio before or during a session.
Translation and captions keep running while you listen to the original. **Translated**
is selected by default and stays silent during preparation/recovery when translation is
unavailable. Phrases play back to back and are never cut off mid-phrase: when translation
falls behind the speaker, speech is paced up to 1.3× faster, and phrases nobody
has heard yet are skipped instead of pausing playback to recover (`a2a-pipeline --fluent`).
With **Match picture to translated speech** on, a line that comes out shorter than the
speech it translates is slowed so it ends where the actor stops, within a bounded stretch
([ADR-0013](docs/adr/0013-turn-timing-fill.md)).
The development app defaults to experimental Qwen voice cloning; disable the original
speaker option to use system voices. Each speaker in a dialogue gets their own voice: a
speaker tracker tells voices apart by their spectral envelope, and each speaker is cast a
distinct installed voice for their pitch (`a2a-pipeline --speakers auto`,
[ADR-0005](docs/adr/0005-multi-speaker-voices.md)). Different-gender speakers separate
reliably; similar voices of the same gender may still share one. The
**experimental** option speaks the target language in each source speaker's own voice, learned
from their first seconds of speech. Choose **Qwen3-TTS**, **Audio8 Realistic**,
**OmniVoice**, or **Chatterbox Multilingual**. These models are research-only and
speaker identity is **not qualified**. Actual process-tap research
runs, including their latency and coverage limits, are recorded in the
[live lab](research/live_dub_lab/SOUNDTRACK-AND-DELAY.md).
Each cloned speaker keeps the same prepared reference for the session. Lines without
sufficient speaker labels wait within the source-lag budget and may be skipped;
the tracker can still confuse actors. See [voice consistency](research/live_dub_lab/VOICE-CONSISTENCY.md).
Use it only with speakers who consented.
The full project's acceptance gates remain open; see [the current decision](reports/week2/decision.md).

Build and open the native app:

```sh
scripts/build_app.sh
open .local/apps/A2A.app
```

In the app, choose a direction, enter the Hy-SimulMT server address, and select
**Check server**. Choose an app playing the source language, then select **Start
translation**. Source audio is sent over the local network to Hy-SimulMT, which
returns translated text. The selected voice model turns that text into speech using
a short reference for each detected speaker. Qwen and Chatterbox use an audio-only
reference; Audio8 and OmniVoice locally transcribe the reference once per speaker.
This uses the installed Apple speech assets, not continuous transcription; an explicit
setup button installs them if needed. Apple Translation is not used. Direct audio
is segmented into short chunks, so Chinese
source captions are unavailable and speech boundaries can affect translation.

**Match picture to translated speech** (on by default with Hy-SimulMT) fixes the gap
between the English and the picture. Start asks which window shows the video; A2A then shows
that window in its own window 8 seconds late (3–20 s, set before starting) and delays every
translated phrase, the original audio and the music and effects by the same amount, so each
phrase starts on its own picture ([ADR-0012](docs/adr/0012-delayed-picture-sync.md)). Watch
A2A's window and keep the source window visible: some players stop drawing hidden windows.
Protected (DRM) video captures as black. **Fine-tune** moves only the picture by up to
±0.5 s during a session, for your output device. Captions still appear as soon as they are
translated.

Allow System Audio Recording when macOS requests it. Stop ends capture and clears
the session's captions. Stopping returns audio ownership to the source app. The
server processes segments and may cause lag or skipped phrases if overloaded.
Translation terminology files are not supported by the Hy adapter. The optional
pronunciation file maps translated words to their spoken form; captions are unchanged.

The app checks whether the selected voice model is installed and offers an explicit
install action. Qwen streams speech and starts two resident workers and keeps at most one future phrase in the back
buffer while the current phrase plays. Audio8, OmniVoice and Chatterbox currently
generate each complete phrase before playback. Two Qwen workers increase memory use and do not bypass
missing speaker references or slow translation. Voice references stay in memory for
the session. Previously saved voice references are not reused by the direct pipeline;
if any exist, the app still offers **Forget saved voices** for the selected source.

The build script uses full Xcode through `scripts/env.sh`, builds optimized code
(`--debug` builds a debuggable app), bundles the translation helper, and signs the local app. The default signature is ad hoc for development;
this is not a notarized distribution build. No third-party model weights are fetched
by building or starting a session. Model installation occurs only through explicit setup.

Verify the deterministic implementation:

```sh
scripts/check.sh
```

This builds every product, checks executable SDK stamps, runs Swift and harness
tests, verifies model provenance rules, and replays synthetic fixtures offline and
with paced input. Apple model runtime tests require an explicit opt-in and are
skipped by default. Native UI startup can be checked on a logged-in desktop without
audio capture or inference:

```sh
scripts/build_app.sh
scripts/smoke_app.sh
```

`scripts/smoke_app.sh --screenshot` additionally requests a window screenshot and
requires macOS screen capture access. The startup check writes its evidence under
`.local/runs/` and uses synthetic display text.

For CLI commands, module boundaries, data rules, and interactive acceptance checks,
read [AGENTS.md](AGENTS.md). The execution baseline is
[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md); ticket history is in
[reports/week2/status.md](reports/week2/status.md).

Run model stages independently without starting a capture/translation workflow:

```sh
scripts/build_stage_probe.sh
apple/.build/debug/a2a-stage-probe asr --input source.wav --out .local/probe/asr --chunk-ms 20 --realtime
apple/.build/debug/a2a-stage-probe mt --input source-items.json --out .local/probe/mt
apple/.build/debug/a2a-stage-probe tts --input .local/probe/mt/result.json --out .local/probe/tts
apple/.build/debug/a2a-stage-probe resample --input source.wav --out .local/probe/resample
```

Text input is `{"items":[{"id":1,"text":"..."}]}`. Output directories must be new.
The ASR and MT probes retain Apple adapters for offline comparison only; they are not
part of the live app. These probes use installed models, write research text/audio to the explicit output
directory, and never play audio. Keep their output in `.local/` or restricted storage.
The pipeline and stage probes accept `--direction en-zh` for English → Mandarin;
`--direction zh-en` is the default. Pass the same direction to each stage of a probe run.
See [the real-source stage diagnosis](reports/week2/stage-diagnosis.md).
