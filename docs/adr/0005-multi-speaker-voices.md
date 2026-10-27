# ADR-0005 — Each speaker in a dialogue gets their own voice

Status: accepted for local development, 25 October 2026, following the user's report
after the fluent-playout work: “it's smoother but the 2 actor sounds the same”.

## Context

D7 in the implementation plan limits alpha to one active speaker. No `SpeakerTracker`
existed, so every unit was attributed to `S1`. The system voice therefore read every line
in one voice. The cloned voice learned one reference from the first finalized utterances,
whoever spoke them; in a dialogue that reference could even mix two people. The state
machine's single-speaker rule (a speaker change re-enters voice acquisition, flushes
playout, and cancels synthesis) would also have interrupted every turn of a dialogue once
speakers were detected.

## Decision

`a2a-pipeline --speakers auto` enables multi-speaker sessions (`PipelineConfig.multiSpeaker`).
The app passes it. Without the flag, behavior and golden traces are unchanged.

- **Tracking.** `VoiceprintSpeakerTracker` (A2AModels) labels VAD-bounded speech, in pieces
  of at most 3 s, by the mel cepstrum of voiced frames (`A2AMedia.Voiceprint`). No model is
  involved: CAM++ is still only declared in the manifest, and the Qwen3-TTS speaker encoder
  is research-only. A neural embedder can replace the voiceprint behind the same protocol.
  The tracker prefers merging, because one actor alternating between voices is worse than
  two actors sharing one. A new speaker needs either a very large distance with 0.5 s of
  voicing, or a large distance confirmed within 15 s by a second segment whose combined
  print stays far from every existing speaker. Short replies (under 0.4 s of voicing) can
  join a known speaker but never create one. At most four speakers are tracked.
- **Attribution.** Each unit takes the speaker who covers most of its source span. This is
  decided again when its translation returns, because the tracker labels a segment only
  when the segment ends.
- **Turns.** Speaker turns never fall back, flush, or cancel. This supersedes D7 for these
  sessions. The phrase being heard finishes, and the next speaker's phrase follows it.
- **Missing voice.** A unit whose speaker has no voice yet waits for it while its source
  is fresh: up to the source-lag limit, 4 s after its source ends. It is then skipped. It
  is never read in another speaker's voice.
- **Unlabeled speech.** Loud music, pitch above 350 Hz, or quiet input can leave speech
  unlabeled; it is attributed to S1 and must still be heard.
  - System and tone voices: S1's default profile is prepared at start, as in single-speaker
    sessions. Once S1 is labeled, their cast voice follows as profile version 2.
  - Cloned voice: until anyone is labeled, references are acquired for S1 the single-speaker
    way.
- **System voice (plumbing).** Each speaker is cast an installed voice of a plausible
  gender for their median pitch (`VoiceCasting`). Voices are distinct until a pool runs
  out; after that, speakers share a voice at another pitch. Novelty voices and the user's
  Personal Voice are never used.
- **Cloned voice (research-only).** Each speaker has their own reference policy. An
  utterance joins a reference only when one speaker covers at least 90% of its labeled
  audio, so no clone is learned from someone else's speech. The worker holds up to eight
  profiles (two versions for each of four speakers). Consent now applies to every cloned
  participant, not only to the first speaker.
- **Data.** Traces carry speaker IDs and spans (`spk.assign`, `spk.change`), never
  features. Summaries report speaker counts and per-speaker profile timing.

## Consequences

Measured results are in [reports/week2/speakers.md](../../reports/week2/speakers.md). The
known limits are:

- Similar voices of the same gender can share one voice.
- A unit that spans a turn is voiced by the majority speaker. Splitting units at turns needs
  recognizer finalization at the turn and is not done.
- A new speaker's first line can take the previous speaker's voice until a second line
  confirms them.
- Casting by pitch can pick the wrong gender for a high-pitched male or low-pitched female
  voice.
- With the cloned voice, a new speaker is silent until their reference is ready: at least
  3 s of their clean, finalized speech, then preparation.

The tracker's thresholds were set on one 120 s drama excerpt, a 10-minute single-narrator
audiobook, and four synthetic two-voice dialogues. No held-out set exists yet.
