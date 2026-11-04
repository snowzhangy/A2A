"""Resident spoken-language identifier for same-language passthrough (ADR-0018).

Loads the ECAPA-TDNN VoxLingua107 classifier (manifest id ecapa-voxlingua107-lid-mlx) once and
answers one closed unit at a time, so the host can tell a line already spoken in the target
language from one to translate. Audio and verdicts never leave the process except as the
language code and its probability.

Protocol (stdin -> stdout):
    request:  u32le header length, JSON header {"id": N, "samples": K}, then K float32le samples
              of mono 16 kHz audio.
    replies:  one JSON line each: {"ev": "ready", "load_s": ...} once, then
              {"id": N, "lang": "en", "p": 0.99, "ms": 7} per request, or {"id": N, "error": "..."}.
    stdin EOF ends the worker.

    python research/lid_worker.py --model DIR [--self-test WAV]
"""

import argparse
import json
import struct
import sys
import time
from pathlib import Path

RATE = 16000


def load(model_dir: Path):
    import mlx.core as mx
    from mlx_audio.lid.models.ecapa_tdnn.config import ModelConfig
    from mlx_audio.lid.models.ecapa_tdnn.ecapa_tdnn import ECAPA_TDNN

    config = json.loads((model_dir / "config.json").read_text())
    model = ECAPA_TDNN(ModelConfig(**config))
    weights = mx.load(str(next(model_dir.glob("*.safetensors"))))
    # The module keeps its residual blocks under two names (block1.. and blocks.N); they are the
    # same arrays, so only the first set is in the checkpoint.
    model.load_weights(list(model.sanitize(weights).items()), strict=False)
    model.eval()
    return model


def identify(model, samples) -> tuple[str, float]:
    import mlx.core as mx

    top = model.predict(mx.array(samples), top_k=1)
    return top[0][0], float(top[0][1])


def read_exact(stream, n: int) -> bytes | None:
    data = b""
    while len(data) < n:
        chunk = stream.read(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def say(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--self-test", type=Path, help="identify one 16 kHz mono float or PCM16 WAV and exit")
    args = parser.parse_args()

    import numpy as np

    started = time.time()
    model = load(args.model)
    identify(model, np.zeros(RATE, dtype=np.float32))  # compile and warm up
    if args.self_test:
        import wave

        with wave.open(str(args.self_test)) as w:
            x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        print(identify(model, x))
        return 0
    say({"ev": "ready", "load_s": round(time.time() - started, 2)})

    stdin = sys.stdin.buffer
    while True:
        head = read_exact(stdin, 4)
        if head is None:
            return 0
        header = json.loads(read_exact(stdin, struct.unpack("<I", head)[0]) or b"{}")
        count = int(header.get("samples", 0))
        payload = read_exact(stdin, 4 * count) if count else b""
        if payload is None:
            return 0
        rid = header.get("id")
        try:
            samples = np.frombuffer(payload, dtype="<f4")
            if count < RATE // 4 or not np.isfinite(samples).all():
                raise ValueError("needs at least 0.25 s of finite audio")
            t0 = time.time()
            lang, p = identify(model, samples)
            say({"id": rid, "lang": lang, "p": round(p, 4), "ms": round((time.time() - t0) * 1000)})
        except Exception as error:  # one bad request must not end the session's worker
            say({"id": rid, "error": str(error)[:200]})


if __name__ == "__main__":
    sys.exit(main())
