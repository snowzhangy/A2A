**ADR-0001 — Swift-first macOS alpha**

Status: selected execution baseline, 24 October 2026. Scope: the first Mac alpha. Related: [implementation plan](../../IMPLEMENTATION_PLAN.md), [process topology](0002-process-topology.md).

Use Swift for application control, pure policies, and native model adapters. Keep `A2ACore` independent of audio devices, Apple speech frameworks, MLX, networking, and UI. Inject clocks and model events, and specify commitment, cancellation, state transitions, and scheduling through deterministic traces. Keep platform types inside adapters.

Swift model ports provide a useful starting point, but each selected revision still needs compilation, upstream parity checks, and streaming tests. Use only the package products required by the chosen combination. Keep Python in research and evaluation tooling; the product path remains locally packaged native inference.

Real-time audio has a separate constraint: callback work must have bounded execution and explicit memory ownership. Use preallocated buffers and atomics; permit bounded mixing/resampling. Introduce a narrow C/C++ shim for rings and callback operations if Swift/ARC behavior cannot meet those rules. A Swift-first application does not require every real-time operation to be written in Swift.

Windows will use its own capture, UI, and inference adapters. At that milestone, decide whether to port the policy core or introduce a shared C++/Rust implementation. Golden traces provide behavioral evidence for that decision. They do not make the runtime integration or port automatic.

This choice prioritizes proving the Mac experience with existing platform integrations. Revisit it if native adapter compilation/quality fails, dependency scope becomes unmanageable, or Windows porting evidence supports a shared native core. Record any replacement decision explicitly; retain the trace contracts across implementations.

Week-1 evidence: build the minimal selected dependency subset, confirm the toolchain/deployment target, test stale-generation rejection, and record actual streaming capabilities for each adapter. Creating a hosted dependency fork is optional; local revision pinning is enough to begin.
