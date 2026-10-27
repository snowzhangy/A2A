# A2A

Local Mandarin ↔ English translation on Apple silicon, targeting macOS 26 or later.

The development app provides translated audio and captions from another app. Choose
**Mandarin → English** or **English → Mandarin** before starting a session.
Use **Listen to → Original / Translated** to switch audio before or during a session.
Translation and captions keep running while you listen to the original. **Translated**
is selected by default and stays silent during preparation/recovery when translation is
unavailable. Phrases play back to back and are never cut off mid-phrase: when translation
falls behind the speaker, the system voice speaks up to 1.3× faster, and phrases nobody
has heard yet are skipped instead of pausing playback to recover (`a2a-pipeline --fluent`).
It uses system voices by default. Each speaker in a dialogue gets their own voice: a
speaker tracker tells voices apart by their spectral envelope, and each speaker is cast a
distinct installed voice for their pitch (`a2a-pipeline --speakers auto`,
[ADR-0005](docs/adr/0005-multi-speaker-voices.md)). Different-gender speakers separate
reliably; similar voices of the same gender may still share one. An
**experimental** option speaks the target language in each source speaker's own voice, learned
from their first seconds of speech. Choose **Qwen3-TTS**, **OmniVoice**, or
**Chatterbox Multilingual** in the experimental model selector. These models are
research-only and speaker identity is **not qualified**; measured in file-mode runs only,
not yet exercised in a live app session; see [voice-match.md](reports/week2/voice-match.md)
and [speakers.md](reports/week2/speakers.md).
Use it only with speakers who consented.
The full project's acceptance gates remain open; see [the current decision](reports/week2/decision.md).

Build and open the native app:

```sh
scripts/build_app.sh
open .local/apps/A2A.app
```

In the app, choose a direction, check languages, and install any missing speech/translation
languages through the setup buttons. Choose an app playing the source language, then
select **Start translation**. Stop the session before changing direction; readiness is
checked for the new source speech locale and translation pair.
Allow System Audio Recording when macOS requests it. Stop ends capture and clears
the session's captions. Stopping returns audio ownership to the source app.

For model comparisons, enable experimental voice cloning, select a model, and use
**Install model** if it is missing. The app shows the download size and blocks session
startup until the selected model is installed. Stop before switching models. Qwen streams
audio during generation; the current OmniVoice and Chatterbox adapters generate each
phrase before playback. Setup verifies pinned artifacts; session startup stays offline.
See [model switching](reports/week2/model-switching.md) for validation and limitations.

**Remember voices for this source** opts into a private local reference store. Saved voices
are reused only after a conservative speaker match, rather than by the session's speaker
number. The app shows collection/preparation progress. **Forget saved voices** removes the
selected source's references after stopping. This stores reference clips and transcripts;
it does not train model weights. See [the reliability review](reports/week2/clone-reliability.md).

The build script uses full Xcode through `scripts/env.sh`, builds optimized code
(`--debug` builds a debuggable app), bundles the translation helper, and signs the local app. The default signature is ad hoc for development;
this is not a notarized distribution build. No third-party model weights are fetched
by building or starting a session. Language downloads occur only through setup.

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
These probes use installed models, write research text/audio to the explicit output
directory, and never play audio. Keep their output in `.local/` or restricted storage.
The pipeline and stage probes accept `--direction en-zh` for English → Mandarin;
`--direction zh-en` is the default. Pass the same direction to each stage of a probe run.
See [the real-source stage diagnosis](reports/week2/stage-diagnosis.md).
