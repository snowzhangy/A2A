# One batch per turn in the local Hy-SimulMT worker

1 November 2026, later. Following the streaming-input work, the next thing standing between a committed
span and its text was not the audio tower and not the answer length: it was how many times a turn submits
its prompt. `mtmd`'s helper evaluates each input chunk as its own batch — the text before the audio, the
audio, the assistant header — so one turn was three full passes over the model for about 90 tokens.
This is worth money here because the model is a 30B MoE at Q4_K_M: every `llama_decode` reads the whole
active weight set (~2 GB for the ~3B active parameters), so the number of submissions costs about as much
as the token count. On an M3 Pro that floor is ~15 ms a pass, and the two extra passes also queue two
extra Metal graphs behind the Qwen3-TTS workers that share the GPU in the live app.

## What changed

`research/hy_worker/hy_worker.cpp` now builds the whole turn in one `llama_batch_ext`: text rows carry
token ids, audio rows carry the tower's embeddings, positions run continuously, the causal mask is the
uniform one the model was trained with, and logits are requested on the assistant header's last token so
the first sampled token needs no extra pass. One `llama_process` call per turn instead of three.

- **On by default** (`merge_prompt`), so `a2a-pipeline --mt hy-local` and the app get it with no new flag.
  `--no-merge-prompt` runs the old path for comparison.
- **It falls back rather than guesses.** A turn goes through `mtmd_helper_eval_chunks` when the model uses
  M-RoPE, when an audio chunk is non-causal, when a chunk's token and position counts disagree, or when the
  turn would exceed `n_batch` — none of which happens for this model at 800 ms turns, but all of which are
  checked per turn. The return code separates "shape did not apply, nothing decoded, the helper still can"
  from "a decode failed halfway", and only the first allows a second pass; a half-decoded turn fails the
  conversation the way any failed turn does.
- **Nothing else moved.** The protocol, the turn schedule, the ST fallback and the Swift host are untouched.

The same measurement caught a bug introduced on the way: `mtmd_helper_eval_chunks` returns 0 on success, and
an intermediate refactor tested it as a bool, so every turn "failed" and reset the conversation while the
benchmark still printed plausible-looking milliseconds. Output-hash checking is what caught it, which is why
every number below carries a hash of the translated text.

## Measurements

Apple M3 Pro, 36 GB, `hy-simulmt-30b-a3b-q4km-gguf` (Q4_K_M), fixture
`.local/fixtures/youtube-NRB0ICxMkrM-0940-stage.wav`, 10 spans of 3 s, streamed at 100 ms per append as the
live pipeline streams it. Two independent runs per row. Reproduce with `scripts/bench_hy_local.sh` (`--load`
adds the last two rows).

| region | condition | median span | median after flush | worst span |
| --- | --- | --- | --- | --- |
| 10–40 s | per-chunk batches (old) | 981 / 985 ms | 359 / 363 ms | 1126 / 1127 ms |
| 10–40 s | one batch per turn | 917 / 947 ms | 349 / 348 ms | 1024 / 1026 ms |
| 40–70 s | per-chunk batches (old) | 940 / 947 ms | 339 / 341 ms | 1110 / 1110 ms |
| 40–70 s | one batch per turn | 871 / 880 ms | 287 / 290 ms | 1012 / 1010 ms |
| 10–40 s, Qwen3-TTS generating in another process | per-chunk | 1270 / 1288 ms | 472 / 467 ms | 1457 / 1433 ms |
| 10–40 s, Qwen3-TTS generating in another process | one batch | 1159 / 1163 ms | 441 / 445 ms | 1279 / 1276 ms |

So: 5–7% off the span, 12–52 ms off the wait after commit, and about 100 ms off the worst span idle; under
the contention the product actually runs in, 9% off the span and 155 ms off the worst span. Fewer
submissions matter most when another process is queueing Metal graphs, which is the argument for keeping this
even where the idle gain looks modest. Per turn, the CPU-side submit fell from ~108 ms to ~23 ms; the GPU
work did not shrink, it moved into the sample read, which is why span totals move less than the
`encode+prefill` line in `--verbose`.

## Does it change the translation?

Not in content, but not bit-for-bit either, and that is worth stating precisely because the rest of this
report is a speed claim.

- On 10–40 s, all 10 spans come out with the same text as before (hash `fb3e5ae1dea5f9da`, which is also what
  the worker produced before this change).
- On 40–70 s, 7 of 10 spans match and 3 pick a different near-equal token: "buns are for **customers**" vs
  "**guests**", "all alone, yet **dared**" vs "yet **she dared**", "the feud of **G'er**" vs "**Ge'er**".
  Same meaning, no missing or repeated content, and each divergence is followed by spans that hash identically
  again — so the KV and the positions agree, and only a sample flipped.

That is what changing the batch shape does to floating point: a different tiling and reduction order moves a
logit by an ULP or two, and greedy decoding shows it whenever two tokens are nearly tied. Both paths are
deterministic run to run (identical hashes above across two runs each); they are only different from each
other. There is no bit-exact way to merge three passes into one. What follows for practice:

- `--no-merge-prompt` is the escape hatch. If a quality run (COMET on the dev set) reads the merged path as a
  regression, that flag restores the previous tokens exactly.
- **Quality metrics have not been re-run for this change.** Content-level equality is what was checked here,
  on two regions of one fixture. The dev-set metric pass is the thing that should bless or revert this, and it
  is listed as a next action.
- The bench hash is a review signal to be read line by line (as golden traces are in this repo), not a
  pass/fail gate: a changed hash means "read the diff", and the diff above is what a benign one looks like.

## Tunings measured and rejected

Same fixture and command, one flag at a time. Kept out so the next person does not spend an afternoon on them.

| flag | result | verdict |
| --- | --- | --- |
| `--kv q8_0` | median 943 vs 916 ms, **and different text** (`1aa75fc076cd2dac`) | rejected: slower and not equivalent |
| `--no-kv-unified` | 911 vs 916 ms, same text | noise; keep unified cells |
| `--flash-attn on` / `off` | on 902 / off 912 ms, `auto` reports `enabled` and matches `on` | keep `auto` |
| `--mlock` | 908 / 916 vs 908 / 917 ms, same text | neutral here, and it pins 18 GB out of pageable memory beside two ~1.6 GB TTS workers; this Mac has already OOMed a model load |
| `--tower-threads`, `--threads`, `--threads-batch` | no material change | the tower is not the bottleneck at 800 ms turns |
| flush straight to ST over all unanswered audio | not implemented: in 10 spans, 7 ended with a turn that answered text, whose text would then arrive later from ST instead of early from the turn | rejected on the span structure |

The shape of the remaining wait, from the same run: 7 of 10 spans close on a turn that answers text
(216–351 ms after flush), 3 need ST over the unanswered tail (245 / 428 / 606 ms). Greedy decode is
~28 ms/token (~39% of the M3 Pro's bandwidth floor for this model), so an answer of 4 tokens is most of a
turn and the tail turn's answer is most of the post-commit wait. Cutting that needs a faster decoder or a
shorter tail, not more batching.

## TTS worker baseline, and why it was left alone

`research/tts_bench.py` (new) drives `research/tts_worker.py` over its own protocol and reports what the
cloning path costs: prepare with a 3 s x-vector reference 10–13 ms, first chunk 202–216 ms, real-time
factor 0.34–0.44 (2.3–3× faster than the playout it feeds). A cProfile pass over the pinned mlx-audio
`generate` shows 15 code-predictor calls per codec token (1905 calls for 127 tokens), with the call and
graph-build overhead around 13% of the loop and the rest kernel time inside `generate` itself. `mx.compile`
over that loop is the known next step (worth ~10% in the earlier dual-worker trial) and still needs a fork
of the pinned generation loop, so it stays out — same conclusion as
[dual-qwen-prefetch.md](dual-qwen-prefetch.md), now with a runnable client to re-check it against.

## Checks

- `scripts/bench_hy_local.sh` — old vs new path, idle and under TTS load, with the text hash on every line, so
  a speed claim can never be made silently next to a change in output. Read a changed hash the way this report
  reads one: `--text` on both sides and compare line by line.
- Still to run before this is blessed as quality-neutral: the dev-set metric pass (COMET and the harness
  onset/underrun metrics) with and without `--no-merge-prompt`, on the dev split only.
- `.local/hy-simulmt/bin/hy-worker --model GGUF --self-test` — streaming resampler still exact.
- `uv run --project research python research/tts_bench.py --reference REF.wav` — TTS first-chunk and RTF.
- `scripts/check.sh` — the deterministic gate; no Swift or harness file changed for this work, and the
  worker is research-only (never linked into the app), so no gate depends on it.
