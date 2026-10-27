# Week-2 gate decision — 24 October 2026

**NO-GO for playback takeover or an external original-speaker alpha.** Development
and captions remain available. The project is not complete against the execution
plan; missing model, lifecycle, and listener evidence cannot count as a pass.

The usable development combination is Apple SpeechTranscriber zh-CN → Apple
Translation zh-Hans→en. The app presents English captions while the selected app's
original audio continues normally. The experimental CLI uses AVSpeechSynthesizer,
which explicitly does not preserve speaker identity. No third-party weight entry
is enabled in the manifest.

| Gate | Evidence and result |
| --- | --- |
| Tap lifecycle | A1's earlier FixturePlayer capture evidence remains in `status.md`. A3's private muted-tap host-crash recovery, device/sleep cycles, and permission transitions are still unqualified. Playback takeover remains blocked. |
| Original-speaker identity | No qualified cloning adapter, consented speaker study, or Match-5 listener results. Pending. The system voice is excluded from identity evidence. |
| Sustained concurrent speed | No qualified ASR/MT/cloning-TTS combination or concurrent Zoom benchmark. Pending. |
| Warm streaming latency and coverage | The earlier-started synthetic live development run at the unchanged 3 s debt / 4 s source-age thresholds failed: 8 units, 3 played, 2 fallbacks, incomplete translation of 4 of 6 sentences. Played-unit onset median 2.609 s, max 2.750 s; capture correlation 1.0; back-transcript WER 0.0 for the audio actually played; zero reported errors and trace drops. This is plumbing evidence and fails coverage, not a feasibility pass. Local artifacts: `.local/runs/current-live-defaults/`. |
| Meaning and intelligibility | No blinded bilingual fixed-sample adequacy review. Pending. Back-transcription of played system-voice output does not replace that review. |
| Model memory | No all-models-resident measurement. Pending. |

The user subsequently selected deterministic verification only. No further live
capture/model acceptance runs were started after that selection. The earlier live
run had completed when termination was attempted; the process had already exited.

This pass adds a signed local SwiftUI app with explicit language setup, source
selection, session captions, and Stop/Quit cleanup. Text delivery is separate from
persisted diagnostics. The app starts the bundled development pipeline in
`--captions-only` mode; it is not the isolated inference XPC topology from E1.
The host still needs the qualified XPC/MLX worker before E1 can close.

Runtime repairs include immediate original-audio recovery on discontinuity and
model failure, clearing profiles/context on Stop, rejecting late stopped-session
events, bounded pending units, translation cancellation, serialized synthesis,
empty-synthesis rejection, device/source/stall checks, capture-loss/timestamp
failure handling, device-rate-correct backlog measurement, bounded Apple ASR
queues, and a VAD evidence check against silence hallucinations.

Deterministic verification: `scripts/check.sh` passed 84 Swift tests and 37 harness
tests, including synthetic offline and paced replay. The manifest and all six
executable SDK stamps passed. Native packaging and strict signature verification
passed with `scripts/build_app.sh`; synthetic native window startup passed with
`scripts/smoke_app.sh` (latest evidence: `.local/runs/app-smoke.G014vX/app.json`).
Apple model runtime tests now require
`A2A_RUN_APPLE_TESTS=1` explicitly. A screenshot attempt returned `could not create
image from window`; only native window startup is verified, not screenshot QA or
a three-person usability acceptance test.

Remaining execution order is A2/A3 interactive qualification → verified cloning
artifacts and native adapter/worker → M1/M2/M3/M4 → E0 and unchanged week-2 gate.
Consent/listener recruitment, wired loopback equipment, and signing/notarization
are still external prerequisites for their respective later gates. No latency or
identity target was relaxed, and no missing result was marked done.
