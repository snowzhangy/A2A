# Cloning silence, delayed responses, and saved references — 25 October 2026

The user reported silence or very late Qwen3-TTS playback and authorized recording the
test video. They also requested remembering voices across sessions and supplied
[dub-anything](https://github.com/swax/dub-anything) as a reference.

## Observed failure

An operational snapshot of the user's active Qwen session covered about 198 seconds.
Of 83 committed units, only 33 completed playback. Thirty units expired without a
speaker profile; sixteen were skipped while catching up and one expired as stale.
The first translated playback started at about 58 seconds. The first S1 profile was
ready at about 54 seconds. Recent Qwen first-chunk delays were mostly 0.14–0.27 seconds;
there were no model errors in that snapshot. Reference collection and playback policy
were therefore major contributors to this run's silence, rather than a stopped model.

The first direct recording attempt found the selected player no longer sending audio.
It produced no source samples and reported `capture stalled`; it is not test evidence.
The saved 120-second dialogue fixture was then played through ffplay and captured through
a process tap, recording about 91.5 seconds. The source, translated stream, actual rendered
output, transcripts, and traces remain in `.local/runs/clone-reliability/`.

## Fixes

- Short replies can reuse a confidently matching known speaker with less voiced evidence;
  they still cannot create a new identity from a brief blip.
- Speaker purity for references uses recognizer token timing, excluding padding added
  for audio context. Clean short turns of at least 0.3 seconds can accumulate a reference.
- Concatenated reference transcripts retain word boundaries.
- Catch-up no longer continually cancels the oldest synthesis refilling an empty bus.
  Later unheard backlog is skipped instead. Hard queue limits remain enforced.
- A failed initial profile resets that speaker's reference collection so it can retry.
- The app displays collecting/preparing/ready state per tracked speaker.
- Opt-in voice memory stores private local reference clips and transcripts and reuses
  them only after a conservative speaker match. It does not retrain model weights.
  Initial and upgraded exemplars are retained together; one no longer overwrites the
  other, and two exemplars of one person do not create a false ambiguity.

## Recorded comparison

The new pipeline replayed the same captured audio in real-time file mode. The following
are diagnostic observations from single stochastic runs, not held-out acceptance metrics
or a controlled acoustic-loopback latency measurement:

| Metric | Captured baseline | Updated, collecting references | Initial saved-reference replay |
| --- | ---: | ---: | ---: |
| Committed / played units | 25 / 18 | 25 / 20 | 25 / 19 |
| First translated audio, source timeline | 5.58 s | 5.38 s | 5.34 s |
| Source-onset lag, median | 4.61 s | 4.59 s | 4.64 s |
| Source-onset lag, p90 | 13.43 s | 12.56 s | 12.98 s |
| Catch-up skips | 6 | 4 | 5 |
| Runtime errors / underruns | 0 / 0 | 0 / 0 | 0 / 0 |

The initial saved-reference replay reused S2's reference. S1 was conservatively rejected
and recollected. That test exposed the first-exemplar overwrite issue; preserving both
reference versions was added afterwards and verified by regression tests. No new real
replay of that final exemplar change is claimed while the user tests the app.

These results show fewer omitted units on the sample, but latency remains substantial.
Saved references do not remove recognition/commit waiting, translation, or spoken-output
backlog, and uncertain matches intentionally collect speech again.

## Voice likeness and audition

Six original/cloned pairs for the two tracked speakers are available in the local
`comparison.html` and `audition/` directory. They contain the source and actual rendered
cloned English for matching units. This assistant session cannot accept audio input, so
no subjective listening verdict is claimed. Similar same-gender actors may still merge
in the DSP tracker; voice identity remains unqualified. Background music is still part of
the captured reference because source separation is not implemented.

The linked project's useful patterns are reusable actor references, source/background
separation, and checking generated lines. It operates on complete videos and can rerender
lines; that workflow does not establish low-latency live dubbing for this app.

## Verification

- `scripts/check.sh`: 166 executed Swift tests, 74 harness tests, all executable SDK
  stamps, and model manifest validation passed before the final exemplar enhancement.
- Final `swift test --package-path apple`: 167 executed tests passed, with the same two
  opt-in Apple adapter tests skipped. Golden traces passed without regeneration.
- Final targeted cache integration: process restart and saved-voice reuse passed.
- Saved-reference regressions cover matching despite reordered session IDs, rejecting
  different/ambiguous voices, corrupt files, 0600 permissions, bounded retention, and
  keeping both reference exemplars without false ambiguity.
- `scripts/build_app.sh`: final optimized app built and signatures verified; the updated
  app was launched for the user. No new capture or inference was started after launch.
- Evidence logs: `.local/clone-reliability-check.log`, `.local/clone-reliability-swift-final.log`,
  `.local/clone-reliability-cache-integration.log`, `.local/clone-reliability-app-build.log`.
