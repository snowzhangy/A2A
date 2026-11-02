"""Signal measurements for capture fidelity, crossfade checks, and fixtures.
Port of apple/Sources/A2AMedia/Signal.swift (NumPy instead of vDSP)."""

from __future__ import annotations

import math

import numpy as np


def rms(x) -> float:
    a = np.asarray(x, dtype=np.float64)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def dbfs(level: float) -> float:
    return 20 * math.log10(level) if level > 0 else -math.inf


def peak(x) -> float:
    a = np.asarray(x)
    return float(np.max(np.abs(a))) if a.size else 0.0


def correlation(a, b) -> float:
    n = min(len(a), len(b))
    if n <= 1:
        return 0.0
    x = np.asarray(a[:n], dtype=np.float64)
    y = np.asarray(b[:n], dtype=np.float64)
    x = x - x.mean()
    y = y - y.mean()
    da, db = float(np.dot(x, x)), float(np.dot(y, y))
    return float(np.dot(x, y) / math.sqrt(da * db)) if da > 0 and db > 0 else 0.0


def best_lag(reference, signal, probe: range, max_lag: int) -> tuple[int, float]:
    """Lag (samples) of `signal` relative to `reference` maximizing correlation over a probe
    window from the reference, searched over 0...max_lag."""
    ref = np.asarray(reference, dtype=np.float64)
    sig = np.asarray(signal, dtype=np.float64)
    m = len(probe)
    lags = min(max_lag + 1, len(sig) - probe.stop + 1)
    if m <= 1 or lags <= 0:
        return 0, -2.0
    window = ref[probe.start:probe.stop]
    centered = window - window.mean()
    energy = float(np.dot(centered, centered))
    if energy <= 0:
        return 0, 0.0
    span = sig[probe.start:probe.start + lags + m - 1]
    # FFT cross-correlation: one pass over every candidate lag (the Swift side uses vDSP_conv).
    size = 1 << int(np.ceil(np.log2(len(span) + m)))
    dots = np.fft.irfft(np.fft.rfft(span, size) * np.conj(np.fft.rfft(centered, size)), size)[:lags]
    c1 = np.concatenate([[0.0], np.cumsum(span)])
    c2 = np.concatenate([[0.0], np.cumsum(span * span)])
    s = c1[m:m + lags] - c1[:lags]
    variance = c2[m:m + lags] - c2[:lags] - s * s / m
    score = np.where(variance > 0, dots / np.sqrt(energy * np.maximum(variance, 1e-30)), -np.inf)
    lag = int(np.argmax(score))
    start = probe.start + lag
    return lag, correlation(window, sig[start:start + m])


def zero_runs(x, min_run: int) -> list[range]:
    a = np.asarray(x)
    runs, start = [], None
    for i, v in enumerate(a == 0):
        if v and start is None:
            start = i
        elif not v and start is not None:
            if i - start >= min_run:
                runs.append(range(start, i))
            start = None
    if start is not None and len(a) - start >= min_run:
        runs.append(range(start, len(a)))
    return runs


def max_step(x) -> float:
    a = np.asarray(x, dtype=np.float64)
    return float(np.max(np.abs(np.diff(a)))) if a.size > 1 else 0.0


def fidelity_probe(sample_rate: int, seconds: float, amplitude: float = 0.25) -> np.ndarray:
    """Sum of sines plus a slow chirp: no digital silence, distinctive for alignment."""
    t = np.arange(int(sample_rate * seconds)) / sample_rate
    chirp = np.sin(2 * np.pi * (220 * t + 40 * t * t / max(seconds, 1)))
    v = 0.5 * np.sin(2 * np.pi * 440 * t) + 0.3 * np.sin(2 * np.pi * 1_234.5 * t) + 0.2 * chirp
    return (v * amplitude).astype(np.float32)
