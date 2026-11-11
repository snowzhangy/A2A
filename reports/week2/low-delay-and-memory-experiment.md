# Three-second delay and Qwen memory experiment

7 November 2026. The live English-to-Mandarin session used an 8 s picture delay,
automatic speaker tracking, local Hy, and two Qwen workers. Its diagnostic trace
showed translations reaching captions while some units expired waiting for speaker
attribution; one later unit returned no Qwen audio after its retry. No live audio or
transcript was retained for this investigation. The observed trace alone does not
establish how often a listener heard each fallback.

The low-delay experiment caps continuous source spans at 1.8 s for a 3 s picture
delay, with a 0.4 s dip window. The original policy had a 3 s floor. Shorter spans
can split phrases and affect translation quality. At delays under 4 s the existing
original-voice restoration path is unavailable. Neither a three-second delay nor
voice identity is guaranteed.

A 27 s synthetic English clip and a fixed synthetic-speaker reference were replayed
with local Hy, background separation, and Qwen, using one speaker and a 3 s picture
delay. This is one clip; the runs were separate stochastic generations.

| Qwen workers | Source units | Played | First TTS chunk, estimated source-start median / max | Playback lateness median / max |
| --- | ---: | ---: | ---: | ---: |
| Two | 18 | 18 | 2.20 / 2.68 s | 5 / 53 ms |
| One | 18 | 18 | 2.15 / 2.64 s | 4 / 57 ms |

First-chunk estimates add the source span duration to measured translation-commit to
first-chunk time; they exclude any gap between the source-span end and its commit.
Neither run reported a TTS error or truncated take. These figures do not measure
recognition or translation quality, identity, real-device presentation latency, or
rapid multi-speaker exchanges.

During the two-worker run, macOS `top` showed snapshots of roughly 2.0 GB per Qwen
process; the single-worker run showed roughly 2.0 GB for its worker. These process
footprints are not additive system-memory savings: compression and shared pages vary.
The app now offers a one-worker mode. Hy runs separately; the language identifier
is used only when same-language passthrough is enabled at a delay of at least 4 s.

Loading Qwen once and running two simultaneous cloned-voice streams remains an
unimplemented optimization. The installed mlx-audio continuous batching path is
non-streaming and rejects reference-conditioned requests; the current adapter also
mutates model-level conditioning state and uses MLX's global random state. Sharing
that object across concurrent jobs requires isolated per-job state, safe scheduling,
cancellation and PCM routing, and a real-model overlap/quality benchmark. Two Python
threads alone do not establish those properties.

An isolated probe loaded one Qwen model, prepared two synthetic voice objects, and
generated two synthetic phrases. Sequential generation completed. Invoking the
same adapter concurrently on two Python threads failed with
`RuntimeError: There is no Stream(gpu, 3) in current thread`. This rules out that
naive implementation, not every possible shared-model scheduler. Probe source and
logs are in the ignored `.local/scratch/low-delay-memory/` directory.
