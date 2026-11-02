"""Voice reference acquisition. Port of apple/Sources/A2ACore/ReferencePolicy.swift.

Pure: the driver measures candidate segments and cuts PCM; this policy decides which
finalized utterances form the reference and when a profile version is due. Only finalized
utterances with recognizer token timing qualify, cut on their token spans. The first version
is due as soon as enough clean speech exists; one upgraded version follows at the target
length, then acquisition stops.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .text import trim_whitespace
from .types import ASRHypothesis, SourceSpan, SpeakerID


@dataclass
class ReferencePolicyConfig:
    # Voiced audio needed before the first profile version.
    min_seconds: float = 3.0
    # Voiced audio for the upgraded (final) profile version.
    target_seconds: float = 8.0
    # Reference length cap; longer prefill slows every synthesis.
    max_seconds: float = 10.0
    min_segment_seconds: float = 0.3
    # Context kept around the first and last token so onsets and decays are not clipped.
    padding_samples: int = 4_800
    min_level_db: float = -45.0
    max_clipped_fraction: float = 0.002


@dataclass(frozen=True, slots=True)
class ReferenceSegment:
    span: SourceSpan
    text: str


@dataclass(frozen=True, slots=True)
class ReferencePlan:
    speaker: SpeakerID
    version: int
    segments: tuple[ReferenceSegment, ...]

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)

    @property
    def seconds(self) -> float:
        return sum(s.span.duration_seconds for s in self.segments)


class ReferenceRejection:
    NO_TIMING = "no_timing"
    EMPTY = "empty"
    TOO_SHORT = "too_short"
    TOO_LONG = "too_long"
    QUIET = "quiet"
    CLIPPED = "clipped"
    COMPLETE = "complete"


@dataclass(frozen=True, slots=True)
class Rejected:
    reason: str


class ReferencePolicy:
    def __init__(self, speaker: SpeakerID, config: ReferencePolicyConfig | None = None) -> None:
        self.config = config or ReferencePolicyConfig()
        self.speaker = speaker
        self._segments: list[ReferenceSegment] = []
        self._version = 0
        self._issued_segments = 0

    @property
    def is_complete(self) -> bool:
        """True once the upgraded version was issued; later utterances are not needed."""
        return self._version >= 2

    @property
    def has_issued_profile(self) -> bool:
        return self._version > 0

    @property
    def accepted_seconds(self) -> float:
        return sum(s.span.duration_seconds for s in self._segments)

    def speech_span(self, h: ASRHypothesis) -> SourceSpan | None:
        """Padded union of a final hypothesis' token spans, or None without recognizer timing."""
        span = self.token_span(h)
        if span is None:
            return None
        return SourceSpan(max(0, span.start - self.config.padding_samples), span.end + self.config.padding_samples)

    @staticmethod
    def token_span(h: ASRHypothesis) -> SourceSpan | None:
        """Speaker coverage is measured on speech timing, excluding synthetic padding."""
        if not h.is_final:
            return None
        spans = [t.span for t in h.tokens if t.span is not None and not t.span.is_empty]
        if not spans:
            return None
        return SourceSpan(min(s.start for s in spans), max(s.end for s in spans))

    def offer(self, span: SourceSpan, text: str, level_db: float,
              clipped_fraction: float) -> ReferencePlan | None | Rejected:
        """Offer one measured, finalized utterance. Returns the plan for a new profile version
        when this segment makes one due, None when accepted without one, or `Rejected`."""
        cfg = self.config
        if self.is_complete:
            return Rejected(ReferenceRejection.COMPLETE)
        trimmed = trim_whitespace(text)
        if not trimmed:
            return Rejected(ReferenceRejection.EMPTY)
        if span.duration_seconds < cfg.min_segment_seconds:
            return Rejected(ReferenceRejection.TOO_SHORT)
        if not math.isfinite(level_db) or level_db < cfg.min_level_db:
            return Rejected(ReferenceRejection.QUIET)
        if clipped_fraction > cfg.max_clipped_fraction:
            return Rejected(ReferenceRejection.CLIPPED)
        if self.accepted_seconds + span.duration_seconds > cfg.max_seconds:
            # After the first version, a segment that cannot fit ends acquisition with what
            # was gathered (an upgrade only when it adds audio).
            if self._version == 1:
                if len(self._segments) > self._issued_segments:
                    return self._issue()
                self._version = 2
            return Rejected(ReferenceRejection.TOO_LONG)
        self._segments.append(ReferenceSegment(span, trimmed))
        total = self.accepted_seconds
        if self._version == 0 and total >= cfg.min_seconds:
            return self._issue()
        if self._version == 1 and total >= cfg.target_seconds:
            return self._issue()
        return None

    def reset(self, speaker: SpeakerID) -> None:
        """A new speaker (or a source discontinuity) starts acquisition over."""
        self.speaker = speaker
        self._segments.clear()
        self._version = 0
        self._issued_segments = 0

    def _issue(self) -> ReferencePlan:
        self._version += 1
        self._issued_segments = len(self._segments)
        return ReferencePlan(self.speaker, self._version, tuple(self._segments))
