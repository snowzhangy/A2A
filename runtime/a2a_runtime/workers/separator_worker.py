"""Resident causal RNNoise separator. Input/output: 48 kHz mono f32le, 10 ms frames."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from a2a_runtime.workers.protocol import WorkerChannel  # noqa: E402


def main() -> None:
    from pyrnnoise import RNNoise

    channel = WorkerChannel()
    separator = RNNoise(48_000)
    original = np.zeros(0, dtype=np.float32)
    latency = 480
    channel.send({"ev": "ready", "model": "rnnoise-xiph", "latency_samples": latency, "frame_samples": 480})
    while True:
        frame = channel.receive()
        if frame is None:
            return
        request, payload = frame
        try:
            op = request.get("op")
            if op == "reset":
                separator.reset()
                original = np.zeros(0, dtype=np.float32)
                continue
            if op != "separate":
                raise ValueError("unknown separator request")
            mix = np.frombuffer(payload, dtype="<f4").astype(np.float32)
            if len(mix) % 480:
                raise ValueError("separator input must contain whole 10 ms frames")
            original = np.concatenate([original, mix])
            for _probability, enhanced in separator.denoise_chunk(mix):
                voice = enhanced.reshape(-1).astype(np.float32) / 32_768.0
                source, original = original[:480], original[480:]
                bed = np.clip(source - voice, -1, 1)
                voice = np.clip(voice, -1, 1)
                interleaved = np.empty(960, dtype=np.float32)
                interleaved[0::2], interleaved[1::2] = voice, bed
                channel.send({"ev": "separated", "id": request.get("id"), "frames": 480}, interleaved.astype("<f4").tobytes())
        except Exception as error:
            channel.send({"ev": "error", "message": type(error).__name__})


if __name__ == "__main__":
    main()
