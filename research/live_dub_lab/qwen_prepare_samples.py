"""Compare repeated per-actor warmup with conditioning-only preparation on matched input."""
import argparse
import array
import json
import os
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "research"))
import voice_clone
from tts_worker import ModelBackend, Shaper, Voice, voiced_rms, RATE
from qwen_worker import ConditioningBackend


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    a = p.parse_args()
    for path in (a.plan, a.out):
        path.resolve().relative_to((ROOT / ".local").resolve())
    if a.out.exists():
        raise FileExistsError("choose a fresh output directory")
    a.out.mkdir(mode=0o700, parents=True)
    os.chmod(a.out, 0o700)
    import numpy as np
    backend = ModelBackend("qwen", 0.6)
    backend.load()
    backend.warmup()
    records = []
    for index, row in enumerate(json.loads(a.plan.read_text())["rows"]):
        Path(row["reference"]).resolve().relative_to((ROOT / ".local").resolve())
        if not row["id"].replace("-", "").isalnum():
            raise ValueError("invalid line ID")
        with wave.open(row["reference"], "rb") as f:
            if (f.getnchannels(), f.getframerate(), f.getsampwidth()) != (1, 24000, 2):
                raise ValueError("expected mono PCM16 24kHz")
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2").astype(np.float32) / 32768
        # Alternate order to reduce the bias from in-process caches and changing load.
        modes = ["warmup", "conditioning"] if index % 2 else ["conditioning", "warmup"]
        for mode in modes:
            v = Voice(array.array("f", pcm), row["ref_text"], "icl")
            start = time.perf_counter()
            if mode == "conditioning":
                ConditioningBackend.prepare(backend, v)
            else:
                # Reproduce the former per-speaker warmup after the production
                # backend adopts direct conditioning, keeping this comparison useful.
                ConditioningBackend.prepare(backend, v)
                warm = array.array("f")
                for chunk in backend.generate(v, "Okay, let's begin.", 2.0):
                    warm.extend(chunk)
                level = voiced_rms(warm)
                if level and v.target_rms:
                    v.gain = v.target_rms / level
                v.seconds_per_character = (len(Shaper(None, 1.0).process(warm)) or len(warm)) / RATE / len("Okay, let's begin.")
            prepare = time.perf_counter() - start
            backend.mx.random.seed(42)
            shaper = Shaper(v.target_rms, v.gain, sentence_end=True)
            start = time.perf_counter()
            chunks = []
            first = None
            for chunk in backend.generate(v, row["text"], 0.32):
                if first is None:
                    first = time.perf_counter() - start
                chunks.extend(shaper.process(chunk))
            file = a.out / f"{row['id']}-{mode}.wav"
            with wave.open(str(file), "wb") as f:
                f.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                f.writeframes(np.rint(np.clip(chunks, -1, 1) * 32767).astype("<i2").tobytes())
            records.append(dict(id=row["id"], steps=mode, prepare_s=prepare, first_s=first,
                generation_s=time.perf_counter() - start, audio_s=len(chunks) / 24000, file=file.name))
            (a.out / "results.json").write_text(json.dumps(dict(model=backend.name, rows=records,
                scope="matched isolated TTS, alternating prepare order, shared process caches"), indent=2) + "\n")
            print(json.dumps(records[-1]), flush=True)


if __name__ == "__main__":
    main()
