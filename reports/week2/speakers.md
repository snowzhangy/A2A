**One voice per speaker — 25 October 2026**

User report after the fluent-playout work: “it's smoother but the 2 actor sounds the same”.
Everything below comes from file-mode runs and offline probes. **No live tap session or
listening test was made**, so perceived voice quality is unverified. Decision record:
[ADR-0005](../../docs/adr/0005-multi-speaker-voices.md).

## Cause

No speaker tracker existed, so every unit belonged to `S1`.
- **System voice.** Every line was read in one voice (Samantha).
- **Cloned voice.** One reference was learned from the first finalized utterances, whoever
  spoke them. On the drama excerpt, the old run's 10.2 s reference was cut from two actors'
  lines, so the cloned voice was a blend of them, used for everyone.
- **State machine.** The single-speaker rule (a speaker change re-enters acquisition, flushes,
  and cancels synthesis) would have interrupted every dialogue turn once speakers were detected.

## What changed

- **Tracker.** `a2a-pipeline --speakers auto` (the app passes it) runs a DSP speaker tracker:
  16 mel-cepstral coefficients of voiced frames (60–7600 Hz) over VAD-bounded segments of at
  most 3 s. It uses no model.
- **Attribution.** Each unit takes the speaker covering most of its span. This is checked
  again when its translation returns.
- **Turns.** Turns never fall back.
- **Missing voice.** A unit whose speaker has no voice yet waits up to 4 s after its source
  ends, then is skipped. It is never read in another speaker's voice.
- **System voice.** Each speaker is cast a distinct installed voice for their pitch.
  Novelty voices and Personal Voice are excluded.
- **Cloned voice.** Each speaker gets their own reference, built only from utterances that
  one speaker covers.
- **Without `--speakers auto`.** Behavior and golden traces are unchanged.

## Tracker calibration

Swift tracker on the current tree, run with `a2a-stage-probe speakers` (labels only, no
features written). The synthetic dialogues alternate two macOS voices reading Mandarin lines,
with known turns.

| Clip | Speakers present | Speakers found | Labels |
| --- | --- | --- | --- |
| Drama excerpt, 120 s (`youtube-NRB0ICxMkrM-0940`) | ≥5: groups of the Qwen3-TTS speaker encoder, consistent with the dialogue | 3 | Customer, waiter, Taoists; the two rarest voices join the Taoists |
| Mandarin audiobook, 600 s, one narrator (LibriVox, public domain) | 1 | 1 | 219 of 219 segments |
| Synthetic, female / male (Tingting / Reed) | 2 | 2 | 19 of 19 segments correct |
| Synthetic, female / male (Meijia / Rocko) | 2 | 2 | 17 of 17 correct |
| Synthetic, male / male (Reed / Grandpa) | 2 | 1 | Merged (18 segments) |
| Synthetic, female / female (Tingting / Meijia) | 2 | 1 | Merged (19 segments) |

**Feature choice.** On drama segments, scored against the encoder's groups, pairwise
same/different-speaker AUC was:

| Features | AUC |
| --- | --- |
| Pitch alone | 0.74 |
| 12 cepstra to 4 kHz | 0.83 |
| 16 cepstra to 7.6 kHz | 0.91 (adopted) |

Pitch varies too much within one actor to add information. On the female/male dialogues,
same-speaker segment distances stay below 0.66, while different-speaker distances are at
least 0.99.

**Thresholds.** A new speaker needs either a distance above 0.8 with 0.5 s of voicing, or
a distance above 0.5 confirmed within 15 s by a second segment whose combined print stays
more than 0.45 from every speaker. Short replies (0.12–0.4 s voiced) join a known speaker
within 0.5 and never create one. Two rejected variants show why both conditions are needed:
- Without the 15 s window, 4 of the narrator's 205 segments split off as a second voice (measured before the short-reply rule, which raises the count of labeled segments to 219).
- Requiring 0.5 for the combined print merged the Taoists into the customer.

These values were set on these clips only; no held-out set exists yet.

**Cost.** 1.5 s of processing for 601 s of audio, including resampling and VAD (optimized
build): 0.25% of one core. Each speaker keeps 16 values plus a 64-bin pitch histogram. With
the cloned voice, 30 s of source audio (5.8 MB) is now kept for the whole session, instead of
until the single reference completes.

## Drama excerpt, end to end

File mode with real-time pacing: `--asr apple --mt apple --translation-only --fluent`, debug
builds on both sides. "Before" is the previous fluent run, which used one voice.

**Distinctness metric** (used in both tables below). Each played unit's rendered English was
embedded with the Qwen3-TTS speaker encoder and assigned to the nearest speaker centroid of
the other units (leave-one-out). The count is how often that matches the unit's tracked
speaker; chance is about one in three. This measures whether speakers sound different. The
synthesizer is conditioned on the same encoder, so it is not identity evidence.

### System voice

"After" is the final code. Two earlier runs of the same feature, before the unlabeled-speech
fallback below, played 23 of 29 units with coverage 0.754, so these differences are mostly
run-to-run variation in recognition timing and in which units catch-up skips.

| | Before | After |
| --- | --- | --- |
| Voices | 1 (Samantha) | 3: S1 Daniel, S2 Samantha, S3 Rishi |
| Rendered units matching their speaker (distinctness) | 5 of 22 | **23 of 23** |
| Units played | 24 of 29 | 25 of 29 |
| Coverage (source s) | 0.773 | 0.682 |
| Fallbacks / catch-up skips | 0 / 5 | 0 / 4 |
| `lag_onset` median / p90 / max (s) | 4.98 / 10.98 / 13.20 | 4.90 / 6.40 / 13.56 |
| English s per source character (median) | 0.207 | 0.217 (earlier runs 0.229–0.234) |

S1 starts with the default voice (profile 1) and is cast Daniel (profile 2) when first
labeled; all ten S1 units were synthesized with profile 2. Rendered pitch matched the cast
voice for every played unit:

| Speaker (voice) | Units | Rendered pitch |
| --- | --- | --- |
| S1 (Daniel) | 10 | 109–124 Hz |
| S2 (Samantha) | 7 | 171–188 Hz |
| S3 (Rishi) | 8 | 113–116 Hz |

English per source character runs 5–13% above the single-voice run because the rate→speed
table was measured on Samantha. Daniel and Rishi are paced with it until each voice's learned
cadence takes over.

**Attribution against the encoder groups.** 12 translated units fall in the three main
groups, and 10 of them carry their group's speaker.
- One miss is a waiter line spoken before the waiter's voice was confirmed.
- One miss is a Taoist line attributed to the customer.
- The four units of the two rarer voices share another speaker's voice.

**Gender mismatch.** The waiter is a male character dubbed at about 215 Hz, so he is cast
a female voice.

### Cloned voice (research-only model)

| | Before | After |
| --- | --- | --- |
| References | 1, mixing two actors (3.8 s, then 10.2 s) | S1 4.0 → 8.6 s, S2 3.7 → 8.7 s, S3 4.1 → 5.7 s |
| Voice ready (source time) | 8.3 s | S1 8.4 s; S2 15.5 s (first labeled 13.9 s); S3 54.1 s (first labeled 50.8 s) |
| Profile preparation | 0.71 s, 1.00 s | 0.66–0.90 s each, six profiles |
| Rendered units matching their speaker (distinctness) | 7 of 20 | **15 of 20** |
| Units played | 22 of 29 | 22 of 29 |
| Coverage (source s) | 0.718 | 0.589 |
| Fallbacks / catch-up skips | 0 / 7 | 0 / 6 |
| Units skipped waiting for a voice | n/a | 0 |
| `lag_onset` median / p90 / max (s) | 5.28 / 8.26 / 13.08 | 4.59 / 7.08 / 13.48 |

**Rejected reference utterances.** One was rejected for mixed speakers; three were not
labeled within 3 s.

"After" is the current tree, without `--voice-memory`.

**Lower coverage.** Catch-up skipped the 10.7 s unit this time (35.4 s of source skipped,
against 24.3 s before). Which long unit catch-up skips varies between runs.

**An earlier run** of this feature, made before the other editor's reference-policy changes,
gave different numbers:

| | Earlier run |
| --- | --- |
| Distinctness | 17 of 20 |
| Units played | 21 |
| Coverage (source s) | 0.688 |
| `lag_onset` p90 (s) | 12.24 |

That p90 came from one unit that catch-up skips in the before run (the trace shows why):
- Its first chunk took 2.3 s, against 0.2 s usually, because the worker was preparing S3's
  upgraded profile. Preparation is serialized with synthesis.
- It then waited 6.1 s behind a 10 s phrase.
- No unit waited for a voice.

**Routing check.** Each speaker's English was compared with source speaker centroids, using
the same encoder across languages. The nearest source speaker was the unit's own for 14 of
20 units (11 of 20 in the earlier run); chance is about 7. This is weak evidence that the
references follow the speakers.

## Speech the tracker cannot label

Loud music, shouting above the 350 Hz pitch limit, and quiet input can leave speech
unlabeled. Such speech keeps a voice:
- **System and tone voices.** S1's default profile is prepared at start, as in single-speaker
  sessions. A cast voice follows as profile 2 once S1 is labeled.
- **Cloned voice.** Until anyone is labeled, references are acquired for S1 the
  single-speaker way.

**Check.** The harness's 84 s synthetic fixture was rebuilt as noise bursts with the same
envelope; the tracker labels nothing in it. Before this fallback, no unit would have played:
every one would have waited for a voice and expired.

| Voice | `--speakers auto` | `--speakers single` | Notes |
| --- | --- | --- | --- |
| Tone, fixture adapters, virtual clock | 36 of 36 | 36 of 36 | |
| Fake clone worker (`--clone-fake`), host clock | 33 of 36 | 33 of 36 | S1's reference is acquired as usual; the first units come before it |

On the original tone-burst fixture, `auto` labeled four speakers and also played 36 of 36,
with no fallbacks.

## Verification

Nineteen new tests:
- `MultiSpeakerTests` (9): attribution by span, re-attribution before synthesis, turns
  without fallback, waiting for a voice and its expiry, first-ready handoff, coverage,
  unlabeled speech in the default voice, and a cast voice replacing the default.
- `SpeakerTrackingTests` (7): analyzer pitch; two voices confirmed or immediate; one voice
  with varied pitch stays one; isolated outliers never pair; blips are unlabeled; the speaker
  cap; long speech is labeled in pieces.
- `VoiceCastingTests` (3): candidate filtering, distinct pitch-matched casting, ambiguous pitch.

`scripts/check.sh` passes on the current tree, which also contains other work from the same day: 169 Swift tests executed and 74 harness tests, with golden traces unchanged and every executable stamped SDK 27.0.

## Still open

Update, 1 November: on real video, the 4-speaker cap filled with music and effects early in a session, and every
later actor merged into an existing speaker. Passing voices now free their slots; see
[speaker-slots.md](speaker-slots.md).

- A live tap session and a listening check of dialogue voices.
- Splitting units at speaker turns. On the excerpt, 4 of 29 units contain both pitch classes,
  three of them 10–14 s long. Splitting needs recognizer finalization at the turn.
- A neural speaker embedding for similar same-gender voices. CAM++ is declared in the
  manifest but unverified.
- Rate calibration for each cast voice.
- A held-out evaluation set for the tracker.
