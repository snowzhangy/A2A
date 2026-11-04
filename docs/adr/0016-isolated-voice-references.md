# ADR-0016: Clone audio-only voices from the isolated voice, never from the scene

Status: accepted for local development on 2 November 2026. It addresses inaudible and inconsistent
cloned speech observed during a live session.

## Problem

The session was the app's live path: `--asr audio --mt hy-local --tts clone --clone-mode xvector
--dual-clone --speakers auto`, with a picture delay. Its trace showed the following:

- Translation was healthy. Every `mt.result` was ok, at about 0.4 s each.
- The translated bus was busy. It consumed translated audio for about 96% of output time.
- What it played was not speech. 57 of 72 takes ran to their character-count token cap
  (`tts.done.truncated`). Native pace was 378 ms per Mandarin character, where the model speaks
  180–220 ms. The worker then learned that pace (`seconds_per_character`) and chose the largest
  speed-up, 1.3×, for most later lines. Lines queued back to back and played up to 4 s past their
  slots, and catch-up skipped some of them.

The model is not the cause. Offline, the same model, mode and temperature (0.6) gave 0/12 cut
takes from a clean reference. Speech recognition read back every word. The full pipeline with the
live flags on clean English gave 1 cut take in 40 and 16 ms median lateness. A synthetic music bed
at +6 dB SNR gave 1 in 33.

The reference is the cause. A live audio-only reference is the first 3 s that pass VAD and speaker
coverage. It is cut from the raw mix, and the profile is pinned for the session (ADR-0009).
Without a transcript, nothing checks that those 3 s are speech. On a 1995 drama soundtrack, 3 s
references taken at different offsets gave 0–5 cut takes out of 12. The reference at 110 s holds
music and effects, and speech recognition finds no words in it. That reference gave 5 cut takes,
4 takes with no recognizable words, and 355 ms per character: the live signature.

The same input through the old pipeline as one speaker opens with those 3 s and then plays clean
English. It reproduced the session: 55% of takes cut, 371 ms per character, 1.5 s median and 3.8 s
maximum lateness, and two lines skipped by catch-up.

## Decision

1. **Isolate, then gate, each audio-only reference segment.** When `ReferencePolicyConfig.allowEmptyText` is
   set (`--asr audio --clone-mode xvector`), the driver runs Apple's AUSoundIsolation over each candidate
   segment, warmed up on half a second of the audio before it. It uses
   `BackgroundSeparator.isolateVoice`, which builds a fresh unit per call, compensates latency, and
   runs on the ordinary driver thread at about 0.1 s for 3 s of audio. The reference is that
   isolated voice. `voiceShareDB` is the isolated level minus the segment's level. A segment below
   −10 dB is rejected as `not_voice`. Level gates apply to the isolated voice, so a segment with
   nothing left after isolation is rejected as `quiet`.

   Measured shares: speech-led segments sit between −2 and 0 dB. Music and effects sit between −35
   and −24 dB. The share is the only speech evidence an audio-only reference gets. If isolation
   fails, the mix is used ungated and `reference_isolation_failed` is counted.
   `--no-reference-isolation` restores the old behavior for comparison.
2. **Transcript-backed references are unchanged.** For ICL mode, or a scripted or recognizer
   transcript, the words already show that the segment is speech. A fixed `--voice-ref` is the
   user's choice.
3. **A take that the token cap stopped teaches the voice nothing.** `research/tts_worker.py` skips the
   `seconds_per_character` update for a truncated take. Its length is the cap's, not the voice's,
   so one bad take no longer pins every later line at 1.3×. Silent takes were already skipped.

## Consequences

- On the reproduction above, the new pipeline cut 3% of takes, spoke at 214 ms per character, and
  played lines 17 ms median and 41 ms maximum from their slots, with no catch-up skips. Isolated
  references from the borderline offsets went from 4/12 to 0/12 cut takes, with no takes that
  recognition could not read.
- The voice waits for clean speech. In the reproduction the profile was ready at about 11 s instead
  of 3 s. Units before it play the original audio (`no_profile`), which beats a garbled clone.
- A source whose speech is always buried under music may never collect a reference. It then keeps
  original playback for that speaker, which is the existing `no_profile` behavior.
- The reference is now the isolated voice. It is cleaner and slightly band-limited by the unit.
  Remembered voices (ADR-0006) store what was used.
- Per-line delivery clips (ADR-0015, off by default) still come from the mix.

DoD: `scripts/check.sh`, covering `ReferencePolicyTests` (gate rules) and harness
`test_tts_speech_budget.py` (pace learner). Isolation itself runs with
`A2A_RUN_APPLE_TESTS=1 swift test --package-path apple --filter VoiceIsolationTests`. Run audio and
transcripts stay under `.local/scratch/tts-runon/`.
