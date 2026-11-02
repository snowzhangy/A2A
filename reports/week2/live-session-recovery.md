# Recovering the lines three live sessions lost — 1 November 2026

A review of three live app sessions (`--asr audio --mt hy --tts clone --speakers auto`) counted
what the listener did not get: session 1 (en→zh, 7 min) translated 67 lines and left 45 in the
original audio (60% coverage); session 2 (zh→en, 24 s) got 7 of 7; session 3 (zh→en, 50 s)
translated 12 and left 3. The causes, in order of lines lost: unlabeled speech (31 lines in
session 1), Qwen speaking Mandarin at about 2 characters per second instead of 4–5 (~14 lines and
24 catch-up skips in session 1), English too long for the 3 s piece it translated (15–32 characters
per source second, where spoken English is about 15), and empty or repeated answers to music and
pauses counted as failures (9 lines).

This record covers the three changes that address those, what each is measured on, and what stays
open. The review's numbers come from the user's sessions; only session 2's trace survived, so the
new measurements below are deterministic reducer tests, a stage-probe measurement on consented
drama audio, and unit tests of the worker's budget.

## Change 1 — an unlabeled piece of speech inherits the speaker it continues (ADR-0005 exception)

The tracker labels the speech between two pauses and gives no label to a piece with too little
voiced speech in it (`VoiceprintSpeakerTracker.Config.minVoicedFrames` / `minContinuationFrames`).
A cloned session requires attribution before choosing a voice (`requireSpeakerAttribution`), so an
unlabeled piece waited and then expired with `source.disposition reason: no_speaker` — 31 lines in
session 1.

`SessionPipeline` now carries the previous label forward when the unlabeled piece **continues that
turn** (`SessionPipeline.swift`, `carriedSpeaker(for:)`):

- no VAD pause since the previous label, and within `--speaker-carry` (default **6 s**) of it; or
- a brief piece — at most `--speaker-carry-piece` (default **1 s**) long — starting within
  `--speaker-carry-pause` (default **3 s**) after the previous label, pause allowed.

Never backwards: a label that arrives after a unit cannot speak that earlier line, because the
later stretch may belong to an actor who started talking without a detectable pause. A `commit.target`
resolved this way carries `"speaker_carried": true`, and the run summary reports
`speaker_carried_units` and the three thresholds. `--speaker-carry 0` restores the strict rule for
comparing runs.

### Why the pause branch was added, and what it costs

The review's first sketch was the no-pause rule alone. Measured on three 3-minute excerpts of the
consented Mandarin drama (`.local/live-dub/input/slices`, offsets 300 s / 900 s / 1500 s) with
`a2a-stage-probe speakers`, which replays the same pieces the live session commits
(`AudioSegmentPolicy`) against the same labels:

| excerpt | pieces | labeled by the tracker | unvoiced today | recovered, no-pause rule only | recovered, shipped rule | left unvoiced | identity conflicts |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 05:00–08:00 | 56 | 46 | 10 | 4 | 9 | 1 | 0 |
| 15:00–18:00 | 72 | 55 | 17 | 4 | 16 | 1 | 0 |
| 25:00–28:00 | 69 | 52 | 17 | 1 | 10 | 7 | 0 |
| **total** | **197** | **153 (78%)** | **44 (22%)** | **9 (20%)** | **35 (80%)** | **9 (4.6%)** | **0** |

The no-pause rule recovers one piece in five because most unlabeled pieces are **short pieces that
start after a pause** (median unlabeled piece 0.62 s): a brief reply, a laugh, the tail of a
sentence. Allowing the carry for a piece of at most 1 s within 3 s of the previous label recovers
35 of the 44.

`identity conflicts` is the risk check, not a wish: for every piece the tracker *did* label, it asks
whether the carry rule would have named a different actor. At the shipped thresholds it is 0 of 153
labeled pieces in these excerpts. It is not zero by construction — widening the pause branch to
6 s / 1.5 s, or dropping the short-piece condition entirely, starts naming conflicts in the same
measurement (5–17 mislabeled pieces per excerpt on the busiest scene). So the short-piece condition
is what keeps the identity rule, and it is a knob, not a secret.

This deliberately relaxes the AGENTS rule "a unit is never voiced with another speaker's voice".
The narrow claim now is: within one uninterrupted stretch, or as a brief reply just after a pause,
unlabeled speech is voiced as the previous actor; anything longer or later still waits for its own
label and is skipped.

## Change 2 — Qwen's audio is capped by the character count it has to speak

`research/tts_worker.py` bounded generation with `expected = len(text)/4 + 1 s` and 1.6× slack for
Mandarin, which tolerates 2.5 characters per second: exactly the pace that produced a 10 s line for
3 s of source and 24 catch-up skips. The cap is now a **floor on pace** rather than slack on an
estimate — `speech_budget_seconds(text, language) = characters / MIN_CHARS_PER_SECOND + 0.75 s`,
with `MIN_CHARS_PER_SECOND = {"chinese": 3.2, "english": 11.0}` — and the worker reports the cut:

- a normal line cannot be truncated: the budget leaves more than 0.5 s above 4 characters/s (Han)
  or 13 characters/s (English), the pace this model measures;
- the 21-character line that took 10 s is capped below 7.5 s, and its `tts.done` carries
  `"truncated": true` plus `budget_ms`; the run summary counts `tts_truncated_units`;
- the same budget reaches the model as its `max_tokens`, so a missed end-of-speech cannot stream
  onto the bus (test: `harness/tests/test_tts_speech_budget.py`).

Truncation cuts where the audio stops: the tail of that line is lost, un-faded. That is the intended
trade — one line losing its tail instead of the queue losing several lines — and it is visible in
the trace rather than silent. With Change 3 shortening the text first, the cap should only ever fire
on model drag.

## Change 3 — a target gets the time its source took, and nothing-to-say is silence

Two different failures were both reported as failed translations.

**Too long.** The validator only had a runaway cap (`max(40, 35 chars/s)`), so a 3 s piece was
allowed 105 characters of English and anything past that was dropped; anything just under it was
spoken late and pushed the queue. `TranslationValidator` now computes a **spoken budget** from the
source span — `(sourceSeconds + 1 s grace) × characters per second`, where the rate comes from the
pipeline's own `estimatedSecondsPerTargetChar` (15/s for English targets, 4/s for Mandarin) so debt
and budget agree — and over-budget text is cut to it at its last clause boundary, or word boundary
when that would throw away more than 60% of the line (`TargetShortener`, ADR-style deletion, keeping
the head). A cut never ends on a dangling function word ("… the price of"), and Han cuts step back
off words when a `WordSegmenter` for the target language is available — the driver now injects one
for en→zh as well as zh→en (`--no-word-cuts` turns both off). If nothing safe fits, the line is still
rejected as `too_long`: a misleading fragment is not better than the original audio. `commit.target`
carries `"shortened": true` and `mt_chars` (what the model returned), the summary counts
`shortened_targets`, and `--no-shorten` restores the old drop.

**Nothing to say.** Music, laughter and pauses come back from the translator as an empty string or a
filler ("Yeah."). Both used to be failures: `mt.result ok:false` plus a `source.disposition` of
`original_only`, which in a session that can hear the original also asks for the fallback. Now:
`TranslationValidator.evaluate` returns `silent(.empty | .filler)`, the unit ends as the new
`source.disposition "silence"` (schema updated), there is no error event and no fallback, and the
unit stops holding debt at once. `FillerLexicon` is a closed word list matched only when the whole
target is fillers — "Oh, that is wrong." and "Yes." are still translated. `HySimulTranslator` hands
a completed-but-empty answer to the reducer as an empty candidate instead of throwing, and the
filler verdict never enters the translation context for the next request. `--no-filler-silence`
restores the old counting for comparing runs.

What the recorded app session says about the budget: its six translated lines ran 9.0–20.7 target
characters per source second (median 12.7), and their audio came out at 0.83–1.1× the source
duration. One line — 62 characters over a 3 s piece — is over the new spoken budget and is now cut
to "…price is final" instead of being spoken 10% late; none exceeded the old hard cap, which is why
the old policy's failure there was lateness rather than a drop. The review's 15–32 characters per
second is where shortening starts to bind, and the review's one >105-character drop is now a
shortened line.

## Also, from trying to re-run the live path

The configured translation server (`http://10.0.1.29:8080`) answers `/health` and `/v1/models` as
a different service (it lists `Qwen3.8 Flash Next` and an unconfigured Qwen3-ASR) and has no
`v1/audio/translations` route. Every direct-audio request fails with "no route for this path", which
the adapter reported as *"Hy-SimulMT server returned an invalid response"* — indistinguishable from a
malformed stream. The message now carries the HTTP status (`Hy-SimulMT server rejected the request
with HTTP 404`). Until the Hy-SimulMT server is reachable again, the live A/B for Changes 1–3 cannot
be run, and no claim here depends on it.

## Limits and what stays open

- Change 1 measures attribution, not identity: a carried label can still be the wrong actor when two
  people speak without a pause between them. The probe's conflict count is the only guard we have,
  and it is 0 on 153 labeled pieces from three excerpts — not on a stadium scene.
- Change 3 keeps the head of the sentence. Simultaneous interpreters delete the *less important*
  part; choosing that needs the model. The natural next step is asking Hy for a shorter rendering
  instead of cutting one, which costs a second request (0.7–1.0 s measured idle) and is not free.
- Filler silence will occasionally silence a real "Yeah.". The list is closed and short, and the
  verdict is visible in the trace (`mt.result … "silence":"filler"`).
- Not touched here: the Hy slowdown in session 3 (0.84 s → 2.2–2.5 s per request, unreproduced
  at 0.7–1.0 s on an idle server), the ~4 s floor between an actor starting and the English
  starting, the 4-speaker tracker cap (session 1 reached S4), the Q4-versus-Q8 model swap, and the
  pronoun/name/half-sentence quality issues from 3 s cuts with no context.
- The app still deletes its trace when a session stops (`SessionModel.cleanUp`). The monitor copies
  traces out of the temporary directory instead; keeping the last few inside the app's own
  diagnostics directory is the cleaner fix and is not done here.

## DoD

```
source scripts/env.sh && scripts/check.sh                    # 230 Swift tests executed, 97 harness tests, gates
swift test --package-path apple --filter TargetBudgetTests   # spoken budget, silence, deletion
swift test --package-path apple --filter MultiSpeakerTests   # carry: inherits, pauses, limits, off
swift test --package-path apple --filter TranslationDirectionTests   # filler lexicon
uv run --project harness pytest harness/tests/test_tts_speech_budget.py -q   # worker budget + truncation
```

Attribution measurement (interactive Mac, DSP only — no capture, no models, no devices):

```
$(swift build --package-path apple --show-bin-path)/a2a-stage-probe speakers \
  --input .local/live-dub/input/slices/ep09-900-180s.wav --out .local/live-dub/runs/attribution \
  [--speaker-carry 6 --speaker-carry-after-pause 3 --speaker-carry-max-piece 1]
# prints pieces / labeled / carried / conflicts / unvoiced
```

Live A/B for Changes 1–3 once the Hy-SimulMT server is reachable (same clip, same flags, one knob set
at a time — `--speaker-carry 0 --no-shorten --no-filler-silence` is the old behaviour):

```
a2a-pipeline --tap-bundle ID --asr audio --mt hy --hy-server URL --tts clone --clone-mode xvector \
  --speakers auto --fluent --translation-only --research-text --trace t.jsonl \
  --source-out s.wav --translated-out t.wav
# read: source.disposition reasons, mt.result silence reasons, shortened_targets,
#       speaker_carried_units, tts_truncated_units
```
