# Where the 3.5 s goes, and what is left to cut

1 November 2026, evening. After one batch per Hy turn, the question was the whole pipeline: from the moment
a phrase starts in the source to the moment its English starts playing. This page splits that onset into its
stages, measures the three ideas that looked like they could shorten it without changing what is said, and
records why none of them is a default. What remains are two segmentation choices, and those change the units
themselves.

## The onset, stage by stage

Paced real-time replay of the 73.6 s synthetic two-speaker dialogue in the live app configuration (installed
helper, `--asr audio --mt hy-local`, `--dual-clone` Qwen xvector, `--fluent`, `--speakers auto`,
`--adaptive-prebuffer`, 8 s / 20 s budgets), two runs. Per unit, from the trace:

| stage | median | what sets it |
| --- | --- | --- |
| span length (source speech in the unit) | 2.17 s (max 3.0) | `AudioSegmentPolicy` max span 3 s |
| VAD hangover, speech-end spans only | 300 ms | `EnergyVAD.hangoverMs` |
| Hy after commit | 331 / 331 ms | last partial turn, plus ST on unanswered tails |
| TTS request to first chunk | 281 / 260 ms | Qwen prefill + 0.32 s of audio at RTF ~0.4 |
| first chunk to playback | 100 / 102 ms | waiting for the second chunk (jitter reserve) |
| **onset** (median / p90) | **3.54 / 3.78 s, 3.52 / 3.76 s** | sum of the above |

Underruns 1 and 1, 24 of 28 units played in both runs. A 3 s cut span adds up as 3000 + 331 + 260 + 100 ≈
3.69 s; a speech-end span as its length + 300 + the same tail. Span length is about 60% of the onset. The
three things after it are each a few hundred milliseconds.

## Measured and not adopted

| idea | measurement | verdict |
| --- | --- | --- |
| Start playback on the first TTS chunk when the session's recent streams were fast | The first chunk is 0.12–0.22 s after leading-silence trim, below the 0.24 s reserve, so a rule bounded by the reserve never fired (A/B: play wait 101 / 100 ms vs 100 / 102 ms, onset unchanged). Without the reserve, across 245 recorded streams, the second chunk came later than the first one lasted in **23 (9%)**, gaps 40–150 ms. | Reverted. The 0.24 s reserve is what keeps phrase starts gap-free. |
| Hold Hy back while a TTS first chunk is pending | First-chunk time by what else ran: Hy idle and TTS alone 249–271 ms median, Hy streaming and TTS alone 240–271 ms. Close to `tts_bench.py`'s isolated 202–216 ms. | Not built: Hy is not what slows the first chunk. |
| Run Hy's tail turn during the 300 ms hangover, on a host "pause" hint | Simulated `EnergyVAD` false pauses (silence that reaches P ms, then speech resumes before 300 ms) per real speech end, below. Overlap ceiling is 300 − P ms, on speech-end spans only. | Deferred. Too little gain on real audio for a speculative KV rollback that cannot be hash-checked. |

False pauses per real speech end:

| pause hint at | overlap ceiling | synthetic dialogue (25 ends) | drama fixture, 120 s (22 ends) |
| --- | --- | --- | --- |
| 100 ms | 200 ms | 0.32 | 1.14 |
| 140 ms | 160 ms | 0.08 | 0.59 |
| 200 ms | 100 ms | 0.08 | 0.23 |

Each false pause would cost a wasted ~250 ms Hy turn on the GPU the TTS first chunk shares. A false pause
late in a 3 s cut adds directly to that span's wait. The drama fixture has 22 speech ends in 120 s, so most of
its spans are 3 s cuts that gain nothing. A rolled-back turn is also not bit-exact, because KV cells can land
elsewhere, so real audio would lose the text-hash check.

Already rejected earlier, not re-run: `--interval 0.16`, `mx.compile` over the code predictor, `--kv q8_0`,
`--flash-attn`, `--mlock`, thread counts, ST straight at flush.

## The levers that are left (a product call)

Both change what a unit is. That is why they are not changed here.

- **Maximum span, 3 s.** This is the largest share of the onset. Cut spans are a third of the units in the
  dialogue and most of them in the drama fixture. Each second off the maximum comes off those units' onset
  nearly one for one. The costs: more, shorter units for Hy and the cloned voice, and translations of
  fragments with less context. That needs the dev-set metric pass before any default moves. Committing target
  text per Hy delta instead of per span attacks the same term, and changes unit semantics further.
- **VAD hangover, 300 ms.** Every speech-end span waits it out. At 200 ms, the table above predicts 0.08–0.23
  extra splits per real speech end, each one a phrase cut where the speaker only paused.

## Fixed on the way: the global tap at a quiet moment

Using the new system-audio source showed something [system-audio-source.md](system-audio-source.md) had
wrong. A global tap's aggregate gets **no IO callbacks at all** while nothing on the Mac is playing:
`a2a-tapctl capture --global` read 0 callbacks in 3 s of silence and 182 callbacks in 2 s with `afplay`
playing. Once something plays, IO starts. It kept running for the rest of a 4 s capture (174 080 frames for a
1 s clip that started 0.3 s in), about 2.7 s past the clip's end. Whether it stops again after a longer quiet
spell was not measured; the stall check below holds either way.

- **Starting a session before the video failed.** The pipeline's 2 s stall check fired:
  `a2a-pipeline --tap-global` on a silent Mac exited 1 with "capture stalled". Missing frames on a global tap
  are now a stall only while some other process has a running output (`ResolvedSource.mixIsIdle`, unit test
  `onlyAGlobalTapWithNothingPlayingIsIdle`); otherwise the check looks again 2 s later. With playback starting
  3 s into a session, the tap captured 188 416 frames, with no failure.
- **The `--seconds` tail now ends by the clock.** It counted captured frames, so with no frames it could never
  end. The stall check had been hiding that: with the stall fix alone, a silent 5 s run sat in its tail for
  about 8.5 minutes, until the output device changed. The tail now has its own deadline. The zero-frame exit
  follows from the code but was not reproduced live, because by then another app held a running output and
  every global run captured it. With frames flowing, a 4 s run with a 3 s tail exits 0 in 5.3 s, as before.
- **An empty exclusion list is refused.** `TapSession` now throws if a global tap would exclude no process of
  ours, instead of hearing our own English. On the build here, the exclusion list held the tool's own pid, as
  expected.

## Checks

- `scripts/check.sh`: 252 Swift tests, 97 harness tests, manifest 17 entries with 0 problems, `== ok`.
- Replays: two runs each, before and after, on the same helper flags; the span list and Hy turn/ST shapes
  are identical across all four runs.
- Not closed here: a listening pass on the global tap's mute behaviour (from the system-audio report), and
  the dev-set metric pass for the merged Hy batch.
