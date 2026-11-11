"""Character-count audio budget in the clone worker (`research/tts_worker.py`, `--tts clone`).

A line whose speech drags past its budget doubles the queue behind it, and the session catches
up by skipping other lines: measured on a live session, about 14 lines came out at roughly 2
characters per second instead of 4-5, and one took 10 s for 3 s of source
(reports/week2/live-session-recovery.md). The worker now bounds emitted audio by the character
count and reports the cut instead of hiding it. The budget runs from the take's first voiced
frame: the silence Qwen generates before it speaks is not speech and must not cost a line its
last words (reports/week2/qwen-take-budget.md). No model, MLX, or audio device involved.
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
from tts_worker import (LEAD_ALLOWANCE_SECONDS, MIN_CHARS_PER_SECOND, PACE_MARGIN_SECONDS, RATE,  # noqa: E402
                        TOKENS_PER_SECOND, Voice, Worker, max_tokens_for, speech_budget_seconds)


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
    assert max_tokens_for(text, "chinese", 1.5) > max_tokens_for(text, "chinese")
    assert max_tokens_for("字" * 500, "chinese", 1.5) == 500


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
    # The model is handed a cap, so a run-away generation (missed EOS) stops there rather than
    # streaming ten seconds onto the bus. A streaming model's cap also covers the silence before
    # its speech, because the worker ends the take once the speech has used the budget.
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
    assert backend.adapter.caps == [max_tokens_for(text, "chinese", lead_seconds=LEAD_ALLOWANCE_SECONDS)]
    # 7.3 s of speech and its leading silence at most: the line cannot run to the 10 s it took.
    assert backend.adapter.caps[0] / TOKENS_PER_SECOND < 7.5 + LEAD_ALLOWANCE_SECONDS
    backend.speech_budget_multiplier = 1.5
    list(backend.generate(reference, text, 0.32))
    assert backend.adapter.caps[-1] == max_tokens_for(text, "chinese", 1.5, LEAD_ALLOWANCE_SECONDS)
    # A whole-phrase model cannot be stopped mid-take, so its cap stays the budget itself.
    backend.speech_budget_multiplier = 1.0
    backend.streaming = False
    list(backend.generate(reference, text, 0.32))
    assert backend.adapter.caps[-1] == max_tokens_for(text, "chinese")
    assert backend.adapter.caps[-1] / TOKENS_PER_SECOND < 7.5


def test_a_line_at_the_normal_pace_is_never_cut():
    done = synthesize(0.22, "一二三四五六七八九十", "chinese")   # 4.5 characters per second
    assert done["truncated"] is False
    assert done["audio_s"] > 0
    assert done["generated_s"] < done["budget_s"]


def test_longer_budget_is_reported_consistently_by_the_worker():
    backend = Backend(0.22)
    backend.speech_budget_multiplier = 1.5
    channel = Channel()
    worker = Worker(backend, channel, 0.5, False)
    worker.voices["v1"] = Voice(array.array("f", [0.1] * RATE), "", "xvector", "chinese")
    text = "一二三四五六七八九十"
    worker.synthesize({"id": 1, "voice": "v1", "text": text})
    assert channel.done()["budget_s"] == pytest.approx(1.5 * speech_budget_seconds(text, "chinese"))


class ShapedBackend:
    """One take: `lead` seconds of silence, `speech` seconds of voice, `tail` seconds of silence."""
    name = "shaped"

    def __init__(self, lead: float, speech: float, tail: float = 0.0, streaming: bool = True) -> None:
        self.lead, self.speech, self.tail = int(RATE * lead), int(RATE * speech), int(RATE * tail)
        self.streaming = streaming
        self.generated = 0

    def load(self) -> None:
        pass

    def prepare(self, voice: Voice) -> None:
        voice.native = "stub-reference"

    def generate(self, voice, text, interval, attempt=0, speed=1.0):
        step = int(RATE * interval)
        total = self.lead + self.speech + self.tail
        for start in range(0, total, step):
            n = min(step, total - start)
            self.generated += n
            yield array.array("f", (0.1 * math.sin(i / 10) if self.lead <= i < self.lead + self.speech else 1e-5
                                    for i in range(start, start + n)))


def shaped_take(backend: ShapedBackend, text: str, **request) -> tuple[dict, Voice]:
    channel = Channel()
    worker = Worker(backend, channel, 0.32, False)
    voice = Voice(array.array("f", (0.1 * math.sin(i / 40) for i in range(RATE))), "", "xvector", "chinese")
    worker.voices["v1"] = voice
    worker.synthesize({"id": 1, "voice": "v1", "text": text, **request})
    return channel.done(), voice


def test_leading_silence_does_not_spend_the_budget():
    # 196 English takes on four cloned voices started with 0.46 s of silence at the median and
    # up to 3.2 s. Counted against the budget, that silence stopped takes whose speech fit it.
    text = "一二三四"   # budget 2.0 s
    budget = speech_budget_seconds(text, "chinese")
    done, _ = shaped_take(ShapedBackend(lead=0.8, speech=budget - 0.1), text)
    assert done["generated_s"] > budget          # the old rule cut this take
    assert done["truncated"] is False
    assert done["lead_s"] == pytest.approx(0.8, abs=0.05)
    assert done["audio_s"] == pytest.approx(budget - 0.1, abs=0.1)


def test_speech_that_runs_on_stops_at_the_budget_after_its_first_voiced_frame():
    text = "一二三四"
    budget = speech_budget_seconds(text, "chinese")
    backend = ShapedBackend(lead=0.8, speech=10.0)
    done, _ = shaped_take(backend, text)
    assert done["truncated"] is True
    # The worker ends the take itself: one streaming chunk past the budget at most.
    assert budget <= done["generated_s"] - done["lead_s"] < budget + 0.33
    assert backend.generated < RATE * (0.8 + budget + 0.33)
    assert done["audio_s"] <= budget + 0.33


def test_a_take_stopped_by_the_token_cap_before_its_budget_is_still_truncated(monkeypatch):
    # Silence longer than the lead allowance: the model's cap ends the take while it is still
    # speaking and before the speech has used its budget. That is a cut line, and its length
    # is the cap's, so it must not teach the voice a pace either.
    import audio_pacing

    text = "一二三四五六七八九十"
    budget = speech_budget_seconds(text, "chinese")
    lead = LEAD_ALLOWANCE_SECONDS + 0.5
    cap = max_tokens_for(text, "chinese", lead_seconds=LEAD_ALLOWANCE_SECONDS) / TOKENS_PER_SECOND
    # The stub honors the cap the way the model does; the last take may start this late.
    backend = ShapedBackend(lead=lead, speech=cap - lead)
    channel = Channel()
    worker = Worker(backend, channel, 0.32, False)
    voice = Voice(array.array("f", (0.1 * math.sin(i / 40) for i in range(RATE))), "", "xvector", "chinese")
    worker.voices["v1"] = voice
    before = voice.seconds_per_character
    import tts_worker
    monkeypatch.setattr(tts_worker, "TAKE_ATTEMPTS", 1)
    worker.synthesize({"id": 1, "voice": "v1", "text": text, "target_s": 2.0})
    done = channel.done()
    assert done["generated_s"] - done["lead_s"] < budget - 0.3    # the speech budget was not reached
    assert done["truncated"] is True
    if audio_pacing.available:
        assert voice.seconds_per_character == before


def test_a_take_that_is_quiet_at_the_cap_lost_nothing():
    # The pause before a late EOS reached the cap on 6 of 196 takes whose words were all there.
    text = "一二三四"
    done, voice = shaped_take(ShapedBackend(lead=0.2, speech=1.0, tail=5.0), text)
    assert done["truncated"] is False
    assert done["audio_s"] == pytest.approx(1.0, abs=0.1)
    assert done["generated_s"] < speech_budget_seconds(text, "chinese") + 0.2 + 0.33


def test_a_whole_phrase_model_is_not_stopped_by_the_worker():
    # Its chunks are slices of a finished phrase: dropping the rest would cut audio the model
    # itself kept inside its own cap.
    text = "一二三四"
    backend = ShapedBackend(lead=0.0, speech=4.0, streaming=False)
    done, _ = shaped_take(backend, text)
    assert backend.generated == RATE * 4
    assert done["audio_s"] == pytest.approx(4.0, abs=0.1)
    assert done["truncated"] is True


def test_the_voice_learns_the_pace_of_its_speech_not_of_the_silence_around_it():
    # Learned from the raw take, the silence sped up 56 of 102 Mandarin takes to the limit while
    # 32 of them still ended more than 0.25 s before their source did.
    import audio_pacing

    if not audio_pacing.available:
        pytest.skip("audio pacing dependencies are not installed")
    text = "一二三四五六七八九十"
    done, voice = shaped_take(ShapedBackend(lead=0.6, speech=2.0, tail=0.5), text, target_s=2.0)
    assert done["truncated"] is False
    speech = done["native_s"]
    assert speech == pytest.approx(2.0, abs=0.2)     # pre-roll and post-roll only
    assert voice.seconds_per_character == pytest.approx(
        0.7 * 0.25 + 0.3 * (speech + PACE_MARGIN_SECONDS) / len(text), abs=1e-6)
    # The raw take is 3.1 s, which would have read as 0.31 s per character.
    assert voice.seconds_per_character < 0.25


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


@pytest.mark.parametrize("silent_chunks", [0, 1, 2])
def test_early_eos_without_speech_retries_before_losing_the_line(silent_chunks):
    class EarlyEOS(Backend):
        def __init__(self):
            super().__init__(0.1)
            self.calls = []

        def generate(self, voice, text, interval, attempt=0, speed=1.0):
            self.calls.append(attempt)
            if attempt == 0:
                for _ in range(silent_chunks):
                    yield array.array("f", [1e-5] * int(RATE * interval))
                return
            yield from super().generate(voice, text, interval, attempt, speed)

    backend = EarlyEOS()
    done = synthesize_with(backend, "好的我知道")
    assert backend.calls == [0, 1]
    assert done["retries"] == 1
    assert done["audio_s"] > 0


def test_an_empty_retry_is_bounded_and_reports_no_audio():
    class Empty(Backend):
        def generate(self, voice, text, interval, attempt=0, speed=1.0):
            yield from ()

    done = synthesize_with(Empty(0.1), "好的")
    assert done["retries"] == 1
    assert done["audio_s"] == 0
    assert done["truncated"] is False


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


def paced_voice_after(seconds_per_character: float, text: str) -> tuple[float, float, dict]:
    """The voice's pace estimate before and after one paced take at the given model speed."""
    channel = Channel()
    worker = Worker(Backend(seconds_per_character), channel, 0.5, False)
    voice = Voice(array.array("f", (0.1 * math.sin(i / 40) for i in range(RATE))), "", "xvector", "chinese")
    worker.voices["v1"] = voice
    before = voice.seconds_per_character
    worker.synthesize({"id": 1, "voice": "v1", "text": text, "target_s": 2.0})
    return before, voice.seconds_per_character, channel.done()


def test_a_take_the_cap_stopped_does_not_teach_the_voice_a_slower_pace():
    # Live en→zh with a reference cloned from music: most takes ran on until the cap, each one
    # taught the voice about 0.38 s per character, and every later line went out at the fastest
    # speed-up (ADR-0016). A run-on take's length is the cap's, not the voice's.
    import audio_pacing

    if not audio_pacing.available:
        pytest.skip("audio pacing dependencies are not installed")
    before, after, done = paced_voice_after(1.0, "我们今天要讨论这个计划")
    assert done["truncated"] is True
    assert after == before


def test_a_take_that_ends_by_itself_still_teaches_the_voice_its_pace():
    import audio_pacing

    if not audio_pacing.available:
        pytest.skip("audio pacing dependencies are not installed")
    before, after, done = paced_voice_after(0.18, "我们今天要讨论这个计划")
    assert done["truncated"] is False
    assert after < before
