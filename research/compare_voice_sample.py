"""Matched-input local voice sample comparison, with private JSON plans in .local/.

Run with research's Python for A2A or VoiceStudio's Python for --engine dub.
The plan supplies rows with id, reference (24 kHz PCM16), ref_text, text and
source_start/source_end. No transcription or translation is timed here.
Dub uses the installed VoiceStudio OmniVoice implementation and an explicit local
checkpoint; it does not change the running backend, its profiles, or its jobs.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import os
import time
import wave
from pathlib import Path

for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_TELEMETRY"):
    os.environ[name] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--engine", choices=("dub", "qwen", "omnivoice"), required=True)
    parser.add_argument("--checkpoint", type=Path, help="dub: installed local OmniVoice snapshot")
    parser.add_argument("--gpu-lock", type=Path, help="existing dub-anything GPU lock; held read-only")
    args = parser.parse_args()
    for path in (args.plan, args.out):
        path.resolve().relative_to((ROOT / ".local").resolve())
    if args.out.exists():
        raise FileExistsError("Use a fresh output directory to preserve measurements")
    plan = json.loads(args.plan.read_text())
    if not 1 <= len(plan["rows"]) <= 32:
        raise ValueError("Use 1–32 comparison lines")
    # Respect other local model jobs without modifying dub-anything's work tree.
    lock = None
    if args.gpu_lock:
        import fcntl
        lock = args.gpu_lock.open("rb")
        print("Waiting for exclusive local GPU access", flush=True)
        fcntl.flock(lock, fcntl.LOCK_EX)
    import numpy as np
    from tts_worker import ModelBackend, Shaper, Voice

    args.out.mkdir(parents=True)
    start = time.perf_counter()
    print(f"Loading {args.engine}, offline", flush=True)
    if args.engine == "dub":
        if args.checkpoint is None or not args.checkpoint.is_dir():
            raise ValueError("dub requires an installed local --checkpoint")
        import torch
        from omnivoice import OmniVoice
        model = OmniVoice.from_pretrained(str(args.checkpoint), device_map="mps",
                                          dtype=torch.float16, load_asr=False,
                                          local_files_only=True)
        load_s = time.perf_counter() - start
        model_name = f"k2-fsa/OmniVoice@{args.checkpoint.name}"
        warm_s = None
    else:
        backend = ModelBackend(args.engine, None)
        backend.load()
        load_s = time.perf_counter() - start
        start = time.perf_counter()
        backend.warmup()
        warm_s = time.perf_counter() - start
        model_name = backend.name
    records = []
    for row in plan["rows"]:
        with wave.open(row["reference"], "rb") as f:
            if (f.getframerate(), f.getnchannels(), f.getsampwidth()) != (24_000, 1, 2):
                raise ValueError("References must be 24 kHz mono PCM16")
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2").astype(np.float32) / 32768
        voice = Voice(array.array("f", pcm), row["ref_text"], "icl")
        start = time.perf_counter()
        if args.engine == "dub":
            torch.manual_seed(42)
            with torch.inference_mode():
                prompt = model.create_voice_clone_prompt(row["reference"], row["ref_text"])
        else:
            backend.mx.random.seed(42)
            backend.prepare(voice)
        prepare_s = time.perf_counter() - start
        shaper = Shaper(voice.target_rms, voice.gain, sentence_end=True)
        chunks, events = [], []
        start = time.perf_counter()
        if args.engine == "dub":
            with torch.inference_mode():
                output = model.generate(text=row["text"], language="English",
                                        voice_clone_prompt=prompt, num_step=32)[0]
            torch.mps.synchronize()
            output = output.detach().float().cpu().numpy().reshape(-1)
            generator = iter([array.array("f", output)])
        else:
            generator = backend.generate(voice, row["text"], 0.32)
        for chunk in generator:
            if shaper.gain is None and voice.gain is not None:
                shaper.gain = voice.gain
            shaped = shaper.process(chunk)
            if shaped:
                events.append(dict(arrival_s=time.perf_counter() - start, frames=len(shaped)))
                chunks.append(np.asarray(shaped, dtype=np.float32))
        generation_s = time.perf_counter() - start
        if not chunks:
            raise RuntimeError("No voiced audio generated")
        output = np.concatenate(chunks)
        if not np.isfinite(output).all():
            raise ValueError("Non-finite output")
        if not row["id"].replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid line id")
        path = args.out / f"{row['id']}.wav"
        with wave.open(str(path), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(24_000)
            f.writeframes(np.rint(np.clip(output, -1, 1) * 32767).astype("<i2").tobytes())
        seconds = len(output) / 24_000
        record = dict(id=row["id"], model=model_name, prepare_s=prepare_s,
                      generation_s=generation_s, first_audio_s=events[0]["arrival_s"],
                      audio_s=seconds, real_time_factor=generation_s / seconds,
                      source_s=row["source_end"] - row["source_start"],
                      reference_sha256=hashlib.sha256(Path(row["reference"]).read_bytes()).hexdigest(),
                      wav_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), chunks=events)
        records.append(record)
        (args.out / "result.json").write_text(json.dumps(dict(engine=args.engine, model=model_name,
            identity_qualified=False, offline=True, scope="isolated TTS; matched directed text and references",
            plan_sha256=hashlib.sha256(args.plan.read_bytes()).hexdigest(), load_s=load_s,
            warm_s=warm_s, rows=records), indent=2) + "\n")
        print(json.dumps({k: record[k] for k in ("id", "prepare_s", "generation_s", "first_audio_s", "audio_s")}), flush=True)
    if lock:
        lock.close()


if __name__ == "__main__":
    main()
