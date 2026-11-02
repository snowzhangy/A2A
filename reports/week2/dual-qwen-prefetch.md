# Two-worker Qwen prefetch experiment

The development app enables `--dual-clone` for Qwen. Two independent resident
workers each prepare the same speaker-specific profiles. The driver permits the
next phrase to render only after the front phrase has started playing, and keeps
at most two phrase records: the playing phrase and one future phrase. PCM stays
on the ordinary driver path and the reducer retains speaker, epoch and generation
checks. Canceled or unattributed units never borrow another speaker's voice.
Other cloning models and deterministic virtual replay retain one worker.

On 29 October 2026, `research/live_dub_lab/benchmark_parallel_qwen.py` ran two
warmed resident Qwen workers on one synthetic 24 kHz Mandarin reference and two
fixed English prompts. Across three alternating serial/parallel trials, the median
time to generate both phrases was 3.21 s serial and 2.34 s parallel. First-chunk
times rose from approximately 0.14–0.16 s serial to 0.21–0.22 s in parallel. This
is an isolated, same-Mac TTS test, not live performance, speaker-similarity evidence,
or a memory benchmark. Two models require additional memory and startup time.

The deterministic fake-worker integration test verifies that the second phrase
starts generating before the first has finished, playback remains in order, and a
third phrase cannot start until the second begins playing. A separate synthetic
Qwen test with two phrases played both in order. A real-server replay of the longer
synthetic Mandarin clip completed only one of four units: one was skipped for
catch-up and two translation requests failed, so it does not demonstrate an end-to-end
coverage improvement. The first profile still waited for finalized recognition.

Both real workers printed an existing Python resource-tracker warning for a leaked
semaphore when the process stopped; the test process nevertheless exited normally.
The design does not fix missing speaker references, request failures, or delayed
recognition. All Qwen cloning remains research-only and identity-unqualified.
