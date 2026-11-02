"""Repeat matched TTS lines across actor switches; local references, no live claims."""
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
from tts_worker import ModelBackend, Voice, RATE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for path in (args.plan, args.out):
        path.resolve().relative_to((ROOT / ".local").resolve())
    args.out.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(args.out, 0o700)
    import numpy as np
    backend = ModelBackend("qwen", 0.6)
    backend.load()
    backend.warmup()
    rows = json.loads(args.plan.read_text())["rows"][:2]
    if len(rows) != 2 or rows[0]["speaker"] == rows[1]["speaker"]:
        raise ValueError("need two separate actors")
    voices = []
    for row in rows:
        Path(row["reference"]).resolve().relative_to((ROOT / ".local").resolve())
        with wave.open(row["reference"], "rb") as f:
            if (f.getnchannels(), f.getframerate(), f.getsampwidth()) != (1, RATE, 2):
                raise ValueError("expected mono PCM16 at 24kHz")
            samples = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2").astype(np.float32) / 32768
        v = Voice(array.array("f", samples), row["ref_text"], "icl")
        backend.prepare(v)
        voices.append(v)
    records, originals = [], {}
    for turn, actor in enumerate((0, 1, 0, 1)):
        started = time.perf_counter()
        first = None
        pcm = array.array("f")
        for chunk in backend.generate(voices[actor], rows[actor]["text"], 0.32):
            if first is None:
                first = time.perf_counter() - started
            pcm.extend(chunk)
        samples = np.asarray(pcm, dtype=np.float32)
        record = dict(turn=turn, actor=actor, first_s=first,
                      gen_s=time.perf_counter() - started, audio_s=len(pcm) / RATE)
        if actor in originals:
            original = originals[actor]
            record["same_frames"] = len(samples) == len(original)
            if len(samples) == len(original):
                record["max_abs_difference"] = float(np.max(np.abs(samples - original)))
                record["correlation"] = float(np.corrcoef(samples, original)[0, 1])
        else:
            originals[actor] = samples
        path = args.out / f"turn-{turn:02}.wav"
        with wave.open(str(path), "wb") as f:
            f.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            f.writeframes(np.rint(np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes())
        os.chmod(path, 0o600)
        records.append(record)
        print(json.dumps(record), flush=True)
    result = dict(model=backend.name, turns=records,
                  scope="Repeated identical text and references across A/B/A/B; isolated TTS, no identity or live quality qualification")
    path = args.out / "results.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    os.chmod(path, 0o600)


if __name__ == "__main__":
    main()
