"""DSP speaker tracker (ADR-0005). Port of apple/Sources/A2AModels/SpeakerTracking.swift.

Labels each stretch of speech with a speaker so every voice in a dialogue gets its own
synthesized voice. No model: speakers are told apart by the mel cepstrum of voiced frames.
The tracker prefers merging, because one actor alternating between voices is worse than two
actors sharing one.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.types import CANONICAL_SAMPLE_RATE, AudioFrame16k, SourceSpan, SpeakerAssign, SpeakerID, SpeechEnd, SpeechStart
from ..media.voiceprint import Voiceprint, VoiceprintAnalyzer


@dataclass
class TrackerConfig:
    max_speakers: int = 4
    # Distance (in typical cepstral spreads) beyond which a segment may be a new speaker.
    new_speaker_distance: float = 0.5
    # Distance that creates a speaker without confirmation.
    immediate_distance: float = 0.8
    # A confirmed speaker's combined print must stay this far from every existing one.
    confirmed_distance: float = 0.45
    # An unmatched segment older than this (canonical samples) cannot be confirmed.
    candidate_samples: int = 15 * CANONICAL_SAMPLE_RATE
    # Voiced 10 ms frames needed to label a segment at all.
    min_voiced_frames: int = 40
    # Short replies can reuse a confidently matching known speaker without creating one.
    min_continuation_frames: int = 12
    # Voiced frames needed for a confirmed or immediate new speaker.
    min_new_speaker_frames: int = 80
    min_immediate_frames: int = 50
    # Long speech is labeled in pieces of at most this many canonical samples.
    max_segment_samples: int = 3 * CANONICAL_SAMPLE_RATE
    # A speaker's print follows their recent speech: older frames are down-weighted.
    cluster_frames: int = 600


class VoiceprintSpeakerTracker:
    def __init__(self, config: TrackerConfig | None = None) -> None:
        self.config = config or TrackerConfig()
        self._analyzer = VoiceprintAnalyzer()
        self._speakers: list[tuple[SpeakerID, Voiceprint]] = []
        self._candidate: Voiceprint | None = None
        self._candidate_end = 0
        self._segment_start: int | None = None
        self._segment: Voiceprint | None = None

    @property
    def speaker_count(self) -> int:
        return len(self._speakers)

    def voiceprint(self, speaker: SpeakerID) -> Voiceprint | None:
        return next((p for s, p in self._speakers if s == speaker), None)

    def pitch_hz(self, speaker: SpeakerID) -> float | None:
        p = self.voiceprint(speaker)
        return p.median_pitch_hz if p is not None else None

    def process(self, frame: AudioFrame16k, vad: list) -> list:
        events: list = []
        for e in vad:
            if isinstance(e, SpeechStart):
                if self._segment_start is None:
                    self._open(e.sample)
            elif isinstance(e, SpeechEnd):
                start = self._segment_start if self._segment_start is not None else e.sample
                events += self._close(max(e.sample, start))
        self._analyzer.process(frame.samples, self._segment)
        if self._segment_start is not None and frame.span.end - self._segment_start >= self.config.max_segment_samples:
            events += self._close(frame.span.end)
            self._open(frame.span.end)
        return events

    def _open(self, sample: int) -> None:
        self._segment_start = sample
        self._segment = Voiceprint()

    def _close(self, end: int) -> list:
        start, vp = self._segment_start, self._segment
        self._segment_start, self._segment = None, None
        if start is None or vp is None or end <= start:
            return []
        cfg = self.config
        if vp.voiced_frames < cfg.min_voiced_frames:
            if vp.voiced_frames < cfg.min_continuation_frames or not self._speakers:
                return []
            known = min(self._speakers, key=lambda s: s[1].distance(vp))
            if known[1].distance(vp) > cfg.new_speaker_distance:
                return []
            speaker = known[0]
        else:
            speaker = self._classify(vp, start, end)
            if speaker is None:
                return []
        return [SpeakerAssign(speaker, SourceSpan(start, end))]

    def _classify(self, vp: Voiceprint, start: int, end: int) -> SpeakerID | None:
        cfg = self.config
        if not self._speakers:
            return self._add(vp)
        if self._candidate is not None and start - self._candidate_end > cfg.candidate_samples:
            self._candidate = None
        nearest, distance = 0, float("inf")
        for i, (_, p) in enumerate(self._speakers):
            d = p.distance(vp)
            if d < distance:
                nearest, distance = i, d
        frames = vp.voiced_frames
        room = len(self._speakers) < cfg.max_speakers
        if distance <= cfg.new_speaker_distance:
            self._speakers[nearest][1].add(vp)
            self._speakers[nearest][1].cap(cfg.cluster_frames)
        elif room and distance > cfg.immediate_distance and frames >= cfg.min_immediate_frames:
            self._candidate = None
            return self._add(vp)
        elif frames >= cfg.min_new_speaker_frames:
            if room and self._candidate is not None and self._candidate.distance(vp) < distance:
                c = self._candidate.copy()
                c.add(vp)
                if all(p.distance(c) > cfg.confirmed_distance for _, p in self._speakers):
                    self._candidate = None
                    return self._add(c)
            self._candidate = vp
            self._candidate_end = end
        return self._speakers[nearest][0]

    def _add(self, vp: Voiceprint) -> SpeakerID:
        sid = SpeakerID(f"S{len(self._speakers) + 1}")
        p = vp.copy()
        p.cap(self.config.cluster_frames)
        self._speakers.append((sid, p))
        return sid
