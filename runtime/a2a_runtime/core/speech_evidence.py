"""Reject recognizer hallucinations whose time ranges contain no detected speech.
Port of apple/Sources/A2ACore/SpeechEvidence.swift.

The history is bounded in source time; delayed finals can still match a completed VAD span.
"""

from __future__ import annotations

from .types import CANONICAL_SAMPLE_RATE, ASRHypothesis, SourceSpan, SpeechEnd, SpeechStart


class SpeechEvidence:
    def __init__(self, history_seconds: int = 60) -> None:
        self._spans: list[SourceSpan] = []
        self._active_start: int | None = None
        self._frontier = 0
        self._history_samples = max(1, history_seconds) * CANONICAL_SAMPLE_RATE

    def advance(self, sample: int) -> None:
        self._frontier = max(self._frontier, sample)
        floor = self._frontier - self._history_samples
        self._spans = [s for s in self._spans if s.end >= floor]

    def process(self, event) -> None:
        if isinstance(event, SpeechStart):
            self._active_start = event.sample
        elif isinstance(event, SpeechEnd):
            start = self._active_start
            if start is not None and event.sample > start:
                self._spans.append(SourceSpan(start, event.sample))
            self._active_start = None

    def accepts(self, h: ASRHypothesis) -> bool:
        timed = [t.span for t in h.tokens if t.span is not None and not t.span.is_empty]
        # Untimed adapters cannot establish overlap; require recent speech instead.
        if not timed:
            if self._active_start is not None:
                return True
            return bool(self._spans) and h.audio_frontier - self._spans[-1].end <= CANONICAL_SAMPLE_RATE
        evidence = list(self._spans)
        if self._active_start is not None and self._frontier > self._active_start:
            evidence.append(SourceSpan(self._active_start, self._frontier))
        return any(e.start < t.end and t.start < e.end for t in timed for e in evidence)
