"""Bounded speech spans for a direct audio translator. Port of
apple/Sources/A2ACore/AudioSegmentPolicy.swift.

`AudioSegmentPolicy` does not infer words or sentence boundaries; its maximum bounds latency
during continuous speech. `AudioStreamFeed` decides what a streaming audio translator may hear
of a span before the span closes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .types import CANONICAL_SAMPLE_RATE, SourceSpan, SpeechEnd, SpeechStart


class AudioSegmentPolicy:
    """Close speech at pauses, or at a bounded cap using the quietest recent frame (ADR-0017)."""

    def __init__(self, max_seconds: float = 3.0, min_seconds: float = 0.25,
                 dip_window_seconds: float = 0.0) -> None:
        if not (max_seconds > 0 and 0 <= min_seconds < max_seconds):
            raise ValueError("AudioSegmentPolicy needs 0 <= min_seconds < max_seconds")
        if not (0 <= dip_window_seconds < max_seconds - min_seconds):
            raise ValueError("AudioSegmentPolicy needs 0 <= dip_window_seconds < max_seconds - min_seconds")
        self.max_samples = int(max_seconds * CANONICAL_SAMPLE_RATE)
        self.min_samples = int(min_seconds * CANONICAL_SAMPLE_RATE)
        self.dip_window_samples = int(dip_window_seconds * CANONICAL_SAMPLE_RATE)
        self._start: int | None = None
        self._levels: list[tuple[int, int, float]] = []
        self.pause_closes = 0
        self.cap_closes = 0

    @classmethod
    def sentences(cls, presentation_delay_seconds: float) -> AudioSegmentPolicy:
        # Leave 3.5 s for translation, voice onset and reserve; keep the cap within 3-6 s.
        return cls(max_seconds=min(6.0, max(3.0, presentation_delay_seconds - 3.5)),
                   min_seconds=0.25, dip_window_seconds=1.0)

    @property
    def open_start(self) -> int | None:
        return self._start

    @property
    def stream_limit_samples(self) -> int:
        # Audio in the dip window waits until the exact cut is known.
        return self.max_samples - self.dip_window_samples

    def process(self, events, frontier: int, level: tuple[SourceSpan, float] | None = None) -> list[SourceSpan]:
        result: list[SourceSpan] = []
        for event in events:
            if isinstance(event, SpeechStart):
                if self._start is None:
                    self._start = event.sample
            elif isinstance(event, SpeechEnd):
                if self._start is not None and event.sample - self._start >= self.min_samples:
                    result.append(SourceSpan(self._start, event.sample))
                    self.pause_closes += 1
                self._start = None
        if self.dip_window_samples > 0 and level is not None and self._start is not None and level[0].end > self._start:
            self._levels.append((level[0].start, level[0].end, level[1]))
        while self._start is not None and frontier - self._start >= self.max_samples:
            end = self._cut(self._start)
            result.append(SourceSpan(self._start, end))
            self.cap_closes += 1
            self._start = end
        if self._start is not None:
            first = next((i for i, frame in enumerate(self._levels) if frame[1] > self._start), None)
            if first is None:
                self._levels.clear()
            else:
                del self._levels[:first]
        else:
            self._levels.clear()
        return result

    def _cut(self, start: int) -> int:
        limit = start + self.max_samples
        window = limit - self.dip_window_samples
        best: tuple[int, float] | None = None
        for frame_start, frame_end, db in self._levels:
            if frame_start >= window and frame_end <= limit and (best is None or db <= best[1]):
                best = ((frame_start + frame_end) // 2, db)
        return best[0] if best is not None else limit


class AdaptiveSpeechThreshold:
    """Track the midpoint between recent noise and speech, never below the fixed floor."""

    def __init__(self, floor_db: float, window_seconds: float = 6.0, min_contrast_db: float = 6.0) -> None:
        if window_seconds <= 0:
            raise ValueError("AdaptiveSpeechThreshold needs window_seconds > 0")
        self.floor_db = floor_db
        self.window_samples = int(window_seconds * CANONICAL_SAMPLE_RATE)
        self.min_contrast_db = min_contrast_db
        self._frames: list[tuple[int, float]] = []

    def update(self, span: SourceSpan, db: float) -> float:
        self._frames.append((span.end, max(-120.0, db) if math.isfinite(db) else -120.0))
        first = next((i for i, frame in enumerate(self._frames)
                      if frame[0] > span.end - self.window_samples), None)
        if first is not None and first > 0:
            del self._frames[:first]
        ordered = sorted(frame[1] for frame in self._frames)
        low = ordered[int(0.1 * (len(ordered) - 1))]
        high = ordered[int(0.8 * (len(ordered) - 1))]
        if high - low < self.min_contrast_db:
            return self.floor_db
        return max(self.floor_db, low + 0.5 * (high - low))


@dataclass(frozen=True, slots=True)
class Append:
    """Send this audio now."""

    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Close:
    """The span is committed and its audio complete: ask for the answer."""

    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Discard:
    """The open span will never close: forget what was sent for it."""


class AudioStreamFeed:
    """Audio is offered up to the detector's last voiced sample only: a later speech end
    reports that sample or a later one, so nothing streamed ever lies past the end of the span
    it belongs to. Every closed span is delivered whole, in order, as appends that tile it
    exactly, then `Close`; a span the segmenter drops (too short) is withdrawn with `Discard`."""

    def __init__(self, max_samples: int, min_append_seconds: float = 0.1) -> None:
        if max_samples <= 0 or min_append_seconds < 0:
            raise ValueError("AudioStreamFeed needs max_samples > 0 and min_append_seconds >= 0")
        self.max_samples = max_samples
        # Smallest append while a span is open (the translator consumes 800 ms turns).
        self.min_append_samples = int(min_append_seconds * CANONICAL_SAMPLE_RATE)
        self._open: tuple[int, int] | None = None  # (start, sent)

    def update(self, closed: list[SourceSpan], open_start: int | None, voiced_end: int | None) -> list:
        """`closed` and `open_start` come from `AudioSegmentPolicy.process` and `open_start`
        after it; `voiced_end` is the detector's last voiced sample while in speech, else None."""
        actions: list = []
        for span in closed:
            frm = span.start
            if self._open is not None:
                if self._open[0] == span.start:
                    frm = min(self._open[1], span.end)
                else:
                    actions.append(Discard())
                self._open = None
            if frm < span.end:
                actions.append(Append(SourceSpan(frm, span.end)))
            actions.append(Close(span))
        if open_start is None:
            if self._open is not None:
                actions.append(Discard())
            self._open = None
            return actions
        if self._open is not None and self._open[0] != open_start:
            actions.append(Discard())
            self._open = None
        start, sent = self._open if self._open is not None else (open_start, open_start)
        if voiced_end is not None:
            safe = min(voiced_end, open_start + self.max_samples)
            if safe - sent >= max(1, self.min_append_samples):
                actions.append(Append(SourceSpan(sent, safe)))
                sent = safe
        self._open = (start, sent)
        return actions
