"""Bounded streaming speech pacing; no model, device, file, or network access.

WSOLA changes duration while retaining pitch. Limits deliberately favor intelligibility
over an exact duration match. Silence compaction only affects unusually long quiet runs.
Pacing is symmetric: a take longer than the speech it translates speeds up, and a take
shorter than that speech slows down so the line ends where the actor stops (fill). Only
the host that plays each phrase in its own slot asks for the slow side, because there a
shorter take is silence over the actor's own tail rather than saved latency.
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
# Slowest speech worth emitting. dub-anything re-times each take between 0.8x and 1.25x of
# the model's own pace and its listeners preferred the re-timed dub: the exchange's rhythm,
# lines taking the time the actor takes, mattered at least as much as the acting. It resamples
# (pitch moves with duration); WSOLA keeps pitch, and holding the time rather than the pitch
# is the point here. A stretched overlap shows up as smearing well before 15% does, so A2A
# stops at 0.85x and leaves the remaining deficit as the actor's own silence.
MIN_SPEED = 0.85
# Widest stretch the pacer can compute, so a caller asking for more gets the floor, not a
# rejected frame.
MIN_COMPUTABLE_SPEED = 0.75
# A deficit smaller than this does not restyle a line's cadence, mirroring the +0.15 s the
# speed-up side allows. Filling changes every syllable's length; a 0.1 s overhang does not.
FILL_GUARD_SECONDS = 0.25


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
        if not math.isfinite(speed) or not MIN_COMPUTABLE_SPEED <= speed <= 1.6:
            raise ValueError(f"Speech speed must be in [{MIN_COMPUTABLE_SPEED}, 1.6]")
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


def choose_speed(text: str, target_seconds, seconds_per_character: float, estimated_seconds=None,
                 allow_fill: bool = False) -> float:
    """Pace one take against the seconds of source speech it translates.

    Above 1 the take is longer than its source and gets faster. Below 1 (`allow_fill`, the
    host that plays phrases in slots) a take shorter than its source is slowed so it ends
    with the actor instead of leaving the tail of their turn silent. Speed-up allows a 0.15 s
    overhang and fill a `FILL_GUARD_SECONDS` deficit before it touches the cadence.
    """
    if not isinstance(target_seconds, (float, int)) or not math.isfinite(target_seconds) or target_seconds <= 0:
        return 1.0
    if isinstance(estimated_seconds, (float, int)) and math.isfinite(estimated_seconds) and estimated_seconds > 0:
        estimated = estimated_seconds
    else:
        estimated = max(0.2, len(text) * seconds_per_character)
    # Small timing errors should not alter a short acknowledgement's cadence.
    if allow_fill and estimated < target_seconds - FILL_GUARD_SECONDS:
        return max(MIN_SPEED, estimated / max(0.5, target_seconds))
    return min(MAX_SPEED, max(1.0, estimated / max(0.5, target_seconds + 0.15)))
