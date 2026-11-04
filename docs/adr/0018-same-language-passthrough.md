# ADR-0018: Play the actor when a line is already in the target language, and budget lines into real silence

Status: accepted for local development on 3 November 2026. It addresses target-language
passthrough, missed lines, speaker attribution, and timing.

## Problem

1. **Lines already in the target language were re-spoken by the clone.**
   - Direct audio translation (Hy-SimulMT) answers a target-language line in the target language.
   - The clone then speaks it again in a slightly different voice. The user heard it.
2. **Some lines were silent.**
   - With `--background`, the bed has the voice removed, and `--translation-only` turns off the original bus.
   - So a line Hy answered with nothing (`empty`) or a filler was silent, not the actor.
   - A Chinese → English session of 399 s had 7 `empty` and 6 `filler` lines.
3. **Chinese → English lines overran.**
   - English runs longer than the Chinese it translates, and Chinese film dialogue leaves few gaps.
   - Each line could be budgeted 1 s past its span (the validator's fixed grace), into the next line. The next line then started late, and so on.
   - In that session lines ended a median 0.92 s after their actor, and 20 of 78 started more than 0.5 s late.
   - Replaying that trace's own audio through the slot schedule reproduced 18 late starts.

## Decision

Both behaviours apply to `--asr audio --tts clone --background` with a picture delay of at least 4 s. `--no-passthrough` turns off the first.

1. **Spoken-language identification per unit.**
   - A resident `research/lid_worker.py` runs ECAPA-TDNN VoxLingua107 on MLX: manifest `ecapa-voxlingua107-lid-mlx`, Apache-2.0, research-only.
   - `A2AModels.LanguageIdentifier` starts it beside the clone workers. The pipeline finds the model under `.local/models` next to the clone worker's checkout, or takes it from `--lid-model`.
   - Each closed unit of at least 1 s sends its isolated voice at 16 kHz. Answers take 12–15 ms.
   - A unit whose language is the session's target with probability ≥ 0.8 becomes `PipelineInput.sourceLanguage(span, matchesTarget: true)`.
   - The reducer cancels its translation and closes it `original_only` / `same_language`, but only if synthesis has not started. A line is never voiced twice.
   - Only the language code and probability reach the run summary; no audio or text.
2. **The actor's own voice comes back on the delayed picture.**
   - `PipelineConfig.restoresOriginalVoice` lets the reducer emit `.restoreOriginalVoice(span)`. It does so for a same-language unit, and for a unit Hy answered with nothing or a filler.
   - The driver holds the separated bed for min(6 s, D − 1.5 s) before writing it to the delayed background bus. The bus is pre-filled with that much less silence, so the bed still lands D behind capture.
   - A span marked for restore is written as bed + its own isolated voice, with 10 ms ramps. Both come from the same render, so the sum is the original sound, and it lands on the picture by construction.
   - The hold keeps wall-clock time through pauses (idle steps flush it).
3. **Budget each line into the silence after it.**
   - When a direct-audio unit's translation arrives, the driver sends `PipelineInput.followingSilence(span, seconds)`. This is the gap to the next span that has closed or opened since, else to everything heard so far.
   - The validator's grace for that line becomes min(1 s, silence + 0.2 s).
   - In a playout simulation on the Chinese → English trace, late starts dropped from 14 to 2, at the cost of about 5% of English characters cut at clause boundaries.
   - English → Chinese was barely affected: 4 to 3 late starts.

## Evidence

Replay with the app's live flags (`.local/scratch/tts-runon/pass-mixed*`), 49 s of the 1995 drama with two English lines (macOS voices) inserted, Chinese → English:

- The identifier marked both English lines (19.0–23.5 s, 24.8–28.9 s) `same_language`. It sent the other 11 to translation.
- Both were restored, none late.
- Identifying the rendered output 8 s later finds English at p = 0.9999 and 0.978 over those spans: the actor's own voice.
- All 14 scheduled phrases started within 250 ms of their slots (median 13 ms).

Identifier sanity check: macOS English and Mandarin voices gave `en` 0.9999 and `zh` 1.0. Every 2 s window of the drama clips gave `zh`, and those clips are Chinese.

## Consequences

- One more model on the GPU, 85 MB and about 15 ms per unit.
- A wrong "same language" verdict leaves a line untranslated. The 0.8 threshold and the 1 s minimum keep that conservative; shorter units are always translated.
- The background bus carries 6 s less silence and the driver holds 6 s of bed (about 2.3 MB at 48 kHz).
- A restore decided after its bed was written plays without the voice for the part already written. The summary counts these as `voice_restored_late`.
- The original implementation decided missing-voice (`no_profile`) and missing-speaker (`no_speaker`) units after the hold. The follow-up below moves the decision earlier and restores those units.
- More English lines are shortened than before. Timing now takes precedence over the last clause of a long line.

## Follow-up: missing speaker, voice and translator results

A subsequent English → Mandarin live session had three brief units with no attributed speaker.
They expired after the held bed had already been written, leaving silence even though a translation
was available. The reducer now decides before the start of the unit leaves the held bed: a unit
still waiting for a label or profile is closed as `original_only` and its isolated source voice is
restored. A translation still pending at that deadline is canceled and restored too; a late result
cannot synthesize over it. Rejected or failed translations request restoration immediately. The
deadline is the bed hold minus 0.7 s for driver and separator jitter; the summary's
`voice_restored_late` remains the measure of missed deadlines. This trades an untranslated original
line for a silent one; it does not establish translated coverage or speaker-identification accuracy.

The same session also showed substantial target shortening. Local captions now show the complete
accepted model response even when the voice receives a shorter prefix. The UI labels that difference;
context and speech still use the shortened text. Caption text stays in memory and out of ordinary
diagnostics. This makes omissions visible but does not solve spoken-content loss.

Further follow-up: an unheard unit canceled by recovery or queue pressure, or whose synthesis
fails before scheduling, is restored if its entire voice is still inside the bed's decision
window. A unit already scheduled is never restored, avoiding a second voice over audible speech.
Stop and discontinuity never restore canceled units.

DoD:

- `scripts/check.sh`, covering `PassthroughTests` (same-language, late verdict, unanswered lines, silence-bounded budget) and the manifest gate.
- `scripts/build_app.sh` and `scripts/smoke_app.sh`.
- The mixed-language replay above.
