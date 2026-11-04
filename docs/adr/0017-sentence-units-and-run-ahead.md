# ADR-0017: Sentence units on the isolated voice, and synthesis that runs ahead of its slot

Status: accepted for local development on 2 November 2026. It addresses continuous synthesis,
timestamped playback, sentence-sized units, and latency.

Research and measurements are in
[reports/week2/sentence-units-and-slot-playout.md](../../reports/week2/sentence-units-and-slot-playout.md).

## Problem

A live app session ran for 605 s with D = 8 s, `--asr audio --dual-clone`. Timestamp playout (ADR-0012) worked:

- Phrases started a median 0.03 s from their slot.
- All 36 late starts had the same cause: the line before was still playing.
- No start was late because its audio was not ready.

Two other rules made the dub choppy and limited it.

1. **Units were 3 s pieces, not sentences.**
   - 74% of units closed at exactly 3.00 s. `AudioSegmentPolicy`'s fixed cap cut them mid-word, not at a pause.
   - The detector is an energy VAD at −40 dB on the mix. It reported 41 speech ends in 605 s, because the music and effects under the dialogue keep the mix above it.
   - Units that did close at a pause ended within 0.25 s of the actor twice as often as capped ones: 45% against 23%.
2. **Synthesis waited for playback.**
   - With `--dual-clone`, `canStartSynthesis` let a line start only once the line before was playing. 181 of 191 lines waited.
   - A line's slack (its slot minus its synthesis end) followed this model: (gap between consecutive source starts) − 0.28 s − (generation time), with a median error of 0.05 s.
   - So quick exchanges nearly missed their slots at any D.

## Decision

Active when `--asr audio` runs with a picture delay (`--presentation-delay-ms` > 0). `--no-sentence-units` restores the old segmenter for comparison.

1. **Pauses come from the isolated voice.**
   - The background separator (ADR-0008) already isolates the voice to build the bed. `BackgroundSeparator.separate` now returns that voice too.
   - The segmenter gets its own `EnergyVAD` on it.
   - Its threshold follows the signal (`AdaptiveSpeechThreshold`): halfway between the quiet floor and the speech level of the last 6 s, never below the session threshold less 5 dB.
   - In the first live test, the fixed −45 dB threshold found almost no pauses: 72 of 96 units ran into the cap.
   - Replayed drama with isolator residue simulated at −36 dB gives the fixed threshold 0 of 39 pauses and the adaptive one 35. On the clean replays the two agree.
   - Voice frames are moved back on the source timeline by the separator's reported latency: 4,440 samples, 92.5 ms. Spans and stream offsets stay in source samples.
   - The session's own VAD, the speaker tracker and the reducer still use the mix, so speaker tracking is unchanged.
   - If separation fails, the segmenter switches to the mix and its timeline only moves forward.
   - On the 120 s movie clip, the voice detector found 37 speech ends, against 22 on the mix, and changed little between −40 and −50 dB.
2. **A unit can be a whole sentence, bounded by the delay.**
   - `AudioSegmentPolicy.sentences(presentationDelaySeconds:)` caps a unit at D − 3.5 s, within 3–6 s: 4.5 s at D = 8.
   - On replays, about 2.5 s was enough after a unit closed: Hy's last turn (0.48 s p90), the voice's first playable audio (0.63 s p90), the detector's 0.3 s hangover, and a reserve.
   - The first live test at D − 2.5 s missed: with screen capture, Hy and two voices sharing the GPU, 5 s units finished synthesis a p10 of 1.05 s after their slot, and 10 of 96 started more than 0.5 s late. Hence 3.5 s.
   - A unit that reaches the cap is cut at the quietest frame of its last second (dub-anything's `dips`), never at a fixed sample.
   - Hy hears an open unit while it is spoken only up to `streamLimitSamples`, the cap less the dip window, because the cut can fall anywhere in that window.
   - Short phrases are not joined across pauses. Joining before both sides are labeled could voice two actors as one, and it costs a wait on every short line.
3. **Synthesis runs ahead.**
   - Under a picture delay, both clone workers start the next queued line whenever one is free.
   - The reducer already holds each phrase until its slot (ADR-0012), so translation and synthesis run continuously, and the reducer with the render callback plays each phrase by its timestamp.
   - Without a picture delay the old gate stays: a ready phrase plays at once, so running ahead would only make lines late.

4. **Mandarin lines are budgeted at the voice's measured pace.**
   - `estimatedSecondsPerTargetChar` for a Mandarin target drops from 0.25 to 0.20 s. It came from the initial import, unmeasured.
   - The cloned voice speaks 182–214 ms per character natively (median of five sessions) and can speed up 1.3×.
   - At 0.25, a 3 s line could hold 16 characters before it was shortened. In the first live test on sentence units, 23 of 40 lines were shortened, which drops translation.
   - At 0.20 the same line holds 20 characters: natively about 1 s longer than its span (the existing grace), or about its span at 1.3×.

The run summary reports `audio_units`: the cap, which detector ran (`voice`, `mix` or `session`), and how many units each rule closed.

## Consequences

- Live, same video, same D = 8 s. Three builds compared:

  | Build | Lines played | Late >0.5 s | Units at cap | Slack p10 | Voice ready after first heard |
  |---|---|---|---|---|---|
  | Before (fixed 3 s cuts on the mix), 605 s | 185 of 190 decided | not counted | 74% | +0.09 s | not measured; ready 3.5–248 s into the session |
  | Fixed −45 dB voice threshold, 5.5 s cap, 626 s | 122 of 133 (11 lost waiting for a voice or label, or as too late) | 10 | 87 of 133 | −0.91 s | up to 284 s |
  | Adaptive threshold, 4.5 s cap, 174 s | 49 of 49 decided | 3 | 4 of 52 | +0.34 s | 6–8 s |

  - On the adaptive build, unit length median was 3.0 s, starts p90 +0.46 s, and pipeline memory stayed flat at 73 MB.
  - Lines were still shortened: 30 of 52 at 0.25 s per character. That led to decision 4; its live effect is not measured yet.

- Units end in pauses, so the cloned voice reads whole phrases. A pause after a unit also absorbs a small overrun, which a capped unit followed directly by the next one could not.
- A unit can now be 4.5 s long at D = 8. Speaker labels still come in pieces of at most 3 s, and a unit takes the speaker who covers most of it. Two actors with no pause between their lines can share a unit and one voice, as they could inside a 3 s piece before.
- Audio-only references are cut from these spans, so a reference can be up to 4.5 s long. It is still isolated and gated (ADR-0016). Isolating 6 s takes about 0.2 s on the driver thread; the capture ring holds 4 s.
- Both clone workers are busy more often. Each learns its own level and pace, so voice consistency between them matters more; the follow-up is to share what they learn.
- If an older line's synthesis is still running when a newer line's slot comes, the newer one plays first, and the older plays late or is skipped by the lag limit. With two workers starting together and 1.45 s median slack, that is rare. It was already possible.
- The cap sets the smallest workable D: cap + 3.5 s. Lowering D in the app shortens the cap, down to the 3 s floor.

DoD:

- `scripts/check.sh`, covering `AudioSegmentTests`: the sentence cap, the dip cut, and the live default.
- `scripts/build_app.sh` and `scripts/smoke_app.sh`.
- Replay evidence (unit shape, slot and end error, slack) under `.local/scratch/tts-runon/`, measured with `.local/scratch/live-monitor/sync.py`.
