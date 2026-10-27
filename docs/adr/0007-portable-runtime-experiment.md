# ADR-0007 — Portable runtime experiment

Status: experimental, 27 October 2026. Related: [ADR-0001](0001-language-boundary.md),
[ADR-0002](0002-process-topology.md).

The macOS Swift application remains the native Mac path. A separate Python runtime under
`runtime/` ports the pure reducer and uses the same C ring and mixer sources. Its Windows
host uses WASAPI process loopback and a render endpoint. Golden traces and deterministic
audio tests check part of the port's behavior; they do not establish production equivalence.
The Python runtime is an experiment and does not supersede the native, isolated inference
architecture in ADR-0002.

The portable host starts separate local processes for faster-whisper, Qwen translation,
and experimental voice cloning. Model paths come from enabled, verified manifest entries;
setup is explicit and inference must not fetch weights. Recognition runs in CTranslate2
on CUDA or CPU. Translation uses PyTorch CUDA, MPS, or CPU. OmniVoice's PyTorch backend
uses CUDA, MPS, or CPU. Actual concurrency, throughput, memory use, model quality, and
device support need measurement for each hardware configuration. Model provenance alone
does not qualify a model for product use or speaker identity.

On Windows, session volume is ducked and loopback gain restored rather than relying on
mute. Session-volume persistence, crash recovery, clipping, other applications changing
volume, output-device changes, and the resulting audible behavior require interactive
qualification before takeover is relied upon. The guard and journal are recovery aids,
not evidence that every failure mode is safe. Until that work passes, use this path only
for controlled development experiments. On macOS, live capture remains in the Swift app.

The portable policy is a second implementation: every policy change must keep the Swift
and Python traces aligned. Worker queues are bounded, speech and translation are never
written to default diagnostics, and failures are propagated to the host. Full parity
requires a native Windows app, packaging, hardware and lifecycle tests, and evaluation
against consented or synthetic speech. Offline agent-directed dubbing, video handling,
soundtrack separation, and per-line QA are separate capabilities and are not provided by
this decision.
