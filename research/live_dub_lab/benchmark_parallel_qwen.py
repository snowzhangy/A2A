"""Compare one and two resident Qwen workers on the same synthetic voice and text.

The input must be a synthetic mono PCM16 WAV at 24 kHz. Only timing and frame
counts are reported; generated speech and the reference remain in memory.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXTS = (
    "Hello everyone, today we will discuss our plans for the next quarter.",
    "Please send the report before Friday so the team can review the budget.",
)


class Worker:
    def __init__(self):
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "research/tts_worker.py"), "--model", "qwen", "--temperature", "0.6"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            event, _ = self.read()
            if event.get("ev") != "ready":
                raise RuntimeError(f"worker startup failed: {event.get('ev')}")
        except BaseException:
            self.close()
            raise

    def send(self, event, payload=b""):
        data = json.dumps(event, ensure_ascii=False).encode()
        self.process.stdin.write(struct.pack("<II", len(data), len(payload)) + data + payload)
        self.process.stdin.flush()

    def exact(self, count):
        data = bytearray()
        while len(data) < count:
            part = self.process.stdout.read(count - len(data))
            if not part:
                raise RuntimeError("worker closed its output")
            data.extend(part)
        return bytes(data)

    def read(self):
        header, payload = struct.unpack("<II", self.exact(8))
        if header > 1_048_576 or payload > 64 * 1024 * 1024:
            raise RuntimeError("invalid worker frame")
        return json.loads(self.exact(header)), self.exact(payload)

    def prepare(self, pcm, transcript):
        self.send({"op": "prepare", "voice": "S1/1", "text": transcript,
                   "mode": "icl", "rate": 24000, "language": "english"}, pcm)
        event, _ = self.read()
        if event.get("ev") != "prepared":
            raise RuntimeError(f"voice preparation failed: {event.get('ev')}")

    def synthesize(self, job, text):
        started = time.monotonic()
        first = None
        frames = 0
        self.send({"op": "synth", "id": job, "voice": "S1/1", "text": text})
        while True:
            event, payload = self.read()
            if event.get("id") != job:
                raise RuntimeError("unexpected job identifier")
            if event.get("ev") == "chunk":
                if first is None:
                    first = time.monotonic() - started
                frames += len(payload) // 4
            elif event.get("ev") == "done":
                return {"first_s": first, "wall_s": time.monotonic() - started,
                        "audio_s": frames / 24000}
            elif event.get("ev") == "error":
                raise RuntimeError("worker synthesis failed")

    def close(self):
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--transcript", required=True, help="exact synthetic reference transcript")
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args()
    if not 1 <= args.trials <= 10:
        parser.error("trials must be between 1 and 10")
    with wave.open(str(args.reference), "rb") as wav:
        if (wav.getnchannels(), wav.getframerate(), wav.getsampwidth()) != (1, 24000, 2):
            parser.error("reference must be mono PCM16 at 24 kHz")
        samples = wav.readframes(wav.getnframes())
    pcm = b"".join(struct.pack("<f", sample / 32768.0) for (sample,) in struct.iter_unpack("<h", samples))
    workers = []
    try:
        for _ in range(2):
            worker = Worker()
            workers.append(worker)
            worker.prepare(pcm, args.transcript)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            for trial in range(args.trials):
                # Both modes run the same two texts on the same warmed workers.
                order = ("serial", "parallel") if trial % 2 == 0 else ("parallel", "serial")
                for mode in order:
                    start = time.monotonic()
                    if mode == "serial":
                        results = [workers[0].synthesize(10 * trial + i, text) for i, text in enumerate(TEXTS)]
                    else:
                        futures = [pool.submit(workers[i].synthesize, 10 * trial + i, text) for i, text in enumerate(TEXTS)]
                        results = [future.result() for future in futures]
                    print(json.dumps({"trial": trial, "mode": mode, "pair_s": time.monotonic() - start,
                                      "results": results}), flush=True)
    finally:
        for worker in workers:
            worker.close()


if __name__ == "__main__":
    main()
