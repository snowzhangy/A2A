# ADR-0008 — Restore separated background sound and a responsive default

Status: accepted for local development, 27 October 2026, following the user's report:
"delay too much and no background sound anymore".

The app defaults to streaming translated speech with adaptive buffering, an 8-second
estimated-debt budget and an 8-second source-lag budget. **Prioritize complete phrases**
remains available but is no longer the default; its 20-second budget in ADR-0007 caused
an unacceptable delay in the user's watching experience. These are policy limits,
not promises of measured latency or full dialogue coverage.

**Keep music and effects (experimental)** is enabled by default in this local research
app. It uses the built-in macOS AUSoundIsolation unit in high-quality voice mode on
ordinary threads, processing incoming 48kHz mono blocks continuously. A second unit
with the same processing path and dry mix aligns the source with the isolated voice;
their difference is the candidate background. ASR and voice acquisition continue to
use the original source, since isolation has not improved recognition in our tests.

This amends ADR-0004's silence-only recovery: when requested, separated background
continues during voice acquisition, between English phrases and during recovery.
The original source bus remains muted in Translated mode. Selecting Original mutes
the separated background bus while the original track is re-rendered, preventing two
copies of the soundtrack. Stopping fades the background out. Separation failure mutes
that bus and never substitutes unprocessed source audio. There is no runtime download.

The optional background ring and scratch space are allocated and prefaulted before
callbacks start. The C mixer drains the bounded mono ring, smooths gain changes and
dry/resumed edges, and adds it independently of speech. Translation flushes leave it
alone. No separation, resampling, allocation, locking, logging or Swift runs in the
IOProc. The ring keeps current soundtrack timing; it does not delay the source video's
picture to match translated speech. The current background path is mono.

Source separation is imperfect: absent ASR detections are not proof that voices are
inaudible. The app toggle identifies the feature as experimental. This local decision
does not qualify residual-dialogue suppression, stereo fidelity, voices, Bluetooth
latency, or external release. Test raw audio and full transcripts remain in `.local/`.

DoD: `scripts/check.sh`, `scripts/build_app.sh`, `scripts/smoke_app.sh`; causal separator
and actual tap evidence are recorded under `.local/live-dub-lab/`.
