"""Bounded streaming speech pacing; no model, device, file, or network access.

WSOLA changes duration while retaining pitch. Limits deliberately favor intelligibility
over an exact duration match. Silence compaction only affects unusually long quiet runs.
NumPy is optional so the standard-library fake worker can import this module; without it,
`available` is False and callers skip pacing.
"""
from __future__ import annotations

import math

try:
    import numpy as np
except ImportError:  # standard-library fake worker
    np = None

available = np is not None
# Faster speech costs more naturalness than the latency it saves; the host's catch-up
# skipping handles the remaining debt. Matches the system voice's cap.
MAX_SPEED = 1.3


class QuietRunCompactor:
    def __init__(self, rate: int = 24000, max_pause: float = 0.18, floor_db: float = -50):
        self.np = np
        self.frame = rate // 100
        self.limit = round(max_pause * rate)
        self.floor = 10 ** (floor_db / 20)
        self.quiet = 0
        self.removed = 0
        self.pending = np.empty(0, dtype=np.float32)

    def process(self, samples, final=False):
        np = self.np
        x = np.concatenate((self.pending, np.asarray(samples, dtype=np.float32)))
        end = len(x) if final else len(x) // self.frame * self.frame
        out = []
        for i in range(0, end, self.frame):
            part = x[i:min(end, i + self.frame)]
            if np.max(np.abs(part), initial=0) < self.floor:
                keep = min(len(part), max(0, self.limit - self.quiet))
                self.quiet += len(part)
                self.removed += len(part) - keep
                if keep:
                    out.append(part[:keep])
            else:
                self.quiet = 0
                out.append(part)
        self.pending = x[end:]
        return np.concatenate(out) if out else np.empty(0, dtype=np.float32)


class StreamingWSOLA:
    def __init__(self, speed: float, rate: int = 24000):
        if not math.isfinite(speed) or not 1 <= speed <= 1.6:
            raise ValueError("Speech speed must be in [1, 1.6]")
        self.np = np
        self.speed = speed
        self.hop = rate // 50  # 20 ms synthesis hop, 40 ms window
        # ±8.3 ms covers half a pitch period down to 60 Hz, so low voices find an aligned
        # overlap instead of crossfading out of phase.
        self.search = rate // 120
        self.fade = np.linspace(0, 1, self.hop, endpoint=False, dtype=np.float32)
        self.data = np.empty(0, dtype=np.float32)
        self.base = 0
        self.received = 0
        self.emitted = 0
        self.next_position = 0.0
        self.tail = None

    def process(self, samples, final=False):
        np = self.np
        x = np.asarray(samples, dtype=np.float32).reshape(-1)
        self.received += len(x)
        if self.speed == 1:
            self.emitted += len(x)
            return x.copy()
        self.data = np.concatenate((self.data, x))
        target = round(self.received / self.speed)
        pieces = []
        while self.emitted < target:
            nominal = round(self.next_position)
            last_needed = nominal + 2 * self.hop + (self.search if self.tail is not None else 0)
            if not final and last_needed > self.received:
                break
            lo = max(self.base, nominal - (self.search if self.tail is not None else 0))
            hi = max(lo, nominal + (self.search if self.tail is not None else 0))
            required = hi - self.base + 2 * self.hop
            data = self.data if required <= len(self.data) else np.pad(self.data, (0, required - len(self.data)))
            chosen = lo
            if self.tail is not None:
                region = data[lo - self.base:hi - self.base + self.hop]
                scores = np.correlate(region, self.tail, mode="valid")
                energies = np.convolve(region * region, np.ones(self.hop, dtype=np.float32), mode="valid")
                scores = scores / np.sqrt(np.maximum(energies, 1e-12))
                chosen += int(np.argmax(scores))
            window = data[chosen - self.base:chosen - self.base + 2 * self.hop]
            block = window[:self.hop].copy()
            if self.tail is not None:
                block = self.tail * (1 - self.fade) + block * self.fade
            self.tail = window[self.hop:].copy()
            n = min(self.hop, target - self.emitted) if final else self.hop
            pieces.append(block[:n])
            self.emitted += n
            self.next_position += self.hop * self.speed
            drop_to = min(self.received, max(self.base, math.floor(self.next_position) - self.search - 1))
            drop = drop_to - self.base
            self.data = self.data[drop:].copy()
            self.base = drop_to
        return np.concatenate(pieces) if pieces else np.empty(0, dtype=np.float32)


def choose_speed(text: str, target_seconds, seconds_per_character: float) -> float:
    if not isinstance(target_seconds, (float, int)) or not math.isfinite(target_seconds) or target_seconds <= 0:
        return 1.0
    estimated = max(0.2, len(text) * seconds_per_character)
    # Small timing errors should not alter a short acknowledgement's cadence.
    return min(MAX_SPEED, max(1.0, estimated / max(0.5, target_seconds + 0.15)))
