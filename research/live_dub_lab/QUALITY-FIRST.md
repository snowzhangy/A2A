# Quality first, with a live source

This records the earlier complete-phrase default. The user's subsequent feedback
supersedes it: see [restored soundtrack and reduced delay](SOUNDTRACK-AND-DELAY.md).

The user's priority is complete, accurate speech before low delay, while retaining
live processing. The app now defaults to **Prioritize complete phrases** (ADR-0014).
The same reducer and adapters serve the app, file-paced replay, and real process taps.

`--quality-first` waits until each phrase has finished synthesis before admitting its
audio, so temporary GPU stalls cannot exhaust the bus during that phrase. It disables
optional early connective/pause splits, allows 20 seconds of estimated debt/source
lag, and retains at most 16 pending units. Limits remain explicit: overload can still
skip unheard work. At the hard queue cap, quality mode discards unheard work without
flushing the phrase already on the bus. Model/service failures still trigger recovery.

No future transcript or pre-generated speech is used. The synchronized test player
opens an audio device, waits on stdin while the pipeline prepares, then plays when
the process tap and models report ready. The helper captures and translates that
audio as it arrives. Voice references are acquired from captured speech.

## Actual live-tap evidence

On the user's 37-second scene, `quality-tap-01` completed **13 of 13 committed units**,
with no underruns, capture overflow, dropped PCM or trace-record drops. Median
source-onset to rendered-onset delay was **16.17 seconds**, maximum 19.09 seconds.
It used Apple ASR/MT and Qwen cloned speech with the series glossary and pronunciation
files. The tap was private and muted; rendered output was recorded from the mixer.

This is a real process-tap playback test. The output device was the currently selected
AirPods. It is not the wired-headphone acceptance configuration, physical loopback
latency, a sustained-session result, an identity qualification or dub-quality proof.
Completion coverage concerns committed source spans; it does not establish that every
word of the original source was recognized or correctly translated. Two tracker
identities were detected for three annotated actors. Translation inspection still
found wrong subjects, pronouns and ASR-derived terminology.

The preceding `quality-qwen-01` file-paced replay also completed 13/13, with 14.74 seconds
median onset delay and no underruns. It used an earlier CLI build before quality mode
also selected the fluent worker's temperature setting; do not treat these runs as a
controlled file-versus-tap comparison. GPU load varied.

Actual recorded playback: `.local/live-dub-lab/quality-tap-01/preview-timeline.mp4`.
It uses the reference video's picture and `rendered.wav`, retains silences and delayed
final speech, and holds the final frame while late speech finishes. Picture alignment
is approximate; the measured timing comes from trace samples, not from this preview.

## Contextual translation research

Two already cached checkpoints, Qwen3 4B Instruct 2507 and Qwen3 30B-A3B Instruct 2507
(both MLX 4-bit conversions), were registered in the main model manifest. The pinned
source/conversion revisions, model-card Apache-2.0 evidence and hashes of the inspected
local artifacts are recorded there. No model weights were downloaded in this work.
They remain research-only and unqualified. The contextual lab has a separate locked
environment using mlx-lm 0.32.2; normal checks never import these models.

The first text-event replays used actual ASR commits from the live tap, rather than
the human-written dubbing script. Both models improved some wording but produced
other errors. Passing every glossary term also introduced irrelevant vocabulary.
The current prompt filters terminology, uses up to eight preceding source fragments,
and excludes earlier generated English to reduce error propagation. It receives no
future lines. The local development series file additionally corrects three observed
ASR errors explicitly. Those corrections were tuned on this scene and cannot count
as held-out evidence or automatic ASR improvement. Context and corrections improved
several inspected lines; wrong pronouns and number agreement still occur.

`--mt context --context-python PY --context-worker FILE [--context-series LOCAL_JSON]`
connects the resident translator to the actual pipeline. Startup verifies model bytes,
loads and warms the model before capture starts. JSONL requests/responses are bounded;
the host caps in-flight requests, rejects late canceled responses, and closes/terminates
the worker on shutdown. The adapter is Mandarin-to-English research functionality;
the app still uses Apple Translation. No runtime model download or external inference
service is used. The attempted contextual live-tap run exceeded the two-minute startup
limit while another independent GPU pipeline was running; the synchronized player
never began source playback. This attempt provides no live translation-quality result.
An orphaned worker was observed after the timeout and exited before explicit cleanup;
the worker now watches for parent loss even during model loading, with a regression
test covering that startup condition. The passing live result above uses Apple MT.

## Reproduce and validate

```sh
python3 -m unittest discover -s research/live_dub_lab -p 'test_*.py' -v
scripts/check.sh
scripts/build_app.sh
scripts/smoke_app.sh

# Interactive Mac: existing capture permission, real audio-device playback.
python3 research/live_dub_lab/run_quality_tap.py \
  --input .local/dub-comparison/scene-37.wav --out .local/live-dub-lab/quality-tap-NEW

# Contextual research variant; series file is optional and private.
uv sync --offline --locked --project research/live_dub_lab
python3 research/live_dub_lab/run_quality_tap.py --context \
  --series .local/live-dub-lab/condor-series-dev.json \
  --input .local/dub-comparison/scene-37.wav --out .local/live-dub-lab/quality-context-tap-NEW
```

All raw audio, full text, series metadata, references and traces remain in `.local/`.
The next quality work is reliable turn/speaker attribution and better ASR/translation;
completion and low word error from TTS cannot establish semantic or dubbing quality.

Final validation logs: `check-quality-final-20261027.log`, `build-quality-final-20261027.log`
and `smoke-quality-final-20261027.log` under `.local/live-dub-lab/`. A cancellation test
initially relied on wall-clock sleeps and failed under parallel load; it now gates the
fake worker response explicitly and verifies a late canceled reply cannot complete the
next request. Deterministic worker tests load no models or Apple runtime assets.
Final checks passed: 180 executed Swift tests (two Apple runtime tests skipped),
83 harness tests, eight lab tests, and SDK/model manifest gates (15 entries, six enabled).
The optimized app build and synthetic startup smoke passed.
