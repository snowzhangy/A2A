# Bidirectional translation — 25 October 2026

The development app now selects Mandarin → English or English → Mandarin before a
session. Changing direction clears captions and asset readiness, then checks the selected
source speech locale and translation pair. The direction stays fixed while a session runs.
Setup downloads remain explicit.

One shared direction value configures source recognition, translation, target validation,
system voice, cloned-voice language, duration estimates, and diagnostic language pair.
English source length limits prefer complete words, including contractions. Mandarin
voice warm-up and duration learning use Mandarin text and cadence estimates. Missing
system voices report an error instead of silently selecting a default language.

The CLI and isolated probes accept `--direction zh-en` (default) or `--direction en-zh`.
The cloned-voice worker accepts an optional `language` in its prepare request, defaulting
to English for existing clients. Mandarin generation gets a larger duration/token budget
per character. The model remains research-only and identity remains unqualified.

## Verification

- `scripts/check.sh`: passed, 139 executed Swift tests and 60 harness tests. The two
  opt-in Apple adapter tests remain skipped in this deterministic command. All seven
  executable SDK stamps passed and the model manifest has no problems.
- `source scripts/env.sh; swift test --package-path apple`: passed again after the final
  English word-boundary adjustment; golden traces passed without regeneration.
- `uv run --project harness pytest harness/tests/test_runner.py -q -k 'english_to_chinese or unknown_direction'`:
  four passed after that adjustment.
- `scripts/build_app.sh`: optimized app built, local signatures verified.
- `scripts/smoke_app.sh`: passed. The synthetic native window verifies direction switching,
  readiness reset, and both playback choices. Evidence: `.local/runs/app-smoke.yIz09x/app.json`.
- `scripts/smoke_app.sh --screenshot`: native evidence succeeded, but optional screenshot
  capture failed with `could not create image from window`. No screenshot verification
  is claimed.
- `git diff --check` and the app Info.plist validation passed.

Separate interactive checks used the installed Apple models, synthetic system speech,
and files under `.local/probe/bidirectional/`, without capture or device playback:

| Check | Result |
| --- | --- |
| English recognition, `asr --direction en-zh` | Two finalized results |
| Mandarin recognition, `asr --direction zh-en` | One finalized result covering the sample |
| Translation, `mt` in each direction | Two completed items per direction |
| System speech, `tts` in each direction | Nonempty WAVs for both items |
| Complete Apple pipeline, English → Mandarin | Two source commits, two target commits, two completed audio units; zero errors, rejected PCM, catch-up skips, or underruns |

The complete-pipeline command was:

```sh
apple/.build/debug/a2a-pipeline \
  --input .local/probe/bidirectional/tts-en/speech.wav \
  --clock host --pace realtime --asr apple --mt apple --tts system \
  --direction en-zh --translation-only --fluent --events-stdout --tail-seconds 8 \
  --trace .local/probe/bidirectional/reverse-pipeline.jsonl \
  --out .local/probe/bidirectional/reverse-pipeline.wav
```

These short synthetic checks establish functional wiring. Live meeting quality, extended
device testing, and real Mandarin cloned-voice quality remain separate qualifications.
