"""research/audio_pacing.py: the clone worker's streaming time-stretch and pause compaction.

Synthetic harmonic signals only; no model, device, or file access.
"""
from __future__ import annotations

import math
import sys

import numpy as np
import pytest

from a2a_harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "research"))
import audio_pacing  # noqa: E402

RATE = 24_000


def voice(f0: float, seconds: float = 2.0) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return sum((0.3 / k) * np.sin(2 * np.pi * f0 * k * t) for k in range(1, 8)).astype(np.float32)


def stretch(x: np.ndarray, speed: float, chunk: int = 7_680) -> np.ndarray:
    w = audio_pacing.StreamingWSOLA(speed, RATE)
    parts = [w.process(x[i:i + chunk]) for i in range(0, len(x), chunk)]
    return np.concatenate(parts + [w.process([], final=True)])


def envelope(x: np.ndarray, f0: float) -> np.ndarray:
    win = int(round(3 * RATE / f0))  # three pitch periods
    return np.sqrt(np.convolve(x * x, np.ones(win) / win, mode="valid"))[2_400:-2_400]


def test_unit_speed_is_identity():
    x = voice(120)
    w = audio_pacing.StreamingWSOLA(1.0, RATE)
    assert np.array_equal(np.concatenate([w.process(x[:1000]), w.process(x[1000:])]), x)


@pytest.mark.parametrize("speed", [1.1, 1.3, 1.6])
def test_output_duration_follows_speed_and_streaming_matches_whole_buffer(speed):
    x = voice(150)
    streamed = stretch(x, speed)
    assert len(streamed) == round(len(x) / speed)
    whole = stretch(x, speed, chunk=len(x))
    assert np.array_equal(streamed, whole)


@pytest.mark.parametrize("f0", [65, 70, 100, 180, 220])
def test_overlaps_stay_in_phase_for_low_and_high_voices(f0):
    # Out-of-phase crossfades show up as level dips; a ±5 ms search lost ~13% at 65 Hz.
    x = voice(f0)
    out = envelope(stretch(x, 1.3), f0)
    ref = envelope(x, f0).mean()
    assert out.min() > 0.97 * ref and out.max() < 1.03 * ref


def test_choose_speed_is_bounded_and_ignores_bad_targets():
    assert audio_pacing.choose_speed("x" * 200, 1.0, 0.075) == audio_pacing.MAX_SPEED
    assert audio_pacing.choose_speed("Okay.", 0.4, 0.075) == 1.0
    for bad in (None, float("nan"), float("inf"), -1, 0, "2"):
        assert audio_pacing.choose_speed("Some longer sentence here.", bad, 0.075) == 1.0
    assert 1.0 < audio_pacing.choose_speed("x" * 60, 3.5, 0.075) < audio_pacing.MAX_SPEED


def test_reference_duration_controls_pacing_when_available():
    text = "A short line."
    assert audio_pacing.choose_speed(text, 2.0, 0.075, 3.0) == audio_pacing.MAX_SPEED
    assert audio_pacing.choose_speed(text, 2.0, 0.075, 1.0) == 1.0
    for bad in (None, float("nan"), float("inf"), -1, 0):
        assert audio_pacing.choose_speed(text, 2.0, 0.075, bad) == audio_pacing.choose_speed(text, 2.0, 0.075)


def test_quiet_run_compactor_shortens_only_long_pauses():
    c = audio_pacing.QuietRunCompactor(RATE, max_pause=0.18)
    tone, short, long = voice(150, 0.2), np.zeros(RATE // 10, np.float32), np.zeros(RATE, np.float32)
    x = np.concatenate([tone, short, tone, long, tone])
    out = np.concatenate([c.process(x[i:i + 5_000]) for i in range(0, len(x), 5_000)] + [c.process([], final=True)])
    assert c.removed == len(long) - round(0.18 * RATE)
    assert len(out) == len(x) - c.removed
    assert math.isclose(float(np.abs(out).max()), float(np.abs(x).max()))
