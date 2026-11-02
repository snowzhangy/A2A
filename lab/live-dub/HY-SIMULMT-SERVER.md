# Hy-SimulMT LAN server: black-box evaluation (29 October)

The app's `--mt hy` path (`HySimulTranslator.swift`) posts each Apple-ASR-committed segment as a WAV
to `http://10.0.1.29:8082/v1/audio/translations?stream=true` and waits for the whole SSE reply. The
server reports `Hy-SimulMT-30B-A3B-Q8_0.gguf` and describes itself as "GGUF via llama-mtmd-cli
(turn-by-turn)". Its source and hardware were not available; everything below is measured from this
Mac over the LAN (ping 8 ms). Probes: the goal scene's 12 lines (ep08 583–620, dub-anything
`script.json` spans), cut from the original 48 kHz mix as the app sends them. Research only.

## Latency

| | measured |
| --- | --- |
| Per request, before the first token | 17.2–24.2 s on every request, back to back (load alone, total minus compute: 17.5–22.4 s) |
| Same, for a 0.5 s clip and for 1 s of silence | 17.2 s and 18.2 s |
| Model compute after that (`run_turn_stream` line) | 0.33–3.6 s per line; 0.5–2.8× real time for short lines, 3.0× on a 29 s file |
| Total per line | 18.2–26.1 s (median 19.5 s) |
| Upload | under 0.1 s to first byte; 16 kHz and 48 kHz give the same text and time |
| Second request while busy | HTTP 429 in 0.05 s |

The fixed 17–22 s is the model being loaded for each request. The cause is structural: in llama.cpp
PR #31198 (`lihy715/llama.cpp.Hyv3Speech` @ e41ca42), `llama-mtmd-cli` sends Hy models
(`MTMD_AUDIO_STREAM_TURN`) to `run_turn_stream` on exactly one `--audio` file and exits; there is no
interactive loop for them, and the PR says `llama-server` cannot run these GGUFs yet. A server built on
the CLI must start a process (and reload 33 GB) per segment.

Effect in the app: the queue is serial (`launchNextTranslation` runs one request at a time). Replaying
the goal scene's 12 lines with each request starting at its line's end (best case) gives English for
line 0 at 25.7 s after the line, line 1 at 47.8 s, line 11 at 225 s. With `--max-source-lag-seconds 45`
the catch-up skips everything after the first one or two lines of a dense scene. That is the "works but
skips" behaviour.

The model itself is fast enough for live use once resident. On one 29 s file (36 turns of 1.6 s + 0.8 s)
it computed in 9.7 s (0.27 s per turn), and its text for each line appeared roughly 1–2 s after the
line ended (estimated by mapping each delta to its turn at uniform compute per turn).

## Quality against the goal dub

Per-segment requests have no context. On the 12 lines:

- Pronouns: 她/他 sound alike, and three lines came back with the wrong gender ("that's him", "his
  legs", "his blade") for the young woman of the previous lines.
- Terms: 全真教 came back as "Quanzhen Sect" in one line and "the whole Zhen family" in the next.
- Fragments: a sentence split across two segments came back as "was cut off by his blade, the ear,".
- Length: 142 English syllables against the goal's 100; median 5.2 syllables per second of the actor's
  line against 3.7 for the goal. 11 of 12 lines exceed the lab's 3.6/s ceiling (goal: 7 of 12). Hy has
  no length control.

The same audio sent as one 29 s file (plus one 12 s file for the end) fixed pronouns ("that's her", "her
blade"), terms ("Quanzhen Sect" both times) and the split sentence ("And then had their ears cut off by
her blade."). Context, not the model, causes the first three problems. Length stays.

Other findings:
- Hallucination: 1 s of silence returned "Yeah."; a 0.5 s clip returned "Right.". Anything the app sends
  without speech will be spoken.
- Separated vocals instead of the mix (4 lines before the run stopped): same text quality, same
  pronoun error. Not worth the separation cost for Hy.
- Robustness: during the run one request ended in `Connection reset by peer`; afterwards `/health`
  timed out (5 s), then answered in 3.5 s with `busy: true` while none of these probes were running,
  so another client (likely the app) was using it. A test of what happens when the app cancels an
  in-flight request (does the server keep working and answer the next request with 429?) did not
  complete and is untested.

## What would make it live and closer to the dub

1. Keep the model resident and stream. Options, by box:
   - llama.cpp (any GPU or Mac): patch `run_turn_stream` to read 0.8 s PCM turns from stdin or a
     socket in a loop, keep the KV across turns, print each turn's answer with a delimiter, and reset
     the window about every 30 s (as SimulInfer and SGLang-Omni do). Wrap that one process in a server
     with SimulInfer's `/translate/ws` protocol (16 kHz float32 frames, 1600 then 800 ms).
   - NVIDIA with room for the FP8 or BF16 checkpoint: Tencent's SimulInfer (vLLM) or SGLang-Omni's
     `/v1/realtime` already do this, with VAD and recovery.
2. App: send audio continuously instead of ASR-committed segments, and use Hy's per-turn clauses as the
   units to voice (Apple ASR stays for speaker and voice references). Then drop the 45 s debt and lag
   caps back to about 8 s.
3. Dub length: a fit step only when a clause is over the syllable budget, or the Interleave model
   (source text plus translation) so the source Chinese can feed the lab's director prompt (glossary,
   speaker, budget).
4. Never voice a turn whose segment had no recognized speech (silence fillers).

## Update, 31 October: resident server and direct audio

The app now saves `http://10.0.1.29:8081` (default in `SessionModel` is still `:8080`, which now
serves an unrelated text model; the readiness check needs HTTP 200 from the root, and `:8080/` returns
404, so it reports unreachable rather than lying). At review time 8081 and 8082 were closed, so the
numbers below come from the peer's 30 October run, `.local/direct-audio-test/trace.jsonl`
(synthetic `zh-demo` fixture, one voice, `--asr audio`, two Qwen 0.6B workers), not from a new probe.

| Stage | measured per unit |
| --- | --- |
| VAD end to commit | about 0.3 s |
| Hy request to result | 0.36–0.80 s (was 18–26 s) |
| Qwen request to first chunk | 0.12–0.32 s |
| Segment end to playback, steady state | 1.0–1.6 s |
| Segment onset to playback, steady state | 2.0–4.3 s (segment length plus about 1.3 s) |
| First line of a new speaker | 3.6 and 5.7 s after segment end: `mt.result` to `tts.request` waits 2.8 and 4.7 s for the 3 s x-vector reference |

What did not change:
- `HySimulTranslator.translate` sends only `direction` and the WAV. The trace's `context_pairs` (1–3)
  never reach Hy, so the context errors above still apply, now per 3 s span; hard cuts at exactly 3.0 s
  (`AudioSegmentPolicy`) can also land mid-word.
- `--asr audio` skips hypotheses (`SessionPipeline.swift:327`), so the 31 October `length_clause`
  commit policy does not affect the app's Hy path.
- `AudioSegmentPolicy` sends spans from 0.25 s, the energy VAD passes music, and Hy output is not
  filtered, so fillers ("Yeah.", "Right.") can still be voiced.
- Direct mode refuses voice memory (`main.swift:263`), so every speaker waits for a fresh reference.

Next, in order of delay saved: known-actor references from the actor DB (removes the 3–5 s
first-line wait, not measured), a server session that keeps KV across spans (allows shorter spans
and restores context), a filler gate for short spans, then streaming SSE deltas (at most about
0.5 s now). Re-run the 12 goal lines against 8081 once it is up.

Scripts (scratchpad, not kept): `hy_probe.py`, `hy_eval.py`, `hy_cancel.py`; raw replies with text
stayed out of the repo.

## Live session, 1 November (app, zh→en, 250 s)

Build with the peer's speaker carry, filler gate and over-budget shortener (all on by default), Hy on
`:8081` (Q4_K_M, resident). Trace: `.local/live-dub/runs/app-sessions/2026-11-01-101208-a2a-4A30FA77-…jsonl`.

| | measured |
| --- | --- |
| Hy request to result | 0.80–1.02 s |
| Qwen first chunk | 0.26–0.46 s |
| Segment end to playback | 1.22–1.42 s (onset 4.2–4.4 s) |
| Pieces with tracker-labeled speech after warm-up | 48: 46 played, 2 silenced as filler |
| Shortened | 30 of 49 played lines; median 62% of Hy's characters kept, minimum 25% |
| Hy length | median 66, max 111 characters per 3 s piece (budget 60 at 15 chars/s + 1 s grace) |

Problems, in order of loss:
1. Warm-up: the session started while `/health` said `"resident": "starting"`. The first request waited
   17 s and expired; the next 10 failed in 0.1 s each. 33 s of speech lost. The app's readiness check
   (HTTP 200 from the root) passes before the model is resident. Fix: wait for `"resident": "ready"`, and
   retry a failed span once.
2. Length: Hy says too much for a 3 s span, so most lines are cut, some to a quarter. `TargetShortener`
   prefers a clause boundary down to 40% of the budget (u17: 74 to 24 characters, 1.4 s of audio in a
   3 s slot). Cut at a word boundary unless the clause cut keeps at least about 75% of the budget, and
   let pace (up to 1.3) absorb the rest; the real fix is length control on the Hy side.
3. Unlabeled stretch (121–151 s): three pieces with non-filler text but no tracker label waited, two were
   skipped by catch-up (debt 10.8 s), one dropped as `no_speaker`. Likely music; carry (6 s) did not reach.
4. Tracker hit its 4-speaker cap at 3:15; later actors share an existing voice (see `SESSION-VOICES.md`).

The filler gate looks right: 12 of 15 silenced pieces had no tracker-labeled speech.

## Local Hy-SimulMT on this Mac (1 November)

`research/hy_local_server.py` serves the app's API on `http://127.0.0.1:8095`, backed by `llama-server` from the
Hy fork (`scripts/build_hy_local.sh`, e41ca42, Metal) and the manifest's `hy-simulmt-30b-a3b-q4km-gguf`.
`llama-server -m X.gguf --mmproj X.gguf` loads the single-file GGUF's audio tower, so no separate projector
is needed. Load is about 25 s warm, plus a 1 s warm-up; `llama-server` holds about 19.4 GB wired (M3 Pro 36 GB,
17% free before the app starts).

Goal scene (ep08 583–620), local only, no app or TTS running:

| | per request |
| --- | --- |
| Per goal line (12) | median 0.84 s, max 1.18 s |
| App-style 3 s cuts (13) | median 1.02 s, max 1.27 s |

Text: one rolling conversation across spans fixes the context errors listed above. "her legs", "her blade" and
"that's her" (the LAN server gave "him/his"), and "Quanzhen Sect" every time. Remaining: a trailing "Yeah." on
one line, an occasional "you know", and 3 s cuts still lose words at the seams (the cut at 601 s gave "arm" for
"ears").

Two cache bugs found and fixed on the way (both caused 2.5–4.6 s spikes, from re-encoding the whole conversation):
the history must replay each answer exactly as generated (not stripped), and the ST fallback needs its own
slot (`-np 2`), or it evicts the conversation's prompt cache.

Live app session on the local server (zh→en, 183 s, two Qwen workers, a2a-08's readiness, shortener and timeout
fixes): 62 pieces with tracker-labeled speech, 60 played and 2 in flight when the session ended; 0 MT failures;
Hy 0.95–1.10 s median per 30 s window; Qwen first chunk 0.23–0.53 s (Hy and Qwen share the GPU); segment end to
playback 1.6–1.9 s median, 3.6 s at the start of a continuous stretch (the first voice's 3.3 s wait is never won back
until a pause). Shortened lines kept a median 74% of Hy's characters (minimum 48%; this morning 62% and 25%). Four
pieces with Hy text but no tracker speech were dropped as `no_speaker`. Memory 12–19% free, swap steady. The
session ended because a rebuild of `.local/apps/A2A.app` re-signed the running bundle (SIGKILL, Code Signature
Invalid), not because of the model.

## Native worker replaces the local server (1 November)

At the user's request the HTTP layer is gone: `research/hy_local_server.py` is deleted. `research/hy_worker/hy_worker.cpp`
links the fork's libllama/libmtmd directly, keeps the conversation in its own KV cache, and talks to
`A2AModels.HyLocalTranslator` over the clone worker's stdin/stdout frames; a2a-pipeline starts it per session.
Ready in 2.9–3.8 s with the GGUF in page cache (load 1.5–2.6 s plus warm-up 1.0–1.2 s). Same text as the server.

| Goal scene, per request | server | native |
| --- | --- | --- |
| Goal lines (12) | median 0.84 s, max 1.18 s | median 0.77 s, max 1.33 s |
| 3 s cuts (13) | median 1.02 s, max 1.27 s | median 1.03 s |

Per turn: audio encoder 50 ms, each model pass 25–35 ms whatever its size (the active MoE experts are read every time),
answer tokens about 25 ms each. One prompt per turn (text, audio, text) and an end-of-turn token carried into the
next turn's prompt cut the waiting turn from six passes to three. What remains in span mode is the four turns of a
3 s span run back to back after the cut. Feeding audio as it arrives (the worker's `append`) would leave only the
last turn and the flush after the cut.
