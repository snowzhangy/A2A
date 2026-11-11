# Qwen takes: the budget counts speech, and the voice learns its own pace

9 November 2026. The user reported four problems with cloned (Qwen3-TTS) playback while
Hy-SimulMT itself worked: the original voice sometimes plays instead of the translation,
some lines are missing although their caption is shown, speech arrives 3–4 s late even
with a prepared voice, and a voice's tone changes between lines.

No live trace exists for those sessions: the app deletes its trace when a session stops.
This report therefore rests on the recorded replays in `.local/scratch/tts-runon/` (the
app's live flags, local Hy, two Qwen workers) and on new isolated runs of the worker with
the installed model. Two worker defects were found and fixed. The other causes are named
below with their measurements and are not changed here.

## What was measured

`research/tts_worker.py` was driven in process with `qwen3-tts-12hz-0.6b-base`, x-vector
conditioning, temperature 0.6, 0.32 s chunks, `target_s` and `fill` set as the slotted host
sets them:

- English: the 49 translated lines of two recorded Mandarin→English replays, spoken on four
  voices cut from the isolated voice of one replay clip. 196 takes.
- Mandarin: the 34 translated lines of one recorded replay on three voices cut from the
  isolated voice of another clip. 102 takes.

Each take was back-transcribed with `a2a-transcribe`, which gives word recall against the
requested text. Recall is limited by the recognizer on names (0.94–0.96 on unchanged
takes), so it is read per take, not as a quality score. Pitch is a median autocorrelation
F0 per take, a coarse measure. Raw audio, texts and scripts stay in the session scratch
directory and `.local/scratch/tts-improvements/`.

## Defect 1: silence before the speech spent the line's budget

Qwen starts a take with silence: 0.46 s at the median, 0.63 s at p90 and up to 3.2 s. It
also pauses before EOS. The character-count budget (`speech_budget_seconds`) was enforced
as the model's token cap, so both silences counted against it.

With the cap raised 2.5 times, only 1 of 196 takes held speech longer than its budget, by
0.06 s. With the cap in force, 24 of 196 single-attempt takes stopped at it. Four of them
lost words, including one that started 3.2 s late and lost half its line.

The same defect explains the two lines lost in the recorded 49 s mixed replay: both were
reported `truncated`, and one sent 0.35 s of audio for a 26-character line.

The budget now runs from the take's first voiced frame:

- The model's token cap is the budget plus `LEAD_ALLOWANCE_SECONDS` (1.5 s) for a streaming
  backend. The worker ends the take once the speech has used the budget.
- A whole-phrase backend keeps the old cap, because its audio cannot be stopped mid-take.
- `truncated` is now set only when the take was still voiced at the cap. A take that reached
  the cap during the pause before EOS lost nothing.
- Either limit counts as the cap. A take whose silence is longer than the lead allowance is
  ended by the model's token cap before its speech has used the budget; it is `truncated`
  and teaches the voice no pace.
- `done` carries `lead_s`.

## Defect 2: the voice's pace was learned from the raw take

Each take teaches the voice its seconds per character, and the next line's speed is chosen
from that. The estimate used the raw take, which is 0.5–0.9 s longer than its speech. On
short Mandarin lines that is about 40% too slow, so lines were sped up that already fit.

The pace is now learned from the audio the pacer is given (shaped, long pauses removed)
plus `PACE_MARGIN_SECONDS` (0.2 s). The margin is a trade, simulated exactly on the recorded
takes because takes are deterministic per voice and text:

| Mandarin, 102 takes | At the 1.3× limit | Sped up, yet ends > 0.25 s early | Ends > 0.4 s late |
| --- | ---: | ---: | ---: |
| Raw take (before) | 56 | 32 | 0 |
| Speech only | 6 | 4 | 9 |
| Speech + 0.2 s (now) | 17 | 13 | 4 |

English takes change little, because their text is longer than the source allows at any
estimate: 138 → 119 at the limit, 31 → 37 more than 0.4 s late.

## Result

Isolated worker, two attempts per take as shipped:

| | English, 196 takes | Mandarin, 102 takes |
| --- | ---: | ---: |
| Takes reported truncated | 21 → 4 | 2 → 0 |
| Takes at the 1.3× limit | 138 → 119 | 55 → 17 |
| Sped up, yet ends > 0.25 s early | 21 → 15 | 31 → 13 |
| Ends > 0.4 s after its source | 31 → 37 | 0 → 4 |
| First audio, median / max | 0.20 / 0.68 s, unchanged | 0.20 / 0.59 s, unchanged |
| Generation time, whole set | 171.2 → 173.6 s | 71.2 → 71.5 s |

The four remaining English truncations are one slow voice (8–9 characters per second)
reaching the cap with every word recognized.

Full pipeline, the two Mandarin→English clips replayed once each with the new worker (the
host binary is unchanged, and single runs differ in speaker timing):

| Clip | Truncated takes | Takes too short for their text | Played | Lateness p90 / max |
| --- | ---: | ---: | ---: | ---: |
| 93 s, D = 5 | 2 → 0 | 0 → 0 | 35 → 35 of 38 | 33 / 413 → 75 / 413 ms |
| 49 s mixed, D = 8 | 4 → 0 | 2 → 0 | 14 → 13 of 14 | 115 / 174 → 272 / 358 ms |

"Too short" means less than 1/22 s of speech per character. Both lines lost in the old mixed
replay are spoken in the new one. The new mixed run lost one early line to `no_profile`,
which is the host's reference timing, not the worker. Lines that are no longer cut run
longer, so lateness rose; nothing came near the 2 s skip limit. English→Mandarin was not
replayed end to end: no recorded English-source clip is available.

## What the four reports come down to

1. **Original voice instead of the translation.** In the recorded replays every such unit
   has a named reason: `no_profile` for the first 2–3 units of a session (the first voice
   was ready 11 s into one clip), `same_language` for lines already in the target language,
   and one `tts_failed`. The stall-based deadline (ADR-0018 follow-up) already removed
   `mt_timeout_restore`. None of these is a Qwen fault, and their live rate is unknown.
2. **Missing lines with the caption shown.** Two causes. The cap defect above is fixed. The
   larger one is policy: the caption shows the whole translation, and the voice speaks the
   prefix that fits the source's time. In five replays 38–64% of lines were shortened and
   19–27% of the translated characters were never spoken.
3. **3–4 s delay.** After a unit closes, the first audio is ready in about 1.0 s: 0.37 s to
   confirm the pause, 0.3–0.5 s for Hy's answer, 0.2 s for Qwen's first chunk (0.5–0.7 s on
   the 2–6% of takes that are retried). The rest is the unit itself, 1–3 s of speech that must
   end first. A prepared voice removes only the wait for the first reference.
4. **Tone changes.** Median pitch varies between takes of one voice by 1.3–1.9 semitones
   (standard deviation) in English and 3.1–3.4 in Mandarin. It follows the sampling more
   than the text (cross-voice correlation 0.2–0.4). Tempo also flipped between 1.0× and
   1.3× from line to line, which defect 2 reduces. Three alternatives were measured and
   rejected: temperature 0.4 (more failed starts, wider pitch), top-p 0.8 (pitch 1.65 → 1.49
   semitones, but more failed starts and lower recall), and in-context conditioning on a
   take of the voice itself (pitch no steadier, level less steady).

## Not done

- Keeping the app's trace after a session. Without it, the live rate of each reason in
  point 1 stays unknown.
- Holding a take until it is complete when its slot allows. That would let the worker
  retry a bad take unheard and pace every take exactly, at the cost of a deadline from the
  host. By arithmetic on the replay timings (not measured), the run-ahead leaves about 1–2.5 s for
  it at D = 5 s and about 0.2 s at D = 3 s.
- The shortening policy in point 2 was addressed in the subsequent desktop follow-up:
  the app now passes `--no-shorten` with the longer bounded voice budget. See
  [ADR-0018](../../docs/adr/0018-same-language-passthrough.md#follow-up-preserve-sentence-endings-in-desktop-playback).

## Verification

- `uv run --project harness pytest harness/tests/test_tts_speech_budget.py`: six new tests
  (leading silence, run-on speech, token cap before the budget, quiet at the cap,
  whole-phrase backends, pace learning)
  and the updated cap test.
- `scripts/check.sh`.
