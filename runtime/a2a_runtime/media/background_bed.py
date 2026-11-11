"""Source-timeline delayed background bed with bounded, in-place original-voice restoration."""

from __future__ import annotations

import numpy as np

from ..core.types import SourceSpan


class BackgroundBed:
    def __init__(self, delay_frames: int, max_hold_frames: int | None = None) -> None:
        if delay_frames < 0:
            raise ValueError("delay_frames must be nonnegative")
        self.delay_frames = delay_frames
        self.max_hold_frames = max_hold_frames or delay_frames + 96_000
        self.pending = np.zeros(0, dtype=np.float32)
        self.emitted = 0
        self.received = 0
        self.dropped = 0
        self.restored = 0

    def push(self, samples) -> None:
        pcm = np.asarray(samples, dtype=np.float32)
        if not len(pcm):
            return
        self.pending = np.concatenate([self.pending, pcm])
        self.received += len(pcm)
        overflow = len(self.pending) - self.max_hold_frames
        if overflow > 0:
            self.pending = self.pending[overflow:]
            self.emitted += overflow
            self.dropped += overflow

    def drain(self, frontier: int) -> np.ndarray:
        ready = max(0, min(self.received, frontier - self.delay_frames) - self.emitted)
        if ready <= 0:
            return np.zeros(0, dtype=np.float32)
        out, self.pending = self.pending[:ready].copy(), self.pending[ready:]
        self.emitted += ready
        return out

    def replace(self, span: SourceSpan, samples) -> int:
        """Replace not-yet-emitted bed with the actor's isolated voice; 0 if it is too late."""
        pcm = np.asarray(samples, dtype=np.float32)
        start = max(span.start, self.emitted)
        end = min(span.end, self.received, span.start + len(pcm))
        if end <= start:
            return 0
        offset = start - self.emitted
        # `samples` starts at the span's start. The part of the line that has already left the
        # bed stays without its voice; what remains goes back at its own source time.
        skipped = start - span.start
        self.pending[offset: offset + end - start] = np.clip(
            self.pending[offset: offset + end - start] + pcm[skipped: skipped + end - start], -1, 1)
        self.restored += end - start
        return end - start

    def reset(self, base_frames: int = 0) -> None:
        self.pending = np.zeros(0, dtype=np.float32)
        self.emitted = self.received = base_frames
        self.dropped = self.restored = 0
