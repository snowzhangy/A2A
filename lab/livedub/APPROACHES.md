# Two ways to dub live on this Mac, measured

Question: the offline dub (`dub-anything`, 12/12 lines ending within 0.25 s of the actor) is
offline — can a live pipeline sound like it, at 2–3 s of delay?

Two labs on this machine answered it in parallel. Both are on the same 37 s scene
(ep08 583–620 s, the inn), on one M3 Pro / 36 GB.

* **A — `lab/live-dub` (the other session): delay the programme.** Separation, per-line
  references, OmniVoice told the slot length, an actor database, and a simulator that replays
  measured stage costs for any presentation delay D. Their numbers are in
  [`lab/live-dub/RESEARCH.md`](../live-dub/RESEARCH.md).
* **B — `lab/livedub` (this one): speak as soon as you can.** Delayed bed with ducking instead of
  separation, a streaming clone so playback starts on the first chunk, and no picture handling.

## Stage costs, this machine, uncontended

| Stage | Model | Cost | Note |
| --- | --- | ---: | --- |
| endpoint | silero VAD (CPU) | 0.25 s | the `min_silence` wait; VAD compute is 0.3% of real time |
| recognise | SenseVoiceSmall (MLX) | **0.03 s** p50 per utterance, RTF 0.03 | CER 12.6% vs the published subtitles |
| recognise | Qwen3-ASR 0.6B | 0.17 s p50 | 11.6% CER; 1.7B: 10.5% |
| translate | Qwen3-30B-A3B-4bit | **0.73 s** p50, first token 0.42 s | 9B dense is 3.4 s; the 2.5 s figure in RESEARCH.md was a contended run |
| speak (streaming) | Qwen3-TTS 0.6B | **0.57 s** to first audio, RTF ≈0.8 | prepare a voice: 0.13 s |
| speak (whole line) | OmniVoice, 16 steps | ~3.8 s first audio | 8 steps: 0.56 s per line; in-slot timing by asking for `duration_s` |
| voice from mix | AUSoundIsolation | 0.15 s latency, off the GPU | their measurement; the enabler for a voice-free bed |

Processing from the actor's last sample to the first English sample, measured in the live run:
**1.67 s** (0.25 endpoint + 0.07 ASR + 1.04 MT + first audio of the clone). So a 2–3 s budget is
met with about 0.8 s to spare, and the GPU is not the binding constraint at that rate.

## What each one delivers

| | A: delay the programme | B: speak now |
| --- | --- | --- |
| delay needed for the English to land in the actor's own window | 10–15 s (simulated: 77–80% coverage at D=10, 87–100% at D=15 with OmniVoice at 8 steps) | not achievable — see below |
| delay 2.5–3 s | 0% in-slot; their simulator drops everything at D=0–3 (11–12% coverage at D=3) | **works**: 6 of 8 units placed, lag p50 2.50 s, p90 3.31 s, 0 underruns |
| bed | separated: voice out, music and effects kept (RoFormer 0.23–0.42 RTF, or AUSoundIsolation off the GPU) | original mix ducked to −8 dB under the English, so the Mandarin is still faintly there |
| timing of the picture | needs a delayed mirror (ScreenCaptureKit → VideoToolbox ring → `AVSampleBufferDisplayLayer`); DRM content captures black | none: the picture stays live, which is exactly why the English cannot be in the actor's slot |
| voice identity | actor database per series, learned across episodes; CAM++ likeness 0.60 own-line vs 0.18 mismatched | online CAM++ clustering: 2 speakers found out of 3 (the third had 0.8 s of speech) — the same-gender merge is still unsolved |
| measured on | 300 s of two episodes, simulated schedule over measured costs | 37 s, run in real time, `out.wav` + trace to listen to |

## Why 2–3 s cannot be a dub

Two facts, no opinion in them:

1. A line's English cannot exist before the actor has finished the line. Add the endpoint wait and
   ~1.7 s of processing and the earliest possible start is `end + ~2 s`.
2. The English is 1.5–1.9× longer than the Mandarin. Placing it at `end + 2 s` means it runs over
   whoever speaks next. In the offline dub that overrun is bought back by re-rendering each take to
   the actor's window, which needs the whole scene.

So a small D buys *following* a conversation, and a large D buys *hearing the film dubbed*.
Their D=0 result (nothing plays) and my D=2.5 result (6 of 8 lines, over ducked speech) are the
same finding read at two points on the axis.

## What B lost at D=2.5, from the trace

* one line per speaker: the clone does not exist until ~3 s of that person has been heard
  (`no-voice`);
* one `skipped:busy`: a 4.9 s merged utterance whose English ran into the next actor's turn;
* English over ducked Mandarin — the listener hears the source under the dub, which A's separated
  bed does not have;
* 2 speakers found where there are 3.

## If one pipeline is built

Take from A, in this order: the separated bed (AUSoundIsolation is off the GPU and cheap), the
actor database instead of online clustering, and `simulate.py` to choose D before another live run.
Take from B: the streaming TTS path (first audio, not total generation — this is the only thing that
makes small D possible), the collision policy in `mixer.fit` with its tests, and the memory gate.
One dial, two settings that are each honest about what they are:

* **D ≈ 2.5–4 s** — Qwen3-TTS streaming, ducked or isolated bed, expect a dropped line per
  handful and the first line of each new voice missing;
* **D ≈ 8–15 s** — OmniVoice at 8–16 steps with per-line references and `duration_s`, separated
  bed, delayed picture: what the offline dub does, at a price the viewer agrees to.

## Files

`segmenter.py` (VAD, ceiling, monotonic windows) · `mixer.py` (delay, ducking, `fit` policy) ·
`stages.py` (ASR / MT / speakers / TTS, plus dummies) · `ttsd.py` (client for A2A's TTS worker) ·
`pipeline.py` (the orchestrator) · `bench_stages.py` (the table above) · `waitgpu.py` (memory gate) ·
`test_cpu.py` (31 checks, no GPU) · `check_segmenter.py` (windows against the directed ones).
Runs: `.local/livedub/live-2.5/`, `.local/livedub/live-6.0/` (out.wav, captions.srt, trace.jsonl,
report.json), `.local/livedub/bench/*.json`.
