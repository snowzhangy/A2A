"""Clock-free bounded delayed-frame ring, ported from A2AVideo (ADR-0012)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DelayedFrame:
    presentation_ns: int
    bytes: int
    is_keyframe: bool
    payload: object


class DelayedFrameQueue:
    def __init__(self, max_bytes: int) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self.bytes = 0
        self.dropped_frames = 0
        self._frames: list[DelayedFrame] = []
        self._head = 0

    @property
    def count(self) -> int:
        return len(self._frames) - self._head

    @property
    def is_empty(self) -> bool:
        return self.count == 0

    @property
    def oldest_ns(self) -> int | None:
        return None if self.is_empty else self._frames[self._head].presentation_ns

    @property
    def newest_ns(self) -> int | None:
        return None if self.is_empty else self._frames[-1].presentation_ns

    @property
    def span_seconds(self) -> float:
        if self.oldest_ns is None or self.newest_ns is None or self.newest_ns <= self.oldest_ns:
            return 0.0
        return (self.newest_ns - self.oldest_ns) / 1e9

    def push(self, frame: DelayedFrame) -> bool:
        if self.is_empty and not frame.is_keyframe:
            return False
        self._frames.append(frame)
        self.bytes += frame.bytes
        while self.bytes > self.max_bytes and self.count > 1:
            self._drop_oldest_group()
        self._compact()
        return True

    def peek_due(self, through_ns: int) -> DelayedFrame | None:
        if self.is_empty or self._frames[self._head].presentation_ns > through_ns:
            return None
        return self._frames[self._head]

    def pop_first(self) -> DelayedFrame | None:
        if self.is_empty:
            return None
        frame = self._frames[self._head]
        self._head += 1
        self.bytes -= frame.bytes
        self._compact()
        return frame

    def clear(self) -> None:
        self._frames.clear()
        self._head = 0
        self.bytes = 0

    def _drop_oldest_group(self) -> None:
        while True:
            self.bytes -= self._frames[self._head].bytes
            self._head += 1
            self.dropped_frames += 1
            if self._head >= len(self._frames) or self._frames[self._head].is_keyframe:
                break
        if self.is_empty:
            self.clear()

    def _compact(self) -> None:
        if self._head > 256 and self._head * 2 > len(self._frames):
            del self._frames[: self._head]
            self._head = 0
