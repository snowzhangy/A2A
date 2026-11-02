"""Fitting one translated line into the live timeline, and mixing it over the delayed source.

The output timeline is the input timeline shifted by `delay` seconds: at output time T the
listener hears input audio from T - delay. That one rule does most of the work — a line whose
actor stopped at input time t belongs at output t + delay, and the pipeline only has to be
faster than `delay` to put it there.

What it cannot do is cut a line off, or run two English voices at once. Those two rules generate
the whole collision policy: wait, then speak faster, then give up on the newer line and keep its
subtitle.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from paths import MIX_RATE


@dataclass
class Slot:
    start: float          # output seconds
    tempo: float = 1.0    # >1 speaks faster
    why: str = "on time"


@dataclass
class Line:
    """A dubbed line, possibly still arriving: the engine streams and playback starts early."""

    index: int
    start: float                  # output seconds where it begins to play
    planned: float                # expected length, from the engine's own duration estimate
    level: float = 1.0            # gain matched to the actor's loudness
    text: str = ""
    speaker: str = ""
    why: str = "on time"
    closed: bool = False          # the engine has produced all of it
    underruns: int = 0
    played_from: float = -1.0
    cursor: int = 0
    _chunks: list[np.ndarray] = field(default_factory=list)
    _mixed: np.ndarray | None = None
    supplied: int = 0

    def buffer(self, chunk: np.ndarray) -> None:
        chunk = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if len(chunk):
            self._chunks.append(chunk)
            self.supplied += len(chunk)
            self._mixed = None

    def close(self) -> None:
        self.closed = True

    @property
    def samples(self) -> np.ndarray:
        if self._mixed is None:
            self._mixed = (np.concatenate(self._chunks) if self._chunks
                           else np.zeros(0, dtype=np.float32))
        return self._mixed

    @property
    def duration(self) -> float:
        """Final length once closed; the estimate while it is still arriving."""
        return (len(self.samples) / MIX_RATE) if self.closed else self.planned

    @property
    def end(self) -> float:
        return self.start + self.duration

    @property
    def pending(self) -> bool:
        return (not self.closed) or self.cursor < len(self.samples)


def budget_words(seconds: float, words_per_second: float, margin: float = 0.92) -> int:
    """How much English fits in `seconds` of speech. Take words_per_second from the engine."""
    return max(3, int(seconds * words_per_second * margin))


def fit(end_input: float, delay: float, ready_at: float, duration: float,
        free_until: float, last_end: float, max_catchup: float = 1.0,
        max_tempo: float = 1.18, max_line: float = 12.0) -> Slot:
    """Where to place a line, given what is already playing.

    end_input  when the actor stopped (input seconds)
    ready_at   output-clock time at which the line's first audio exists
    duration   the line's length at normal speed
    free_until output time by which it should be finished — usually the next actor's onset,
               shifted by the same delay
    last_end   output time the previous dubbed line finishes
    """
    target = end_input + delay
    start = max(target, ready_at)
    why = "late" if start - target > 0.05 else "on time"
    if duration > max_line:
        return Slot(start, 1.0, "skipped:too-long")
    deadline = max(free_until, start + 0.6, last_end + 0.6)
    if start < last_end + 0.05:                 # never talk over our own voice
        pushed = last_end + 0.05
        if pushed + duration <= deadline + max_catchup:
            return Slot(pushed, 1.0, "pushed")
        start, why = pushed, "late"             # wait as far as we can, then speak faster
    if start + duration <= deadline + 0.15:
        return Slot(start, 1.0, why)
    room = max(0.2, deadline - start)           # speaking fast beats stepping on the next line
    tempo = min(max_tempo, duration / room)
    if start + duration / tempo <= deadline + 0.15:
        return Slot(start, tempo, "sped")
    return Slot(start, 1.0, "skipped:busy")


def loudness(audio: np.ndarray) -> float:
    """Voiced RMS: the average of the loudest 40% of 10 ms frames."""
    if len(audio) == 0:
        return 0.0
    frame = max(1, len(audio) // 200)
    n = len(audio) // frame
    if n == 0:
        return float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    e = np.sqrt((audio[: n * frame].reshape(n, frame).astype(np.float64) ** 2).mean(axis=1))
    loud = e[e >= max(0.004, np.percentile(e, 60))]
    if len(loud) == 0:
        loud = e
    return float(np.sqrt(np.mean(loud ** 2)))


def match_gain(line_audio: np.ndarray, actor_audio: np.ndarray, floor: float = 0.02,
               ceiling: float = 3.0) -> float:
    """Set the dubbed line's gain from the actor's own loudness in that window."""
    want, got = loudness(actor_audio), loudness(line_audio)
    if want <= 1e-4 or got <= 1e-4:
        return 1.0
    return float(np.clip(want / got, floor, ceiling))


@dataclass
class Mixer:
    """Delayed source bed with the dubbed lines mixed over it, ducked while they speak."""

    delay: float = 2.5
    duck: float = 0.38          # ~-8 dB: music and effects stay, the actor's voice steps back
    ramp: float = 0.03
    rate: int = MIX_RATE
    lines: list[Line] = field(default_factory=list)
    pos: int = 0                # output samples emitted
    clipped: int = 0
    _state: list[float] = field(default_factory=lambda: [0.0])
    _bed: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float32))

    def add_input(self, samples: np.ndarray) -> None:
        if len(samples):
            self._bed = np.concatenate([self._bed, samples])

    def _envelope(self, start: int, n: int, active: list[Line]) -> np.ndarray:
        g = np.ones(n, dtype=np.float32)
        for line in active:
            s = int(line.start * self.rate)
            lo, hi = max(start, s), min(start + n, int(line.end * self.rate))
            if hi > lo:
                g[lo - start: hi - start] = self.duck
        # Ramp with a one-pole filter that carries its state across blocks. Convolving each block
        # separately would fade the envelope at every block edge: a 100 Hz hum on the bed.
        tau = max(1.0, self.ramp * self.rate / 3.0)
        a = float(np.exp(-1.0 / tau))
        from scipy.signal import lfilter
        g, state = lfilter([1.0 - a], [1.0, -a], g, zi=np.array([self._state[0] - g[0]]))
        self._state[0] = float(state[-1]) + float(g[-1])
        return g.astype(np.float32)

    def block(self, n: int) -> np.ndarray:
        """n samples of output at the current output clock."""
        out = np.zeros(n, dtype=np.float32)
        bed_from = self.pos - int(self.delay * self.rate)
        if bed_from >= 0:
            chunk = self._bed[bed_from: bed_from + n]
            out[: len(chunk)] = chunk
        elif bed_from + n > 0:                    # the opening blocks, with the delay still ahead
            chunk = self._bed[0: bed_from + n]
            out[-bed_from: -bed_from + len(chunk)] = chunk

        lo, hi = self.pos, self.pos + n
        active = [l for l in self.lines if int(l.start * self.rate) < hi
                  and int(l.end * self.rate) > lo]
        out *= self._envelope(self.pos, n, active) if active else 1.0
        for line in active:
            s = int(line.start * self.rate)
            offset = self.pos - s                 # output index -> this line's sample index
            a, b = max(0, -offset), min(n, line.supplied - offset)
            if b > a:
                out[a:b] += line.samples[a + offset: b + offset] * line.level
                line.cursor = b + offset
                if line.played_from < 0:
                    line.played_from = self.pos / self.rate
            elif not line.closed:
                line.underruns += 1               # slower than playback: silence, never a stutter
        self.lines = [l for l in self.lines if l.pending]
        peak = float(np.max(np.abs(out))) if n else 0.0
        if peak > 1.0:
            self.clipped += 1
            out /= peak
        self.pos += n
        return out

    @property
    def time(self) -> float:
        return self.pos / self.rate
