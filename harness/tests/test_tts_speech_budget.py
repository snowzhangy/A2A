"""Character-count audio budget in the clone worker (`research/tts_worker.py`, `--tts clone`).

A line whose speech drags past its budget doubles the queue behind it, and the session catches
up by skipping other lines: measured on a live session, about 14 lines came out at roughly 2
characters per second instead of 4-5, and one took 10 s for 3 s of source
(reports/week2/live-session-recovery.md). The worker now bounds emitted audio by the character
count and reports the cut instead of hiding it. No model, MLX, or audio device involved.
"""
from __future__ import annotations

import array
import math
import sys
from types import SimpleNamespace

import numpy as np

import pytest

from a2a_harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "research"))
from tts_worker import (MIN_CHARS_PER_SECOND, RATE, TOKENS_PER_SECOND, Voice, Worker,  # noqa: E402
                        max_tokens_for, speech_budget_seconds)


def test_budget_leaves_room_for_the_pace_the_model_reaches():
    # Measured brisk pace on this model: 4-5 Han characters per second, 13-15 English ones.
    for language, brisk in (("chinese", 4.0), ("english", 13.0)):
        for characters in (2, 12, 21, 60, 120):
            text = ("字" if language == "chinese" else "x") * characters
            natural = characters / brisk
            assert speech_budget_seconds(text, language) > natural + 0.5, text
    # The observed drag of about 2 characters per second no longer runs to its full length:
    # the 21-character line that took 10 s of audio is capped below 7.5 s.
    assert speech_budget_seconds("字" * 21, "chinese") < 7.5


def test_token_cap_follows_the_budget_and_stays_bounded():
    text = "字" * 21
    assert max_tokens_for(text, "chinese") == math.ceil(speech_budget_seconds(text, "chinese") * TOKENS_PER_SECOND)
    # A two-character acknowledgement still gets a whole short utterance, and nothing runs long.
    assert max_tokens_for("OK", "english") >= 25
    assert max_tokens_for("word " * 500, "english") == 500


class Channel:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, header: dict, payload: bytes = b"") -> None:
        self.events.append(header)

    def done(self) -> dict:
        return next(e for e in self.events if e["ev"] == "done")


class Backend:
    """Yields a fixed speech rate in seconds per character, as one streaming chunk per 0.5 s."""

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
        step = RATE // 2
        for start in range(0, total, step):
            end = min(total, start + step)
            yield array.array("f", (0.1 * math.sin(i / 10) for i in range(start, end)))


def synthesize(seconds_per_character: float, text: str, language: str) -> dict:
    channel = Channel()
    worker = Worker(Backend(seconds_per_character), channel, 0.5, False)
    voice = Voice(array.array("f", (0.1 * math.sin(i / 40) for i in range(RATE))), "", "xvector", language)
    worker.voices["v1"] = voice
    worker.synthesize({"id": 1, "voice": "v1", "text": text})
    return channel.done()


def test_a_line_that_reaches_the_budget_is_reported_as_truncated():
    text = "一二三四"   # budget 2.0 s; this backend keeps generating for 4 s
    done = synthesize(1.0, text, "chinese")
    assert done["truncated"] is True
    assert done["budget_s"] == pytest.approx(speech_budget_seconds(text, "chinese"))
    assert done["generated_s"] >= done["budget_s"] - 0.05
    assert 0 < done["audio_s"] <= done["generated_s"]


def test_the_budget_reaches_the_model_as_its_token_cap():
    # Enforcement is the model's: the worker hands it the cap, and a run-away generation
    # (missed EOS) stops there rather than streaming ten seconds onto the bus.
    from tts_worker import ModelBackend

    class Adapter:
        streaming = True

        def __init__(self) -> None:
            self.caps = []

        def generate(self, voice, text, interval, max_tokens, temperature):
            self.caps.append(max_tokens)
            yield SimpleNamespace(audio=np.zeros(RATE // 2, dtype=np.float32), sample_rate=RATE)

    backend = ModelBackend("qwen", None)
    backend.mx = SimpleNamespace(random=SimpleNamespace(seed=lambda seed: None), eval=lambda *args: None)
    backend.np = np
    backend.adapter = Adapter()
    backend.streaming = True
    reference = Voice(array.array("f", (0.1 * math.sin(i / 40) for i in range(RATE))), "", "xvector", "chinese")
    text = "字" * 21
    list(backend.generate(reference, text, 0.32))
    assert backend.adapter.caps == [max_tokens_for(text, "chinese")]
    # 7.3 s of audio at most: the 21-character line cannot run to the 10 s it took.
    assert backend.adapter.caps[0] / TOKENS_PER_SECOND < 7.5


def test_a_line_at_the_normal_pace_is_never_cut():
    done = synthesize(0.22, "一二三四五六七八九十", "chinese")   # 4.5 characters per second
    assert done["truncated"] is False
    assert done["audio_s"] > 0
    assert done["generated_s"] < done["budget_s"]


class SilentStartBackend:
    """Near-silence until the token cap for the first `silent_takes` takes, then speech.

    The failure seen live on short Mandarin lines: no voiced frame, generation runs to the cap,
    and the host receives no audio."""
    name = "silent-start"
    streaming = True

    def __init__(self, silent_takes: int) -> None:
        self.silent_takes = silent_takes
        self.calls: list[tuple[str, int]] = []
        self.generated: list[int] = []

    def load(self) -> None:
        pass

    def prepare(self, voice: Voice) -> None:
        voice.native = "stub-reference"

    def generate(self, voice, text, interval, attempt=0, speed=1.0):
        self.calls.append((text, attempt))
        self.generated.append(0)
        silent = len(self.calls) <= self.silent_takes
        step = int(RATE * interval)
        total = int(RATE * speech_budget_seconds(text, voice.language)) if silent else RATE
        for start in range(0, total, step):
            n = min(step, total - start)
            self.generated[-1] += n
            level = 1e-5 if silent else 0.1
            yield array.array("f", (level * math.sin(i / 10) for i in range(start, start + n)))


def synthesize_with(backend, text: str, language: str = "chinese") -> dict:
    channel = Channel()
    worker = Worker(backend, channel, 0.32, False)
    voice = Voice(array.array("f", (0.1 * math.sin(i / 40) for i in range(RATE))), "", "xvector", language)
    worker.voices["v1"] = voice
    worker.synthesize({"id": 1, "voice": "v1", "text": text})
    return channel.done()


def test_a_silent_take_stops_early_and_is_generated_again():
    from tts_worker import SILENT_TAKE_SECONDS

    backend = SilentStartBackend(silent_takes=1)
    done = synthesize_with(backend, "好的我知道")
    # The failed start costs about a second of generation, not the whole budget.
    assert backend.generated[0] < RATE * (SILENT_TAKE_SECONDS + 0.33)
    assert backend.generated[0] < RATE * speech_budget_seconds("好的我知道", "chinese")
    # The retry samples with another seed and ends the fragment as an utterance.
    assert backend.calls == [("好的我知道", 0), ("好的我知道。", 1)]
    assert done["retries"] == 1
    assert done["audio_s"] > 0.5
    assert done["truncated"] is False


def test_a_line_that_stays_silent_is_still_reported_once_retried():
    text = "好的"
    backend = SilentStartBackend(silent_takes=2)
    done = synthesize_with(backend, text)
    assert [attempt for _, attempt in backend.calls] == [0, 1]
    assert done["retries"] == 1
    # The last take keeps the old contract: it runs to the budget and reports no audio.
    assert done["audio_s"] == 0
    assert done["truncated"] is True


def test_a_take_with_speech_is_never_retried():
    backend = SilentStartBackend(silent_takes=0)
    done = synthesize_with(backend, "你好")
    assert backend.calls == [("你好", 0)]
    assert done["retries"] == 0


def test_retry_seed_is_deterministic_and_differs_per_attempt():
    from tts_worker import retry_text, take_seed

    assert take_seed(1234, 0) == 1234
    assert take_seed(1234, 1) != 1234
    assert take_seed(1234, 1) == take_seed(1234, 1)
    assert 0 <= take_seed(0xFFFFFFFF, 3) <= 0xFFFFFFFF
    # Punctuation already present is left alone; it is never spoken either way.
    assert retry_text("好的。", "chinese") == "好的。"
    assert retry_text("okay", "english") == "okay."


def test_a_silent_take_does_not_teach_the_voice_a_slower_pace():
    import audio_pacing

    if not audio_pacing.available:
        pytest.skip("audio pacing dependencies are not installed")
    channel = Channel()
    worker = Worker(SilentStartBackend(silent_takes=2), channel, 0.32, False)
    voice = Voice(array.array("f", (0.1 * math.sin(i / 40) for i in range(RATE))), "", "xvector", "chinese")
    worker.voices["v1"] = voice
    before = voice.seconds_per_character
    worker.synthesize({"id": 1, "voice": "v1", "text": "好的", "target_s": 0.6})
    assert channel.done()["audio_s"] == 0
    # Cap-length silence would otherwise read as 0.7 s per character and speed up later lines.
    assert voice.seconds_per_character == before
