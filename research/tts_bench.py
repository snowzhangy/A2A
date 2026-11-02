"""Measure the resident Qwen3-TTS worker: prepare time, first-chunk time, real-time factor.

Dev harness (not product code, and not a gate): it speaks the same worker protocol as
A2AModels.CloneVoiceSynthesizer and prints per-unit timings, so a change in research/tts_worker.py can be
read as milliseconds instead of listened to. The reference is float32 mono PCM at 24 kHz.

  uv run --project research python research/tts_bench.py --reference ref.wav [--repeat N] [--text "..."]

Run it beside scripts/bench_hy_local.sh --load to measure the two model processes sharing one GPU.
"""
from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKER = os.path.join(ROOT, "research", "tts_worker.py")
PYTHON = os.path.join(ROOT, "research", ".venv", "bin", "python")
RATE = 24_000

DEFAULT_TEXTS = [
    "Today we will discuss the plan for the next quarter.",
    "We disagree with this plan, and the budget needs to grow by twenty percent.",
    "Please send me the report before Friday.",
]


def read_wav_f32(path: str) -> bytes:
    """float32 mono PCM from a WAV file (the stdlib `wave` reader rejects IEEE float)."""
    blob = open(path, "rb").read()
    assert blob[:4] == b"RIFF" and blob[8:12] == b"WAVE", f"{path} is not a WAV file"
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(blob):
        size, = struct.unpack("<I", blob[pos + 4:pos + 8])
        tag = blob[pos:pos + 4]
        if tag == b"fmt ":
            code, channels, rate, _, _, bits = struct.unpack("<HHIIHH", blob[pos + 8:pos + 24])
            if code == 0xFFFE:  # WAVE_FORMAT_EXTENSIBLE carries the real code in the sub-format
                code, = struct.unpack("<H", blob[pos + 32:pos + 34])
            fmt = (code, channels, rate, bits)
        elif tag == b"data":
            data = blob[pos + 8:pos + 8 + size]
        pos += 8 + size + (size & 1)
    code, channels, rate, bits = fmt
    assert rate == RATE and channels == 1, f"reference must be {RATE} Hz mono, got {rate} Hz / {channels} ch"
    assert code == 3 and bits == 32, "reference must be float32"
    return data


def frame(header: dict, payload: bytes = b"") -> bytes:
    data = json.dumps(header).encode()
    return struct.pack("<II", len(data), len(payload)) + data + payload


def read_exact(f, n: int) -> bytes | None:
    out = bytearray()
    while len(out) < n:
        block = f.read(n - len(out))
        if not block:
            return None
        out += block
    return bytes(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reference", required=True)
    ap.add_argument("--model", default="qwen")
    ap.add_argument("--mode", default="xvector")
    ap.add_argument("--interval", type=float, default=0.32)
    ap.add_argument("--temperature", type=float, default=0.6)
    ap.add_argument("--repeat", type=int, default=2)
    ap.add_argument("--text", action="append", default=None)
    args = ap.parse_args()

    pcm = read_wav_f32(args.reference)
    proc = subprocess.Popen(
        [PYTHON, WORKER, "--model", args.model, "--interval", str(args.interval),
         "--temperature", str(args.temperature)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr)
    stdin, stdout = proc.stdin, proc.stdout

    def events():
        while True:
            prefix = read_exact(stdout, 8)
            if not prefix:
                return
            hl, pl = struct.unpack("<II", prefix)
            header = json.loads(read_exact(stdout, hl) or b"{}")
            payload = read_exact(stdout, pl) or b""
            yield header, payload

    stream = events()
    ready = next(stream)[0]
    print(f"ready: model={ready.get('model')} load={ready.get('load_s'):.1f}s "
          f"warmup={ready.get('warmup_s'):.1f}s streaming={ready.get('streaming')}")

    t0 = time.perf_counter()
    stdin.write(frame({"op": "prepare", "voice": "s1", "mode": args.mode, "rate": RATE,
                       "text": "", "language": "chinese"}, pcm))
    stdin.flush()
    header, _ = next(stream)
    print(f"prepare ({args.mode}, {len(pcm)/4/RATE:.1f} s reference): {header.get('ev')} "
          f"{time.perf_counter() - t0:.3f}s prepare_s={header.get('prepare_s')}")

    texts = args.text or DEFAULT_TEXTS
    for i in range(args.repeat):
        for n, text in enumerate(texts):
            job = i * 100 + n
            t0 = time.perf_counter()
            first = None
            samples = 0
            stdin.write(frame({"op": "synth", "id": job, "voice": "s1", "text": text}))
            stdin.flush()
            for header, payload in stream:
                if header.get("id") != job and header.get("ev") != "error":
                    continue
                if header["ev"] == "chunk":
                    if first is None:
                        first = time.perf_counter() - t0
                    samples += len(payload) // 4
                elif header["ev"] == "done":
                    audio_s = header.get("audio_s", 0)
                    gen_s = header.get("gen_s", 0)
                    print(f"unit {job}: first={first if first is None else round(first, 3)}s "
                          f"gen={gen_s:.3f}s audio={audio_s:.2f}s rt={gen_s / max(audio_s, 1e-6):.2f} "
                          f"pace={header.get('pace')} truncated={header.get('truncated')}")
                    break
                elif header["ev"] == "error":
                    print(f"unit {job}: error {header.get('message')}")
                    break
    stdin.close()
    proc.wait(timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
