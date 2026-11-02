"""Port of the synthetic-voice cases in apple/Tests/A2AModelsTests/SpeakerTrackingTests.swift."""

from __future__ import annotations

import math

import numpy as np

from a2a_runtime.core.types import AudioFrame16k, SourceSpan, SpeakerAssign, SpeakerID
from a2a_runtime.media.voiceprint import Voiceprint, VoiceprintAnalyzer
from a2a_runtime.models.fixtures import EnergyVAD
from a2a_runtime.models.speaker_tracking import VoiceprintSpeakerTracker

VOWELS = [(730, 1090, 2440), (270, 2290, 3010), (300, 870, 2240), (530, 1840, 2480), (570, 840, 2410)]


def voice(f0: float, scale: float, syllables: int, offset: int = 0, pitch_drift: float = 0.0) -> np.ndarray:
    """Vowel-like speech: a glottal pulse train with spectral tilt through three formants."""
    rate = 16_000.0
    out = []
    phase = 0.0
    for s in range(syllables):
        v = VOWELS[(s * 3 + offset) % len(VOWELS)]
        n = int(0.2 * rate)
        f = f0 * (1 + pitch_drift * math.sin(s + offset))
        x = np.zeros(n)
        lp1 = lp2 = 0.0
        for i in range(n):
            phase += f / rate
            pulse = 0.0
            if phase >= 1:
                phase -= 1
                pulse = 1.0
            lp1 = 0.9 * lp1 + pulse
            lp2 = 0.9 * lp2 + lp1
            x[i] = lp2
        for fc, bw in ((v[0] * scale, 80.0), (v[1] * scale, 100.0), (v[2] * scale, 120.0)):
            r = math.exp(-math.pi * bw / rate)
            theta = 2 * math.pi * fc / rate
            a1, a2 = 2 * r * math.cos(theta), -r * r
            y1 = y2 = 0.0
            for i in range(n):
                y = (1 - r) * x[i] + a1 * y1 + a2 * y2
                y2, y1 = y1, y
                x[i] = y
        ramp = 0.03 * rate
        x *= np.minimum(1.0, np.minimum(np.arange(n), n - 1 - np.arange(n)) / ramp)
        out.append(x.astype(np.float32))
    audio = np.concatenate(out)
    rms = float(np.sqrt(np.mean(audio * audio)))
    return (audio * (0.1 / rms)).astype(np.float32)


GAP = np.zeros(9_600, np.float32)


def track(audio: np.ndarray, tracker: VoiceprintSpeakerTracker) -> list[tuple[SpeakerID, SourceSpan]]:
    vad = EnergyVAD()
    labels = []
    for start in range(0, len(audio), 320):
        samples = audio[start:start + 320]
        frame = AudioFrame16k(SourceSpan(start * 3, (start + len(samples)) * 3), samples)
        for e in tracker.process(frame, vad.process(frame)):
            if isinstance(e, SpeakerAssign):
                labels.append((e.speaker, e.span))
    return labels


def dialogue(a, b, turns=8) -> np.ndarray:
    parts = []
    for turn in range(turns):
        f0, scale = b if turn % 2 == 1 else a
        parts += [voice(f0, scale, 8, offset=turn), GAP]
    return np.concatenate(parts)


def test_analyzer_finds_the_pitch_of_voiced_speech():
    a = VoiceprintAnalyzer()
    vp = Voiceprint()
    a.process(voice(140, 1, 8), vp)
    assert vp.voiced_frames > 100
    assert abs(vp.median_pitch_hz - 140) / 140 < 0.04
    silent = Voiceprint()
    VoiceprintAnalyzer().process(np.zeros(16_000, np.float32), silent)
    assert silent.voiced_frames == 0


def test_a_second_voice_is_confirmed_by_its_second_turn():
    tracker = VoiceprintSpeakerTracker()
    labels = track(dialogue((115, 1), (215, 1.18)), tracker)
    assert [s.raw_value for s, _ in labels] == ["S1", "S1", "S1", "S2", "S1", "S2", "S1", "S2"]
    assert tracker.speaker_count == 2
    assert abs(tracker.pitch_hz(SpeakerID("S1")) - 115) < 8
    assert abs(tracker.pitch_hz(SpeakerID("S2")) - 215) < 12


def test_two_odd_segments_far_apart_do_not_make_a_speaker():
    tracker = VoiceprintSpeakerTracker()
    parts = []
    for turn in range(11):
        odd = turn in (1, 10)
        parts += [voice(215, 1.18, 8, offset=turn) if odd else voice(115, 1, 8, offset=turn), GAP]
    labels = track(np.concatenate(parts), tracker)
    assert len(labels) == 11 and {s for s, _ in labels} == {SpeakerID("S1")}
