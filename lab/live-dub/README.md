# Live-dub lab

Experiments toward dub-anything quality (`condor (dubbed).mp4`) in a live session, plus a per-series
actor database that recognizes actors on a new episode. Research only: nothing here is product code,
gate evidence, or a qualified model. Findings are in [RESEARCH.md](RESEARCH.md).

Code lives here; everything derived from episodes (audio, transcripts, voice references, voiceprints,
takes, renders) stays in `.local/live-dub/` (gitignored). The actor database is biometric data:
`.local/live-dub/actors/<series>/` is 0700 and its files 0600.

## Setup

```sh
cd lab/live-dub
UV_PROJECT_ENVIRONMENT=../../.local/live-dub/venv uv sync      # mlx-audio 0.6.1, mlx-lm, sherpa-onnx, ...
VIRTUAL_ENV=../../.local/live-dub/venv uv pip install torch    # only to convert the RoFormer checkpoint
./fetch_models.sh <hf-repo|url> ...                            # research-only; writes models/lock.json
PY=../../.local/live-dub/venv/bin/python
```

Models (all research-only, licenses unverified unless noted; observed revisions and SHA-256 in
`.local/live-dub/models/lock.json`): Qwen3-ASR 1.7B/0.6B 8-bit, SenseVoiceSmall, Qwen3-ForcedAligner
0.6B, Qwen3-30B-A3B-Instruct-2507 4-bit, Qwen3.5-9B 4-bit, Qwen3-4B-Instruct-2507 4-bit, 3D-Speaker
CAM++ and ERes2NetV2 zh-cn (sherpa-onnx release assets), Silero VAD, Kim's Mel-Band RoFormer
(converted locally from VoiceStudio's checkpoint; author-tagged GPL-3.0: local inference only).
TTS reuses the repository's pinned OmniVoice and Qwen3-TTS weights in `.local/models/` (read-only).

## Pieces

| Script | What it does |
| --- | --- |
| `separate.py` | Streaming RoFormer vocals/background, fixed latency of one window |
| `tools/isolate.swift` | Offline AUSoundIsolation (macOS) voice/background, for comparison |
| `asr.py` | Silero VAD segments + Qwen3-ASR / SenseVoice; CER against published subtitles |
| `translate.py` | Local-LLM dubbing translation: context, series glossary, speaker, syllable budget |
| `tts.py`, `bench_tts.py` | OmniVoice (exact slot duration) and Qwen3-TTS takes; per-line references; WSOLA fit |
| `speakers.py`, `actordb.py` | CAM++ embeddings, clustering, open-set actor database with stored references |
| `pipeline.py` | Causal pass over an episode: what a live engine decides, with per-stage compute timed |
| `simulate.py` | Replays a pass on one GPU queue for presentation delay D; late lines are misses |
| `render.py` | Background bed + scheduled takes, level-matched, muxed under the episode's picture |
| `enroll.py` | Offline enrollment: global clustering of watched episodes into the actor database |
| `replay_db.py` | Re-run recognition policies over recognized episodes without ASR |
| `eval_takes.py` | Back-transcription WER and CAM++ likeness of a pass's takes |
| `frames.py`, `sheets_enroll.py` | Contact sheets (frame per line) to check who speaks |

## Typical run

```sh
$PY separate.py --episode ep08 --start 0 --end 2735.14 --out ../../.local/live-dub/runs/sep-ep08-full
$PY pipeline.py --episode ep08 --no-tts --readonly-db         # recognize every line (ASR, splits)
$PY enroll.py --series condor-ep08-offline --names "A01=Yang Guo" ../../.local/live-dub/runs/live-ep08-0-0-enroll
$PY pipeline.py --episode ep09 --series condor-ep08-offline --start 580 --end 880 --readonly-db --steps 8
$PY simulate.py ../../.local/live-dub/runs/live-ep09-580-880 --delays 0,3,6,10,15,20 --separation isolation
$PY render.py ../../.local/live-dub/runs/live-ep09-580-880 --delay 10 --separation isolation --start 580 --end 880
# No picture delay (catch-up): clause units, concise English, natural-length takes
$PY pipeline.py --episode ep09 --series condor-ep08-offline --start 580 --end 880 --readonly-db --steps 8 \
    --min-silence 0.15 --max-speech 5 --syl-rate 3.6 --strict --duration natural --out ../../.local/live-dub/runs/catchup-ep09
$PY simulate.py ../../.local/live-dub/runs/catchup-ep09 --mode catchup --separation isolation --max-lag 7
$PY render.py ../../.local/live-dub/runs/catchup-ep09 --schedule schedule-catchup-isolation.json --start 580 --end 880
```
