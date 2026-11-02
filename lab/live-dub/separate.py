"""Vocal / background separation with Kim's Mel-Band RoFormer in MLX (the separator dub-anything uses).

Weights: converted locally from VoiceStudio's `vocals_mel_band_roformer.ckpt` (sha256 87201f4d…7559e).
mlx-audio's preset notes say the author tagged them GPL-3.0: fine for local research inference,
never shippable in the product without a license decision.

Streaming use: `StreamSeparator` takes stereo 44.1 kHz blocks and emits vocals with a fixed latency
of one window (overlap-add with cosine-tapered windows; `overlap` is the shared fraction). The background is the input minus
the vocals, delayed by the same latency, so the two stay sample-aligned.

usage: python separate.py --episode ep08 --start 583 --end 703 --window 8 --overlap 0.125 [--out DIR]
"""
from __future__ import annotations

import argparse
import time

import mlx.core as mx
import numpy as np

from common import MODELS, RUNS, episode_audio, save, write_json

RATE = 44_100


def load_model():
    from mlx_audio.sts.models.mel_roformer.model import MelRoFormer
    model = MelRoFormer.from_pretrained(MODELS / "roformer")
    model.eval()
    return model


class StreamSeparator:
    """Overlap-add separation over fixed windows. Latency = `window` seconds after a sample arrives."""

    def __init__(self, model, window: float = 8.0, overlap: float = 0.125):
        from scipy.signal.windows import tukey
        self.model = model
        self.n = int(window * RATE)
        self.hop = self.n - int(overlap * self.n)
        self.win = tukey(self.n, 2 * overlap).astype(np.float32)[:, None] + 1e-4
        self.buf = np.zeros((self.n, 2), np.float32)    # last full window of input
        self.acc = np.zeros((self.n, 2), np.float32)    # overlap-add accumulator for vocals
        self.wsum = np.zeros((self.n, 1), np.float32)
        self.pending = np.zeros((0, 2), np.float32)
        # Output index j holds input index j - (n - hop): the first hop covers start-up zeros.
        self.dry = np.zeros((self.n - self.hop, 2), np.float32)
        self.compute_s = 0.0

    def _run(self, window: np.ndarray) -> np.ndarray:
        t = time.perf_counter()
        out = self.model(mx.array(window.T[None]))
        mx.eval(out)
        self.compute_s += time.perf_counter() - t
        return np.array(out[0]).T

    def push(self, block: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Feed stereo samples; returns (vocals, background) for samples that are now final."""
        self.pending = np.concatenate([self.pending, block])
        self.dry = np.concatenate([self.dry, block])
        voc_out = []
        while len(self.pending) >= self.hop:
            self.buf = np.concatenate([self.buf[self.hop:], self.pending[:self.hop]])
            self.pending = self.pending[self.hop:]
            v = self._run(self.buf)
            self.acc += v * self.win
            self.wsum += self.win
            done = self.acc[:self.hop] / np.maximum(self.wsum[:self.hop], 1e-3)
            voc_out.append(done)
            self.acc = np.concatenate([self.acc[self.hop:], np.zeros((self.hop, 2), np.float32)])
            self.wsum = np.concatenate([self.wsum[self.hop:], np.zeros((self.hop, 1), np.float32)])
        if not voc_out:
            return np.zeros((0, 2), np.float32), np.zeros((0, 2), np.float32)
        voc = np.concatenate(voc_out)
        dry, self.dry = self.dry[:len(voc)], self.dry[len(voc):]
        return voc, dry - voc


def separate_offline(model, audio: np.ndarray, window: float = 8.0, overlap: float = 0.125,
                     block: float = 0.5) -> tuple[np.ndarray, np.ndarray, float]:
    """Run the streaming separator over a whole array; returns aligned (vocals, background, compute_s)."""
    sep = StreamSeparator(model, window, overlap)
    step = int(block * RATE)
    tail = np.zeros((sep.n, 2), np.float32)
    vocs, beds = [], []
    for i in range(0, len(audio) + sep.n, step):
        chunk = audio[i:i + step] if i < len(audio) else tail[:step]
        v, b = sep.push(chunk)
        vocs.append(v)
        beds.append(b)
    lead = sep.n - sep.hop
    voc = np.concatenate(vocs)[lead:lead + len(audio)]
    bed = np.concatenate(beds)[lead:lead + len(audio)]
    return voc, bed, sep.compute_s


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", default="ep08")
    ap.add_argument("--start", type=float, default=583)
    ap.add_argument("--end", type=float, default=703)
    ap.add_argument("--window", type=float, default=8.0)
    ap.add_argument("--overlap", type=float, default=0.125)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    audio = episode_audio(a.episode, RATE, a.start, a.end, stereo=True)
    model = load_model()
    voc, bed, compute = separate_offline(model, audio, a.window, a.overlap)
    dur = len(audio) / RATE
    out = a.out or RUNS / f"separate-{a.episode}-{int(a.start)}-{int(a.end)}-w{a.window:g}-o{a.overlap:g}"
    save(f"{out}/vocals.wav", voc, RATE)
    save(f"{out}/background.wav", bed, RATE)
    stats = {"seconds": dur, "window_s": a.window, "overlap": a.overlap, "compute_s": round(compute, 2), "rtf": round(compute / dur, 3),
             "latency_s": a.window}
    write_json(f"{out}/stats.json", stats)
    print(stats)


if __name__ == "__main__":
    main()
