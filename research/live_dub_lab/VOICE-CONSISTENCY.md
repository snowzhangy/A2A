# Returning-actor voice consistency

The user's returning-actor report exposed reference replacement and attribution errors.
The earlier responsive/background tap prepared two versions per tracked speaker. The
waiter's later turns changed from profile 1 to profile 2; S1 also changed after a third
actor had been merged into that identity. Cloned sessions now retain one prepared
reference per speaker, isolate Qwen conditioning caches, and reset sampling per voice.
ADR-0009 records the decision and the bounded wait for missing speaker labels.

## Matched local model check

Two manually selected actors and their directed text/reference pairs were generated
in order A/B/A/B using the resident local Qwen model at temperature 0.6. Both returning
samples had exactly the same frame count and float waveform as that actor's first
sample: maximum absolute difference 0, correlation 1.0. The two phrase durations were
6.8 and 2.8 seconds. This verifies repeatability on identical inputs despite another
actor intervening. It does not measure different-line voice likeness, automatic actor
recognition, live performance, or equivalence to dub-anything. Generation times varied
with concurrent machine load and are not a speed benchmark.

Private evidence: `.local/live-dub-lab/voice-consistency-01/`.

```sh
uv run --offline --locked --project research python research/live_dub_lab/qwen_voice_consistency.py \
  --plan .local/dub-comparison/plan.json --out .local/live-dub-lab/voice-consistency-NEW
```

## Actual live process tap

The same synchronized 37-second scene used Apple ASR/MT, Qwen, DSP tracking, adaptive
buffering, separated background, and the existing 8-second debt/source-lag settings.
The selected output was AirPods; physical/Bluetooth presentation delay is unmeasured.
No reference or transcript was supplied to this live acquisition run.

| Measurement | Result |
| --- | ---: |
| Committed phrases completed | 11/13 |
| Completed committed-source coverage | 91.1% |
| Rendered onset delay, median / max | 5.61 / 9.75 s |
| Underruns / dropped PCM / capture overflow | 0 / 0 / 0 |
| Prepared profiles | S1 v1, S2 v1; no replacements |
| Background separation failed | false |

One phrase was skipped for catch-up and one unlabeled shout was skipped instead of
using a guessed actor. The earlier responsive run completed 12/13 at 4.87 seconds
median, 6.29 maximum, and 90.7% duration coverage. These single runs have different
ASR segmentation and nondeterministic load; the new run does not establish a latency
improvement. Both voices remained pinned on later returning turns. Three annotated
actors still map to only two tracker identities, so actor assignment remains a material
limitation. Dub-quality live translation and new-episode recognition remain unachieved.

Private evidence: `.local/live-dub-lab/voice-locked-tap-01/`, including actual source,
separated background, post-gain rendered output and traces. `preview-timeline.mp4`
retains pauses and delayed final speech from `rendered.wav`, with no audio added in
post-production. Its picture is approximate and holds the last frame through the tail.

```sh
python3 research/live_dub_lab/run_quality_tap.py --responsive-background --debt 8 --lag 8 \
  --input .local/dub-comparison/scene-37.wav --out .local/live-dub-lab/voice-locked-tap-NEW
```

## Validation

Regression coverage includes A/B/A/B with late profile upgrades, translations arriving
before labels, unrelated turns while waiting for a profile, missing/ambiguous labels,
expiry, discontinuity, codec-cache collisions, and intervening-actor sampling.
Required checks are `scripts/check.sh`, `scripts/build_app.sh`, `scripts/smoke_app.sh`
and the local lab's standard-library unittest suite. Models remain research-only and
identity-unqualified. Other concurrent experiments are left running; memory pressure
is checked before starting additional model work, and further inference waits when
there is insufficient available memory.

Final checks passed: 190 executed Swift tests (two Apple runtime tests skipped),
86 harness tests, eight lab tests, SDK/manifest gates, optimized local app build and
synthetic native startup smoke. The actual live trace also validates: 12,867 events,
no schema errors or reported trace drops. Logs are `check-voice-consistency-final-20261027.log`,
`build-voice-consistency-20261027.log` and `smoke-voice-consistency-20261027.log` under
`.local/live-dub-lab/`. Restart an existing translation session to load the rebuilt helper.
