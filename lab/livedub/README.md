# livedub — dubbing at a chosen delay, on one M3 Pro

Two questions, kept separate because they have different answers:

1. **How fast can the machine go?** Recognition, translation and the first sample of a cloned
   voice, measured per stage: `bench_stages.py`, table in
   [APPROACHES.md](APPROACHES.md). Answer so far: the actor's last sample to the first English
   sample is **1.67 s**.
2. **What does that delay buy?** Nothing that sounds like the offline dub, and that is arithmetic
   rather than engineering: see APPROACHES.md. A line's English cannot start before the line
   ends, and it is 1.5–1.9× longer than the Mandarin, so a small delay means speaking over the next
   actor, and an in-slot dub means delaying the programme by 10–15 s.

## Run it

```sh
V=../../.local/live-dub/venv/bin/python     # has mlx_audio, mlx_lm, sherpa-onnx, opencc

$V test_cpu.py                              # 31 checks, no models, no GPU
$V check_segmenter.py --sweep               # endpoints against measured actor windows
$V waitgpu.py --once                        # is there room for another Metal model?

# the whole thing, live-paced, on the 37 s scene the offline dub was directed on
$V pipeline.py --file ../../.local/live-dub/input/ep08-16k.wav --start 583 --end 620 \
     --delay 2.5 --glossary ../../.local/livedub/glossary-condor.json --out live-2p5

# the schedule alone, no models: scripted words, a tone where the clone would be
$V pipeline.py --file ... --stages dummy --tts-rtf 0.8 --out dummy
```

Output per run, in `.local/livedub/<out>/`: `out.wav` (the delayed bed ducked under the English),
`captions.srt`, `trace.jsonl` (every stage's time for every line), `report.json`.

## Measure one stage

```sh
$V bench_stages.py asr --model sensevoice
$V bench_stages.py mt  --model a3b
$V bench_stages.py tts --engine qwen --ref <wav> --ref-text "…中文…" --text "English line"
```

## Ground rules here

* Everything is `.local/`: research audio and transcripts are never committed.
* Nothing was listened to. Intelligibility checks are recogniser-based, likeness is unmeasured, and
  the mixed bed still carries the Mandarin under the English — that is a listening decision.
* The GPU belongs to whoever got there first; `waitgpu.py` exists because MLX fails with
  `Insufficient Memory` instead of queueing.
