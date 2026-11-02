# ADR-0011 — Optional Qwen LiveTranslate cloud session

Status: local development experiment, 30 October 2026, explicitly requested by the user.
This adds an opt-in exception to D8 for this backend only. The existing Hy and local
voice path remains the default. Selecting the cloud option sends the captured source
app's audio continuously to QwenCloud in the chosen Singapore or Beijing region;
service charges and the provider's data handling apply. The UI states this before
starting. A user-entered API key stays in app memory and is passed once through the
helper's control pipe; it is not stored in defaults, arguments, environment, or traces.

The provider does turn detection, translation, and voice cloning in one session.
Cloning uses frequency `always`, intended for changing speakers, but the provider may
use a default voice before cloning completes and attribution/identity is unqualified.
The cloud backend does not apply local background separation, terminology,
phrase-first scheduling, voice memory, or per-speaker voice gates. These limitations
are explicitly reflected in the UI. The original/translated switch uses the existing
C mixer and clears queued speech when switched; callbacks never perform network I/O.

The cloud session is a separate streaming driver. It feeds provider PCM and playback
choices through `SessionPipeline`, whose provider mode owns playback gains, admission,
flushes and stop behavior. Provider events do not expose the same committed source
spans or voice profiles, so the local per-unit attribution and catch-up policies do
not apply. Its PCM queue is bounded; overflow or capture loss stops the session and
releases the tap. Live speaker attribution/identity, latency, queue-pressure, and
failure testing remain necessary before external release. Provider source
and target text appear only in transient app events; no text or audio is added to
standard diagnostic traces. Stop requests end capture and release the tap; final
in-flight speech may be discarded.

DoD: `scripts/check.sh`, `scripts/build_app.sh`, `scripts/smoke_app.sh`. An actual
Qwen account and API key are additionally required for live provider verification;
synthetic protocol and UI tests do not establish cloud quality or latency.
