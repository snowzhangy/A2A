# Streaming source audio into the local Hy-SimulMT worker

1 November 2026. Direct audio translation (`--asr audio --mt hy-local`) used to send each span to
the worker only once it closed. The worker then fed it as 1600/800 ms turns, one after another, so a
3 s span cost about 0.8–1.0 s after its commit. Most of that was turns whose audio had arrived
seconds earlier. The live app session at 11:05 had a 1.06 s median Hy time inside a 4.7 s median
onset.

## What changed

- **Spans stream while they are spoken.** `AudioStreamFeed` (A2ACore) sends the open span's audio
  up to the VAD's last voiced sample (`EnergyVAD.voicedEnd`), in steps of at least 100 ms. A later
  speech end reports that sample or a later one, so nothing streamed lies past its span's end. On
  close, the feed sends the rest of the span and a flush. A span the segmenter drops (under 0.25 s)
  is withdrawn with `discard`. The worker answers complete turns as the audio arrives, so only the
  last partial turn, and any ST fallback, waits for the commit. The reducer is unchanged: the driver
  closes the span with the worker before the reducer commits it, and the translation request
  redeems that span's answer.
- **The worker gives the same answer for a streamed span.** At 48 kHz, a stateful decimator
  evaluates the same 127-tap filter, in the same summation order, only at the kept samples. The
  pieces therefore resample bit-for-bit as the whole span did, at a third of the work.
  `hy-worker --self-test` checks this without a model. The conversation's reset rules (window,
  gap, direction, context) now apply only where a span starts, as they did before.
- **Task prompts stay resident.** The "first slice ~245 ms" in the worker's timings was not audio
  encoding: a 0.9 s first turn cost the same. It was the encoder waiting behind the asynchronous
  prefill of the task prompt, both for each new conversation and for every ST fallback. Both
  prompts are now decoded once per direction and kept in the KV cache: seq 0 for the conversation,
  seq 1 for ST. A reset removes only what follows the prompt. First-turn encode plus prefill went
  from 360 to 146 ms, and an ST of 2.4 s went from 462 to 255 ms.
- **Frames leave on a serial writer queue.** The worker reads stdin only between turns, so a full
  pipe now blocks that queue instead of the capture loop.
- **The new path is gated on worker protocol 2.** The worker's `ready` frame announces
  `"protocol": 2`. An older worker gets whole spans, and the pipeline logs a rebuild hint.
  `--no-hy-stream` restores whole-span sending for A/B runs.
- **An unstreamed span now fails.** While streaming, a span that reaches its request without a
  ticket fails instead of being sent whole. A whole send would interleave with the open span's
  audio and shift every later answer by one span.
- **Worker timings are traced.** `mt.result` carries `worker_ms` (worker time after the span
  closed), `worker_turns` and `worker_st`.

## Evidence

**Equivalence**

- Greedy decoding, so identical audio gives identical text.
  - Old and new worker on 16 kHz input (refactor only): identical text over 25 spans.
  - Whole and streamed spans at 48 kHz (100, 53 and 37 ms pieces): identical text hashes.
  - Full pipeline replay, old worker with whole spans against new worker with streaming (before
    resident prompts): all 28 committed targets and spans identical.
- Resident prompts change how the prompt is batched and where KV cells sit, so results are not
  bit-identical.
  - Two of three fixtures matched exactly.
  - With 3 s cuts, 2 of 25 spans differed at a near-tie: an already mistranslated word, then a
    synonym in the following span. This is noise-level divergence, not a measured quality change.

**End-to-end latency**

- Run setup:
  - Paced real-time replay of a 73.6 s synthetic two-speaker Mandarin dialogue (system voices).
  - Live app configuration: hy-local, `--dual-clone` Qwen xvector, `--fluent`, `--speakers auto`,
    `--adaptive-prebuffer`, 8 s / 20 s budgets.
  - No other GPU load. Two runs each.
- Onset is measured from source span start to rendered playback start.

| | Hy MT median / p90 / max | Onset median / p90 / max | Idle-bus onset median | Played |
| --- | --- | --- | --- | --- |
| Before (whole spans) | 857 / 1170 / 1280 ms | 4.23 / 4.70 / 4.91 s | 4.23 s | 23 of 28 |
| Streaming | 393 / 715 / 821 ms | 3.54 / 3.82 / 4.05 s | — | 24 of 28 |
| Streaming + resident prompts | 340 / 459 / 590 ms | 3.50 / 3.74 / 3.84 s | 3.63 s | 24 of 28 |
| Same, installed app helper | 370 / 481 / 643 ms | 3.56 / 3.84 / 4.30 s | 3.67 s | 24 of 28 |
| Before, with `--background` | 871 / 1240 / 1277 ms | 4.25 / 4.66 / 5.02 s | 4.25 s | 23 of 28 |
| After, with `--background` | 340 / 459 / 580 ms | 3.52 / 3.76 / 3.80 s | 3.62 s | 24 of 28 |
| After, live tap (`--background`) | 318 / 503 / 601 ms | 3.58 / 3.72 / 3.91 s | 3.63 s | 25 of 27 |

- Underruns were 1 in every replay, before and after.
- The `--background` rows add the app's separated background bus (Apple `AUSoundIsolation`).
- The live tap row used the app helper's full flag set and tapped ffplay playing the same clip,
  with 15 s of lead silence. It had 0 capture overflow, 0 record drops and 0 underruns, and every
  `mt.result` carried worker stats.
- Its single error and `runtime_failed` are ffplay exiting at the end of the clip ("source app
  exited"), which ended the last unit.
- Worker time after close, with streaming, by span type:
  - Spans the 3 s cut closed: 353 ms median.
  - Spans a speech end closed: 431 ms median.
  - Most of the speech-end cost is ST fallback, which 8 of 19 such spans needed (about 700 ms
    each before resident prompts).

## Not adopted

- **Shorter clone-worker chunks.** `--interval 0.16` lowered isolated first-chunk time from 107 to
  69 ms. End to end it saved only 58 ms of median onset: leading silence is trimmed before the first
  chunk is emitted. It also costs 11% more TTS GPU time, so the default stays 0.32 s.
- **Qwen code predictor.** The 15-pass predictor dominates each 24 ms codec step. `mx.compile` over
  it saved 10% with identical tokens, but it would require forking the pinned mlx-audio generation
  loop. Live first-chunk time (about 280 ms against 110 ms isolated) is set by GPU contention, not
  by that loop.

## Remaining latency, largest first

1. The 3 s maximum span (segmentation policy).
2. The 300 ms VAD hangover before a speech end can close a span.
3. ST fallback on unanswered tails.
4. TTS first audio under contention.

Committing target text per Hy delta, instead of per span, would attack the first item, but it
changes unit semantics.

## Limits

All numbers come from one synthetic dialogue on one Mac, through replay and one live tap. There was
no listener check and no real programme audio. Hy-SimulMT and Qwen cloning remain research-only.
