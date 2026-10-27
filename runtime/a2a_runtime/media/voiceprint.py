"""Speaker voiceprints for the DSP speaker tracker: mel cepstrum and pitch of voiced frames
in 16 kHz mono speech. Port of apple/Sources/A2AMedia/Voiceprint.swift (NumPy for vDSP).

Every 10 ms frame above -40 dBFS is tested for voicing by normalized autocorrelation
(40 ms Hann window, 60–350 Hz). A voiced frame contributes a 16-coefficient cepstrum
(25 ms Hamming window, 512-point FFT, 32 mel bands from 60 to 7600 Hz) and its pitch.
Traces carry speaker IDs, never these features.
"""

from __future__ import annotations

import math

import numpy as np

COEFFICIENTS = 16
# Typical spread of each voiced-frame cepstral coefficient in speech (120 s drama excerpt).
SPREAD = np.array([12.05, 17.11, 15.91, 11.35, 8.44, 8.28, 7.72, 7.2, 6.91, 6.26, 6.07, 5.02, 4.93, 4.62, 4.45, 4.18])
PITCH_FLOOR_HZ = 60.0
PITCH_BINS_PER_OCTAVE = 24.0
PITCH_BINS = 64


class Voiceprint:
    """Voiced-frame statistics of one stretch of speech, or of a speaker cluster."""

    __slots__ = ("sum", "weight", "pitch")

    def __init__(self) -> None:
        self.sum = np.zeros(COEFFICIENTS)
        self.weight = 0.0
        self.pitch = np.zeros(PITCH_BINS)

    def copy(self) -> Voiceprint:
        v = Voiceprint()
        v.sum, v.weight, v.pitch = self.sum.copy(), self.weight, self.pitch.copy()
        return v

    @property
    def voiced_frames(self) -> int:
        return int(math.floor(self.weight + 0.5))

    @property
    def mean(self) -> np.ndarray:
        return self.sum / self.weight if self.weight > 0 else self.sum

    @property
    def median_pitch_hz(self) -> float | None:
        total = self.pitch.sum()
        if total <= 0:
            return None
        acc = 0.0
        for b, count in enumerate(self.pitch):
            acc += count
            if acc >= total / 2:
                return PITCH_FLOOR_HZ * 2 ** ((b + 0.5) / PITCH_BINS_PER_OCTAVE)
        return None

    def add_frame(self, cepstrum: np.ndarray, pitch_hz: float) -> None:
        self.sum += cepstrum.astype(np.float64) / SPREAD
        self.weight += 1
        b = int(math.floor(math.log2(pitch_hz / PITCH_FLOOR_HZ) * PITCH_BINS_PER_OCTAVE))
        self.pitch[min(PITCH_BINS - 1, max(0, b))] += 1

    def add(self, other: Voiceprint) -> None:
        self.sum += other.sum
        self.pitch += other.pitch
        self.weight += other.weight

    def cap(self, frames: int) -> None:
        """Bounds the weight so a cluster keeps adapting to its speaker's recent speech."""
        if self.weight <= frames:
            return
        f = frames / self.weight
        self.sum *= f
        self.pitch *= f
        self.weight = float(frames)

    def distance(self, other: Voiceprint) -> float:
        """RMS difference of the mean normalized cepstra."""
        if self.weight <= 0 or other.weight <= 0:
            return math.inf
        d = self.sum / self.weight - other.sum / other.weight
        return float(math.sqrt(np.dot(d, d) / COEFFICIENTS))


class _Tables:
    def __init__(self) -> None:
        n, m = VoiceprintAnalyzer.PITCH_WINDOW, VoiceprintAnalyzer.CEPSTRUM_WINDOW
        self.hann = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / (n - 1))).astype(np.float32)
        self.hamming = (0.54 - 0.46 * np.cos(2 * np.pi * np.arange(m) / (m - 1))).astype(np.float32)
        bands, size, sr = VoiceprintAnalyzer.BANDS, VoiceprintAnalyzer.FFT_SIZE, VoiceprintAnalyzer.SAMPLE_RATE

        def mel(f):
            return 2595 * math.log10(1 + f / 700)

        def hz(v):
            return 700 * (10 ** (v / 2595) - 1)

        lo, hi = mel(60), mel(7600)
        edges = [hz(lo + (hi - lo) * i / (bands + 1)) for i in range(bands + 2)]
        bins = [int(math.floor((size + 1) * e / sr)) for e in edges]
        fb = np.zeros((bands, size // 2 + 1), dtype=np.float32)
        for b in range(1, bands + 1):
            left, c, r = bins[b - 1], bins[b], bins[b + 1]
            for k in range(left, c):
                fb[b - 1, k] = (k - left) / max(1, c - left)
            for k in range(c, r):
                fb[b - 1, k] = (r - k) / max(1, r - c)
        self.filters = fb
        q = np.arange(1, COEFFICIENTS + 1)[:, None]
        b = np.arange(bands)[None, :]
        self.dct = np.cos(np.pi * q * (b + 0.5) / bands).astype(np.float32)


class VoiceprintAnalyzer:
    """Streams 16 kHz samples and adds each voiced frame to a caller-owned voiceprint."""

    SAMPLE_RATE = 16_000
    HOP = 160
    PITCH_WINDOW = 640
    CEPSTRUM_WINDOW = 400
    FFT_SIZE = 512
    BANDS = 32
    # Lags searched (400 Hz down to 60 Hz); peaks above 350 Hz are rejected as octave errors.
    MIN_LAG = 40
    MAX_LAG = 266
    MIN_VOICED_LAG = 46
    VOICING = 0.5
    LEVEL_DB = -40.0
    _tables: _Tables | None = None

    def __init__(self) -> None:
        if VoiceprintAnalyzer._tables is None:
            VoiceprintAnalyzer._tables = _Tables()
        self._t = VoiceprintAnalyzer._tables
        self._buffer = np.zeros(0, dtype=np.float32)
        self._buffer_start = 0
        self._next = 2
        self._total = 0

    def process(self, samples, into: Voiceprint | None) -> None:
        """Consumes `samples`. Voiced frames are added to `into`; with None, frames are only
        skipped (nothing is analyzed outside speech)."""
        x = np.asarray(samples, dtype=np.float32)
        self._buffer = np.concatenate([self._buffer, x])
        self._total += len(x)
        H, P = self.HOP, self.PITCH_WINDOW
        while self._next * H + H // 2 + P // 2 <= self._total:
            if into is not None:
                self._analyze(self._next, into)
            self._next += 1
        keep_from = self._next * H + H // 2 - P // 2
        if keep_from - self._buffer_start > 4_096:
            self._buffer = self._buffer[keep_from - self._buffer_start:]
            self._buffer_start = keep_from

    def _analyze(self, i: int, into: Voiceprint) -> None:
        x, t = self._buffer, self._t
        base = i * self.HOP - self._buffer_start
        center = base + self.HOP // 2
        hop = x[base:base + self.HOP]
        mean_square = float(np.mean(hop * hop, dtype=np.float32))
        if 10 * math.log10(mean_square + 1e-12) < self.LEVEL_DB:
            return
        n = self.PITCH_WINDOW
        start = center - n // 2
        window = x[start:start + n]
        f = (window - np.float32(np.mean(window, dtype=np.float32))) * t.hann
        padded = np.concatenate([f, np.zeros(self.MAX_LAG, np.float32)])
        lags = np.correlate(padded[: n + self.MAX_LAG - 1], f, mode="valid")[: self.MAX_LAG]
        if lags[0] <= 0:
            return
        index = int(np.argmax(lags[self.MIN_LAG:self.MAX_LAG]))
        peak = lags[self.MIN_LAG + index]
        lag = self.MIN_LAG + index
        if peak / lags[0] < self.VOICING or lag < self.MIN_VOICED_LAG:
            return
        m = self.CEPSTRUM_WINDOW
        c_start = center - m // 2
        frame = x[c_start:c_start + m] * t.hamming
        spectrum = np.fft.rfft(frame, self.FFT_SIZE)
        power = (spectrum.real ** 2 + spectrum.imag ** 2).astype(np.float32)
        log_mel = np.log(t.filters @ power + np.float32(1e-10)).astype(np.float32)
        cepstrum = t.dct @ log_mel
        into.add_frame(cepstrum, self.SAMPLE_RATE / lag)
