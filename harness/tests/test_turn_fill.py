"""Turn fill: a take shorter than the speech it translates is slowed to end with the actor.

dub-anything re-times every line so it ends where the actor stops, and its listeners put that
rhythm above the acting itself (docs/research.md findings 7-8). A2A has only ever sped speech up,
so a short English line left the rest of the actor's turn silent. Slowing is offered only by a
host that plays each phrase in its own slot (`--presentation-delay-ms`, ADR-0012/0013), where a
line that ends with its actor cannot delay the line after it.

Covers the pace choice, the worker protocol flag, and the emitted duration. No model, MLX, or
audio device: the backend is the stub used by `test_tts_speech_budget.py`.
"""
from __future__ import annotations

import array
import math
import sys

import pytest

from a2a_harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "research"))
import audio_pacing  # noqa: E402
from tts_worker import RATE, Voice, Worker  # noqa: E402


class Channel:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.chunks: list[bytes] = []

    def send(self, header: dict, payload: bytes = b"") -> None:
        self.events.append(header)
        if header["ev"] == "chunk":
            self.chunks.append(payload)

    def done(self) -> dict:
        return next(e for e in self.events if e["ev"] == "done")


class Backend:
    """One streaming voice: a fixed seconds-per-character, emitted in 0.32 s chunks."""

    name = "stub"
    streaming = True

    def __init__(self, seconds_per_character: float) -> None:
        self.seconds_per_character = seconds_per_character

    def load(self) -> None:
        pass

    def prepare(self, voice: Voice) -> None:
        voice.native = "stub-reference"

    def generate(self, voice, text, interval, attempt=0, speed=1.0):
        total = int(RATE * len(text) * self.seconds_per_character)
        step = int(RATE * interval)
        for start in range(0, total, step):
            end = min(total, start + step)
            # A 120 Hz harmonic tone: voiced everywhere, so nothing is trimmed as quiet.
            yield array.array("f", (0.15 * math.sin(i / 13) * (1 + 0.2 * math.sin(i / 900))
                                    for i in range(start, end)))


def synthesize(header: dict, seconds_per_character: float = 0.06) -> dict:
    if not audio_pacing.available:
        pytest.skip("audio pacing dependencies are not installed")
    channel = Channel()
    worker = Worker(Backend(seconds_per_character), channel, 0.32, False)
    voice = Voice(array.array("f", (0.1 * math.sin(i / 40) for i in range(RATE))), "", "xvector", "english")
    worker.voices["v1"] = voice
    worker.synthesize(header)
    return channel.done() | {"chunks": len(channel.chunks)}


# MARK: The choice


def test_without_fill_a_short_take_is_left_alone():
    # The live host plays as soon as audio is ready: there a short line is saved latency.
    assert audio_pacing.choose_speed("x" * 20, 3.0, 0.075) == 1.0
    assert audio_pacing.choose_speed("x" * 20, 3.0, 0.075, 1.0) == 1.0
    assert audio_pacing.choose_speed("x" * 20, 3.0, 0.075, 1.0, allow_fill=False) == 1.0


def test_fill_slows_a_short_take_toward_the_actor_and_stops_at_the_floor():
    # Proportional while the deficit is smaller than what the floor would over-correct.
    assert audio_pacing.choose_speed("x" * 20, 3.0, 0.075, 2.6, allow_fill=True) == pytest.approx(2.6 / 3.0)
    assert audio_pacing.choose_speed("x" * 20, 2.0, 0.075, 1.0, allow_fill=True) == audio_pacing.MIN_SPEED
    # A deficit smaller than the guard does not restyle the cadence.
    assert audio_pacing.choose_speed("x" * 20, 2.0, 0.075, 1.8, allow_fill=True) == 1.0
    assert audio_pacing.choose_speed("x" * 20, 2.0, 0.075, 1.95, allow_fill=True) == 1.0


def test_fill_never_outruns_the_actor_and_never_replaces_speed_up():
    for target in (0.5, 1.0, 4.0):
        for estimated in (0.1, 0.5, 2.0, 30.0):
            speed = audio_pacing.choose_speed("x" * 40, target, 0.075, estimated, allow_fill=True)
            assert audio_pacing.MIN_SPEED <= speed <= audio_pacing.MAX_SPEED
            if estimated > target:
                assert speed == audio_pacing.choose_speed("x" * 40, target, 0.075, estimated)


def test_fill_ignores_a_target_that_means_nothing():
    for bad in (None, float("nan"), float("inf"), -1, 0, "2"):
        assert audio_pacing.choose_speed("x" * 20, bad, 0.075, 1.0, allow_fill=True) == 1.0


# MARK: The pacer stretched the other way


def voice_tone(f0: float = 150.0, seconds: float = 1.5):
    import numpy as np
    t = np.arange(int(RATE * seconds)) / RATE
    return sum((0.3 / k) * np.sin(2 * np.pi * f0 * k * t) for k in range(1, 8)).astype(np.float32)


@pytest.mark.parametrize("speed", [0.75, 0.8, 0.85, 0.95])
def test_the_pacer_stretches_by_the_asked_amount_and_streams_like_a_whole_buffer(speed):
    import numpy as np
    x = voice_tone(150)

    def run(chunk: int):
        pacer = audio_pacing.StreamingWSOLA(speed, RATE)
        parts = [pacer.process(x[i:i + chunk]) for i in range(0, len(x), chunk)]
        return np.concatenate(parts + [pacer.process([], final=True)])

    streamed, whole = run(7_680), run(len(x))
    assert len(streamed) == round(len(x) / speed)
    assert np.array_equal(streamed, whole)
    # A stretched overlap that loses phase shows up as level dips and lost energy.
    assert np.sqrt((streamed[-2_400:] ** 2).mean()) > 0.5 * np.sqrt((x[-2_400:] ** 2).mean())
    assert np.sum(streamed ** 2) > 0.9 * np.sum(x ** 2)


@pytest.mark.parametrize("f0", [65, 100, 220])
def test_stretching_keeps_low_and_high_voices_in_phase(f0):
    import numpy as np
    x = voice_tone(f0)
    pacer = audio_pacing.StreamingWSOLA(0.85, RATE)
    out = np.concatenate([pacer.process(x), pacer.process([], final=True)])
    win = int(round(3 * RATE / f0))
    env = np.sqrt(np.convolve(out * out, np.ones(win) / win, mode="valid"))[2_400:-2_400]
    ref = np.sqrt(np.convolve(x * x, np.ones(win) / win, mode="valid")).mean()
    assert env.min() > 0.97 * ref and env.max() < 1.03 * ref


def test_the_pacer_refuses_a_speed_it_cannot_compute():
    for bad in (0.5, 0.0, -1, 1.7, float("nan")):
        with pytest.raises(ValueError):
            audio_pacing.StreamingWSOLA(bad, RATE)


# MARK: The worker honours the flag


def test_a_filled_take_is_longer_than_the_take_the_model_gave():
    plain = synthesize({"id": 1, "voice": "v1", "text": "This is one line."})
    paced = synthesize({"id": 1, "voice": "v1", "text": "This is one line.", "target_s": 2.0})
    filled = synthesize({"id": 1, "voice": "v1", "text": "This is one line.", "target_s": 2.0, "fill": True})
    assert plain["pace"] == 1.0 and paced["pace"] == 1.0
    assert filled["pace"] == audio_pacing.MIN_SPEED
    # The take lasts as much longer as it was slowed, give or take the tail one hop trims.
    assert filled["audio_s"] == pytest.approx(plain["audio_s"] / audio_pacing.MIN_SPEED, abs=0.05)
    assert filled["native_s"] == pytest.approx(plain["audio_s"], abs=0.05)


def test_a_take_that_already_fits_is_untouched_by_fill():
    native = synthesize({"id": 1, "voice": "v1", "text": "Short."})
    fits = synthesize({"id": 1, "voice": "v1", "text": "Short.", "target_s": native["audio_s"] + 0.1,
                       "fill": True})
    assert fits["pace"] == 1.0
    assert fits["audio_s"] == pytest.approx(native["audio_s"], abs=0.02)


def test_a_long_take_under_fill_still_speeds_up():
    long_line = "This line runs well past the second of speech it translates."
    paced = synthesize({"id": 1, "voice": "v1", "text": long_line, "target_s": 0.5})
    filled = synthesize({"id": 1, "voice": "v1", "text": long_line, "target_s": 0.5, "fill": True})
    assert paced["pace"] == audio_pacing.MAX_SPEED == filled["pace"]
    assert filled["audio_s"] < paced["native_s"]
