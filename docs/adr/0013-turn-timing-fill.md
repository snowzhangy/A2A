# ADR-0013 — Give each line the time its actor took

Status: accepted for local development, 2 November 2026, following the standing request to bring
the live translation to the quality of
[dub-anything](https://github.com/swax/dub-anything)
([external review](../../reports/week2/dub-anything-review.md),
[near-real-time comparison](../../reports/week2/near-realtime-dub-comparison.md)).

## Problem

The pipeline paced translated speech in one direction only. A take longer than the source span was
sped up to at most 1.3×; a take **shorter** than it was played short and the rest of the actor's
turn went by with nothing on the translated bus. Reference dubs measured this and fixed it:
dub-anything re-times every line to end where the actor stops, and its listeners ranked that
rhythm of the exchange at least as high as the acting itself (its `docs/research.md` findings 7-8).
Nothing in A2A even measured the gap.

With [ADR-0012](0012-delayed-picture-sync.md) each phrase now plays in its own slot, so the timing
is no longer free-for-all: the host already decides that a phrase starts at its source start + D.
A phrase that ends where its actor ended therefore costs nothing, because the phrase after it
still waits for its own slot.

## Decision

**Fill**: when a take comes out shorter than the speech it translates, slow it, within a bound, so
it ends on the actor's own last sound.

- The **reducer** owns the permission (`SynthesisJob.allowFill`, `SessionPipeline.allowFill(of:)`)
  and grants it only when all of these hold: the session plays in slots
  (`presentationDelaySeconds > 0`), the unit has real token timing (`!timingEstimated`: a forced
  split has no actor end to land on), and the unit is **not yet late for its slot**. A late phrase
  would spend the lateness it already has on stretching and push everything behind it.
- The **worker** owns the number, because only it knows how long the take is going to be:
  `audio_pacing.choose_speed(..., allow_fill=True)` returns `estimated / target` clamped to
  `[MIN_SPEED, MAX_SPEED]`. A deficit under `FILL_GUARD_SECONDS` (0.25 s, the same tolerance the
  speed-up side allows) leaves the cadence alone. `MIN_SPEED` is 0.85: WSOLA keeps pitch, and a
  stretched overlap smears transients well before that. dub-anything's floor is 0.8 with
  resampling, which moves pitch instead of smearing; a listener decides whether A2A goes that far.
- The target is the unit's own source span, whose end is the detector's last voiced sample. A
  clause committed mid-turn is therefore filled to its own pause, not to the end of the sentence.
- `StreamingWSOLA` accepts speeds down to 0.75 so a request for the floor is always computable.
  Stretching was verified to give the exact duration, streamed-equals-whole-buffer output, and
  in-phase overlaps down to a 65 Hz voice.
- `tts.request` carries `fill` when the unit is fillable; `tts.done.pace` can now be below 1.
- `a2a-pipeline --no-turn-fill` turns it off for an A/B listen, and the run summary records
  `turn_fill`.
- The harness reports the reference implementation's vocabulary: `turn_dead_air_s` (the English
  shorter than the actor's speech) and `turn_overrun_s` (longer), per unit and in aggregate, on
  units with real speech timing. `--no-turn-fill` and no-slot runs are the control.

Without a picture delay nothing changes: live playback plays as soon as audio is ready, where a
short line is saved latency and slowing it would delay every later phrase.

## Consequences

Fill buys back part of a line's dead air, not all of it: at 0.85× a take must be within 15% of its
slot to close the gap. The remaining deficit is a **text** problem — English that is too short
(or too long) for the time available — and is answered by length-controlled translation, not by
more stretching. The choice of how much to stretch therefore depends on the duration estimate:
OmniVoice has a rule that predicts a take's length from its reference (±0.2 s median), Qwen has an
in-process average per speaker, which is what the pace is computed from when no rule is available.

For OmniVoice a model-side alternative exists and is probably better: it takes `duration_s`, so a
filled line could be *requested* longer instead of stretched after the fact. That is a separate
experiment with its own listening check; the reference project found that asking OmniVoice for a
slower speed barely changes a take's length, while an explicit duration does.

A/B listening evidence is outstanding. The deterministic checks prove the mechanism and its
measurement, not that a stretched line sounds better than the actor's own silence.

DoD: `scripts/check.sh` (`TurnFillTests` for the reducer's permission, `test_turn_fill.py` for the
pace choice and the pacer's stretch, `test_clone_pipeline.py` for the driver, worker protocol and
metrics end to end, `test_metrics.py` for the new rows), plus
[the parity report](../../reports/week2/dub-parity.md).
