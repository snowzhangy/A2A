**ADR-0003: One aggregate IOProc for capture and re-render**

Status: accepted for the alpha host, 24 October 2026. Refines [ADR-0002](0002-process-topology.md) and plan section 4.5, which describe separate capture and render callbacks. Related: `apple/Sources/A2AAudio/TapSession.swift`, `apple/Sources/A2ARTShim/engine.c`.

## Decision

The host creates one private aggregate device that contains the process tap and the current default output device as its main sub-device. A single C IOProc (`a2a_engine_ioproc`) receives the tapped input and the device output buffers in the same cycle. In that cycle it:

1. copies the input into the capture ring and the original-bus delay line;
2. renders the original bus at the delay set by the control thread, plus the translated bus, with per-sample gain ramps;
3. appends one capture and one output timing record to its SPSC record ring.

The capture and output records stay distinct. Their trace events remain `buf.capture` and `buf.output`.

## Rationale

- The original bus is re-rendered from the capture (C11). When capture and output share one cycle and one clock, the re-render path adds roughly one I/O cycle plus the device latencies. Without it we would need cross-device drift correction, and that path is also where the ≤60 ms A2 target is most likely to slip.
- There is one real-time callback to audit instead of two. That means one set of preallocated buffers and no cross-callback handoff.
- The HAL compensates drift between the tap and the main sub-device (`kAudioSubTapDriftCompensationKey`). That compensation is HAL resampling, which we have not yet measured. The `clock.drift` measurement and the loopback test (A2) must cover it.

## Consequences

- An output-device change or sleep/wake rebuilds the aggregate. That is a source/device discontinuity: the epoch increments and pending units are canceled.
- Aggregate input channels list the sub-device inputs before the tap channels. The engine skips `inputChannelOffset` channels. The offset is derived at setup, and a test covers it with a synthetic buffer list (`IOProcAdapterTests`). Confirm the ordering on hardware during A1.
- If a future output path needs a device that is not the aggregate's main sub-device, such as a separate monitor output, a second callback and the week-4 adaptive resampler become necessary. Record that change in a superseding ADR.
- `AudioDeviceStart` on the aggregate blocks until the user answers the System Audio Recording consent prompt, as observed on 24 October 2026. Start only after a permission preflight, and never on the UI thread.
