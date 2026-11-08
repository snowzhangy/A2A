"""Core value types shared by every stage. Port of apple/Sources/A2ACore/Types.swift.

Pure data: no I/O. Keep field names and semantics in lockstep with the Swift source;
tests/test_golden.py replays the Swift golden traces through this port byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass, field

CANONICAL_SAMPLE_RATE = 48_000
UINT64_MASK = (1 << 64) - 1
UINT32_MASK = (1 << 32) - 1


def wrapping_sub_u64(a: int, b: int) -> int:
    """Swift `&-` on UInt64."""
    return (a - b) & UINT64_MASK


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """Half-open span of canonical 48 kHz source samples since the current epoch started."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(f"SourceSpan end {self.end} precedes start {self.start}")

    @property
    def is_empty(self) -> bool:
        return self.end == self.start

    @property
    def count(self) -> int:
        return self.end - self.start

    @property
    def duration_seconds(self) -> float:
        return self.count / CANONICAL_SAMPLE_RATE

    def union(self, other: SourceSpan) -> SourceSpan:
        return SourceSpan(min(self.start, other.start), max(self.end, other.end))

    def contains(self, sample: int) -> bool:
        return self.start <= sample < self.end

    def __str__(self) -> str:
        return f"[{self.start},{self.end})"


@dataclass(frozen=True, slots=True, order=True)
class UnitID:
    raw_value: int

    def __str__(self) -> str:
        return f"u{self.raw_value}"


@dataclass(frozen=True, slots=True)
class SpeakerID:
    raw_value: str

    def __str__(self) -> str:
        return self.raw_value


@dataclass(frozen=True, slots=True)
class AudioFrame16k:
    """Mono 16 kHz frame for VAD/speaker/ASR. `span` is in canonical 48 kHz samples."""

    span: SourceSpan
    samples: object  # numpy float32 array or sequence of floats


# MARK: VAD and speaker events


@dataclass(frozen=True, slots=True)
class SpeechStart:
    sample: int


@dataclass(frozen=True, slots=True)
class SpeechEnd:
    sample: int


VADEvent = SpeechStart | SpeechEnd


@dataclass(frozen=True, slots=True)
class SpeakerAssign:
    speaker: SpeakerID
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class SpeakerChange:
    span: SourceSpan


SpeakerEvent = SpeakerAssign | SpeakerChange


# MARK: Recognition and translation


@dataclass(frozen=True, slots=True)
class ASRToken:
    text: str
    # Recognizer-reported timing, when the adapter provides it.
    span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class ASRHypothesis:
    # Recognizer utterance counter; comparison state resets when it changes.
    utterance: int
    tokens: tuple[ASRToken, ...]
    is_final: bool
    # Latest canonical source sample the recognizer had accepted for this hypothesis.
    audio_frontier: int

    @property
    def text(self) -> str:
        return "".join(t.text for t in self.tokens)


@dataclass(frozen=True, slots=True)
class CommittedPair:
    source: str
    target: str


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    unit_id: UnitID
    # Last N committed source/target pairs.
    context: tuple[CommittedPair, ...]
    glossary: tuple[tuple[str, str], ...]
    uncommitted_source: str
    # Prefix within this unit; empty for v0 closed-unit mode.
    committed_target_prefix: str = ""
    # Audio corresponding to this committed source unit, in canonical sample time.
    source_span: SourceSpan | None = None


@dataclass(frozen=True, slots=True)
class TranslationWorkerStats:
    """What a streaming speech translator spent on one span, numbers only (`mt.result` `worker_*`)."""

    # Worker time after the span closed: what the answer still cost once its audio was complete.
    after_close_ms: int
    # Conversation turns the span was fed as.
    turns: int
    # Unanswered speech went through whole-segment translation at the close.
    sentence_fallback: bool


@dataclass(frozen=True, slots=True)
class TranslationCandidate:
    unit_id: UnitID
    text: str
    # True when the adapter reports the generation completed rather than hitting a cap.
    complete: bool = True
    # Timing a resident translation worker reported for this answer; None for other adapters.
    worker_stats: TranslationWorkerStats | None = None


# MARK: Voice profiles and synthesis


@dataclass(frozen=True, slots=True)
class ReferenceQuality:
    duration_seconds: float
    snr_estimate_db: float
    clipped_fraction: float
    overlap_suspected: bool


@dataclass(frozen=True, slots=True, eq=False)
class VoiceProfile:
    speaker_id: SpeakerID
    version: int
    # 3–10 s voiced, resampled from the 48 kHz capture branch to the model's native rate.
    reference_pcm: object = field(repr=False)
    # From our own ASR of the reference clip.
    reference_text: str
    sample_rate: int
    channel_count: int
    quality: ReferenceQuality
    # Median voiced pitch from the speaker tracker, when known. Plumbing voices are cast by it.
    pitch_hz: float | None = None


@dataclass(frozen=True, slots=True)
class PreparedVoice:
    """Opaque per-adapter conditioning prepared once per profile version."""

    speaker_id: SpeakerID
    profile_version: int
    handle: int


@dataclass(frozen=True, slots=True)
class PCMFormat:
    sample_rate: int
    channel_count: int


@dataclass(frozen=True, slots=True)
class PCMChunkHeader:
    """Identity of a synthesized chunk. The host admits it only through `PlayoutGate`."""

    unit_id: UnitID
    speaker_id: SpeakerID
    profile_version: int
    session_epoch: int
    playout_generation: int
    format: PCMFormat
    # Source span this chunk translates.
    span: SourceSpan
    is_last: bool
