"""Streaming windowed-sinc resampler (Kaiser window, tabulated kernel). Port of
apple/Sources/A2AMedia/Resampler.swift, vectorized per block.

Output sample j is centered on input time j * inRate/outRate; the processing latency is
`latency_input_frames` because the kernel needs that much lookahead. Positions advance by
repeated addition exactly as in the Swift source, so both produce the same sample count.
"""

from __future__ import annotations

import math

import numpy as np


def _bessel_i0(x: float) -> float:
    total, term, k = 1.0, 1.0, 1.0
    while term > 1e-12 * total:
        term *= (x / (2 * k)) * (x / (2 * k))
        total += term
        k += 1
    return total


def _round_half_away(x: float) -> float:
    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)


class StreamingResampler:
    TABLE_RESOLUTION = 512

    def __init__(self, input_rate: int, output_rate: int, half_width: int = 16, beta: float = 8.0) -> None:
        if input_rate <= 0 or output_rate <= 0 or half_width <= 0:
            raise ValueError("rates and half_width must be positive")
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.half_width = half_width
        self._step = input_rate / output_rate
        self._cutoff = min(1.0, output_rate / input_rate) * 0.97
        self._history = np.zeros(0, dtype=np.float32)
        self._history_start = 0
        self._position = 0.0
        self._input_count = 0
        if input_rate == output_rate:
            self._radius = 0
            self._table = np.zeros(0, dtype=np.float32)
            return
        self._radius = int(math.ceil(half_width / self._cutoff))
        n = self._radius * self.TABLE_RESOLUTION
        x = np.arange(n + 1, dtype=np.float64) / self.TABLE_RESOLUTION
        arg = x * self._cutoff
        sinc = np.where(arg == 0, 1.0, np.sin(np.pi * arg) / np.where(arg == 0, 1.0, np.pi * arg))
        r = x / self._radius
        i0b = _bessel_i0(beta)
        window = np.array([_bessel_i0(beta * math.sqrt(1 - v * v)) / i0b if v < 1 else 0.0 for v in r])
        table = np.zeros(n + 2, dtype=np.float32)
        table[: n + 1] = (self._cutoff * sinc * window).astype(np.float32)
        self._table = table

    @property
    def latency_input_frames(self) -> int:
        return self._radius

    @property
    def latency_seconds(self) -> float:
        return self._radius / self.input_rate

    def process(self, samples) -> np.ndarray:
        x = np.asarray(samples, dtype=np.float32)
        self._input_count += len(x)
        if self.input_rate == self.output_rate:
            return x.copy()
        self._history = np.concatenate([self._history, x]) if len(self._history) else x.copy()
        return self._drain(final=False)

    def flush(self) -> np.ndarray:
        """Emits the remaining output, treating samples beyond the end as silence."""
        if self.input_rate == self.output_rate:
            return np.zeros(0, dtype=np.float32)
        return self._drain(final=True)

    def _positions(self, final: bool) -> np.ndarray:
        """Output positions for this drain. cumsum over [p, step, step, ...] performs the same
        sequential float64 additions as the Swift loop's `position += step`."""
        step, radius = self._step, self._radius
        end = float(self._input_count)
        expected_total = math.floor(self._input_count / step)
        p = self._position
        if final:
            bound = expected_total - _round_half_away(p / step) + 2
        else:
            bound = (end - radius - p) / step + 2
        n = max(2, int(math.ceil(bound)) + 1)
        while True:
            seq = np.cumsum(np.concatenate([[p], np.full(n, step)]))
            if final:
                rounded = np.where(seq / step >= 0, np.floor(seq / step + 0.5), -np.floor(-seq / step + 0.5))
                stop = rounded >= expected_total
            else:
                stop = seq + radius >= end
            if stop.any():
                count = int(np.argmax(stop))
                self._position = float(seq[count])
                return seq[:count]
            n *= 2

    def _drain(self, final: bool) -> np.ndarray:
        positions = self._positions(final)
        out = self._samples(positions) if len(positions) else np.zeros(0, dtype=np.float32)
        keep_from = int(math.floor(self._position)) - self._radius - 1
        if keep_from > self._history_start:
            drop = min(len(self._history), keep_from - self._history_start)
            self._history = self._history[drop:]
            self._history_start += drop
        return out

    def _samples(self, positions: np.ndarray) -> np.ndarray:
        radius, scale = self._radius, float(self.TABLE_RESOLUTION)
        hist, start = self._history, self._history_start
        centers = np.floor(positions).astype(np.int64)
        frac = positions - centers
        ks = np.arange(-radius, radius + 2, dtype=np.int64)
        idx = centers[:, None] - start + ks[None, :]
        dist = np.abs(ks[None, :].astype(np.float64) - frac[:, None])
        valid = (idx >= 0) & (idx < len(hist)) & (dist < radius)
        f = np.where(valid, dist * scale, 0.0)
        i = f.astype(np.int64)
        w = (f - i).astype(np.float32)
        t0 = self._table[i]
        taps = t0 + (self._table[i + 1] - t0) * w
        values = hist[np.clip(idx, 0, max(0, len(hist) - 1))] if len(hist) else np.zeros_like(taps)
        return np.sum(np.where(valid, values * taps, np.float32(0)), axis=1, dtype=np.float32)

    @staticmethod
    def resample(samples, input_rate: int, output_rate: int) -> np.ndarray:
        r = StreamingResampler(input_rate, output_rate)
        return np.concatenate([r.process(samples), r.flush()])
