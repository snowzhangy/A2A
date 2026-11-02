# ADR-0009 — Keep each cloned voice stable across dialogue turns

Status: accepted for local development, 27 October 2026, following the user's report
that an actor sounds different each time the dialogue switches back to them.

The live trace showed two independent problems: a speaker's reference changed from
version 1 to version 2 during playback, and the DSP tracker sometimes assigned speech
to the wrong actor. Longer references can contain another actor when tracking merges
voices. Replacing conditioning mid-session therefore changes the voice the listener
has already learned to associate with an actor.

For cloned playback, the driver disables reference upgrades after the first usable
reference. The reducer pins that speaker's first prepared profile until stop or source
discontinuity, including when late or duplicate preparation events arrive. This does
not increase the three-second minimum acquisition requirement. System/tone casting
and explicitly upgradeable reference policies retain their existing behavior.

Qwen keeps the reference codec cache with the prepared voice. Upstream's length/sum
fingerprint cannot uniquely identify reference audio, so distinct voices cannot share
that cache. The speaker-embedding cache retains the exact immutable reference array,
with a bounded lifetime, instead of using its sample sum. Each synthesis resets the
Qwen sampling stream from an in-memory hash of reference PCM, mode and target language.
Another actor's generation or a canceled unit cannot alter that stream. Neither the
hash, seed, embedding, transcript nor PCM enters ordinary diagnostics. Fixed sampling
improves repeatability; it does not prove perceptual identity across different lines.

In cloned multi-speaker sessions, translation may finish before the source label.
The reducer waits for one speaker to cover more than half the unit's span before
choosing a voice. Labels and prepared profiles can arrive in either order. A unit
without a usable label expires at the existing source-lag limit rather than borrowing
the previous actor's voice. Target commits awaiting attribution carry
`speaker_pending: true` instead of claiming a confirmed speaker. This amends ADR-0005's
unlabeled/default-voice behavior for cloned dialogue only. Missing labels can reduce
dialogue coverage; no additional delay is imposed when sufficient labels already exist.

The DSP tracker still merges some actors. A stable profile cannot repair an incorrect
label, automatically separate all actors, or qualify recognition in a new episode.
Existing opted-in voice memory remains conservatively matched and source-scoped;
session speaker numbers do not select saved identities. This change does not train
a recognition model or qualify identity, naturalness or external release.

DoD: `scripts/check.sh`, `scripts/build_app.sh`, `scripts/smoke_app.sh`. Repeated-turn
model samples and actual process-tap evidence are described in
[the voice consistency report](../../research/live_dub_lab/VOICE-CONSISTENCY.md).
