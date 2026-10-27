**ADR-0002 — Keep audio playback in the host and isolate inference**

Status: selected execution baseline, 24 October 2026; worker topology requires the week-1 runtime probe. Related: [implementation plan](../../IMPLEMENTATION_PLAN.md), [language boundary](0001-language-boundary.md).

The application host owns the process tap, aggregate/output devices, original-audio ring, mixer, session epoch, and playout generation. A bundled XPC service owns resident models and emits recognition, translation, profile, and PCM results. The inference service never renders sound. CLI feasibility experiments can initially combine these components in one process.

Start with bounded XPC batches rather than shared memory. Messages contain version, epoch, generation, sequence, sample format, and frame count. Validate payload sizes and sender identity, acknowledge consumed batches, cap in-flight work, and cancel outdated requests. Audio callbacks never call XPC. Move to shared memory only if profiling shows a material transport cost.

Load and warm models before taking over playback. With healthy capture/replay established, hold the source in mute-when-tapped mode and play the captured original through the host mixer. Translation, monitoring, and recovery use gain ramps within that mixer. Takeover must wait for the audio lifecycle tests; permissions or unavailable models must leave ordinary playback usable.

Profile readiness alone does not start translated playback. Require a fresh, current-generation unit with buffered PCM and a defined source handoff boundary. Track any repeated/original-only source interval. Prepared voice conditioning can be reused; previously spoken text remains immutable.

On worker failure, stall, or excessive source lag, the host cancels pending units, advances the playout generation, and ramps to live original audio. Initial target: recovery within 250 ms of detecting the failure, including a 20–50 ms ramp. Measure that target. A flushed queue does not prove recovery; require a healthy worker, a new unit, buffered audio, and a stable retry interval.

A host crash requires OS tap teardown to release playback; the dead host cannot rescue itself. Qualify stop, force termination, device changes, and permission transitions with FixturePlayer before relying on this behavior. Process isolation also cannot guarantee survival under system-wide memory pressure, so measure and cap resident usage.

Week-1 probe: load a representative selected MLX model inside the signed bundled XPC service; test Metal resource loading, cancellation, worker crash/restart, and host original-audio recovery. If that topology fails, use a supervised bundled helper behind the same contract and record the reason before E1. Hardware/permission probes run on a provisioned interactive Mac; ordinary CI covers pure contracts and compilation.
