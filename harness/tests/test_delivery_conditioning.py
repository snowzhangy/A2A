"""Per-line delivery conditioning: a cloned line may be spoken on a reference that ends with
the actor's own delivery of that line (ADR-0015).

dub-anything's largest acting lever is that every line's reference includes the actor's original
of that line, so a shout stays a shout (docs/research.md findings 6 and 17-20, kept for
Mandarin → English). A2A pins one profile per speaker for the whole session (ADR-0009), which is
the right voice delivered flat. These tests cover the worker half of the change: what may be
appended, what is refused and why, and what the pinned profile keeps. No model and no MLX — the
backend is the stub used by `test_tts_speech_budget.py`. The Swift half (who is allowed to ask)
is `DeliveryConditioningTests`, and the audible question is still a listener's.
"""
from __future__ import annotations

import array
import math
import sys

import pytest

from a2a_harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "research"))
import tts_worker as W  # noqa: E402
from tts_worker import RATE, Voice, Worker  # noqa: E402


def tone(seconds: float, level: float = 0.2, f0: float = 120.0) -> array.array:
    return array.array("f", (level * math.sin(2 * math.pi * f0 * i / RATE)
                             for i in range(int(RATE * seconds))))


def concat(*parts: array.array) -> array.array:
    out = array.array("f")
    for p in parts:
        out.extend(p)
    return out


class Channel:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def send(self, header: dict, payload: bytes = b"") -> None:
        self.events.append(header)

    def done(self) -> dict:
        # The most recent verdict, so one test can ask twice (refused, then accepted).
        return next(e for e in reversed(self.events) if e["ev"] == "done")


def rms(x) -> float:
    return math.sqrt(sum(v * v for v in x) / max(1, len(x)))


class Backend:
    """Records every reference it is asked to prepare, so the appended clip is visible."""

    name = "stub"
    streaming = True
    native_speed = False

    def __init__(self) -> None:
        self.prepared: list[tuple[int, str]] = []

    def load(self) -> None:
        pass

    def prepare(self, voice: Voice) -> None:
        self.prepared.append((len(voice.pcm), voice.text, voice.pcm))
        voice.native = "stub-reference"

    def generate(self, voice, text, interval, attempt=0, speed=1.0):
        step = int(RATE * interval)
        total = int(RATE * 0.8)
        for start in range(0, total, step):
            yield tone(step / RATE, level=0.1)


def worker_with(reference: array.array, mode: str = "xvector", text: str = "",
                max_delivery_seconds: float = 4.0) -> tuple[Worker, Channel, Backend]:
    backend = Backend()
    channel = Channel()
    worker = Worker(backend, channel, 0.32, True, max_delivery_seconds)
    pinned = Voice(reference, text, mode)
    backend.prepare(pinned)          # what the host's `prepare` op does once per speaker
    worker.voices["v1"] = pinned
    return worker, channel, backend


def synth(worker: Worker, channel: Channel, delivery: array.array | None = None, **extra) -> dict:
    header = {"id": 1, "voice": "v1", "text": "We will discuss the plan."}
    payload = b""
    if delivery is not None:
        header["delivery"] = True
        payload = delivery.tobytes()
    header.update(extra)
    worker.synthesize(header, payload)
    return channel.done()


def test_the_lines_own_audio_is_appended_after_the_pinned_reference():
    pinned = tone(6.0)
    worker, channel, backend = worker_with(pinned)
    done = synth(worker, channel, delivery=tone(2.0, level=0.3, f0=200.0))
    assert done["delivery"] == "line"
    assert done["delivery_s"] == pytest.approx(2.0, abs=0.01)
    assert len(backend.prepared) == 2, "the variant is a second prepared reference"
    total, _, _ = backend.prepared[1]
    assert total == len(pinned) + int(RATE * 2.0)
    assert done["delivery_prep_s"] >= 0


def test_a_request_without_delivery_prepares_nothing_extra():
    worker, channel, backend = worker_with(tone(6.0))
    done = synth(worker, channel)
    assert done["delivery"] == "absent" and done["delivery_s"] == 0.0
    assert len(backend.prepared) == 1


def test_a_long_line_contributes_its_loudest_seconds_only():
    # A 30 s turn where the actor only really performs the middle: the clip must be that part,
    # not the first two seconds of throat-clearing.
    quiet, loud = tone(3.0, level=0.004), tone(2.0, level=0.3, f0=180.0)
    worker, channel, backend = worker_with(tone(4.0), max_delivery_seconds=2.0)
    done = synth(worker, channel, delivery=concat(quiet, tone(20.0, level=0.004), loud, quiet))
    assert done["delivery"] == "line"
    assert done["delivery_s"] == pytest.approx(2.0, abs=0.02)
    total, _, variant_pcm = backend.prepared[1]
    assert total == int(RATE * 4.0) + int(RATE * 2.0)
    appended = variant_pcm[int(RATE * 4.0):]
    # The performed seconds went in, not the quiet ones around them.
    assert abs(rms(appended) - rms(loud)) < 0.02, (rms(appended), rms(loud))


def test_a_delivery_of_quiet_room_tone_is_refused_rather_than_used():
    worker, channel, backend = worker_with(tone(5.0))
    done = synth(worker, channel, delivery=tone(2.0, level=0.0))
    assert done["delivery"] == "too_quiet"
    assert len(backend.prepared) == 1, "the line is spoken on the pinned profile"


def test_in_context_conditioning_needs_the_words_that_belong_to_the_audio():
    worker, channel, backend = worker_with(tone(5.0), mode="icl", text="我们下周开会")
    refused = synth(worker, channel, delivery=tone(1.5, level=0.3))
    assert refused["delivery"] == "needs_source_text"
    accepted = synth(worker, channel, delivery=tone(1.5, level=0.3), delivery_text="好的")
    assert accepted["delivery"] == "line"
    total, text, _ = backend.prepared[-1]
    assert (total, text) == (int(RATE * 5.0) + int(RATE * 1.5), "我们下周开会 好的")


def test_a_reference_at_the_models_bound_cannot_grow():
    worker, channel, backend = worker_with(tone(W.MAX_REFERENCE_SECONDS - 0.05))
    done = synth(worker, channel, delivery=tone(1.0, level=0.3))
    assert done["delivery"] == "reference_bound"
    assert len(backend.prepared) == 1


def test_the_pinned_profile_keeps_what_the_conditioned_take_taught_it():
    worker, channel, backend = worker_with(tone(6.0))
    pinned = worker.voices["v1"]
    before = pinned.seconds_per_character, pinned.gain
    synth(worker, channel, delivery=tone(2.0, level=0.3))
    assert list(worker.voices) == ["v1"], "the one-unit variant is not registered as a speaker"
    assert (pinned.seconds_per_character, pinned.gain) != before


def test_a_backend_that_cannot_prepare_a_variant_falls_back_and_says_so():
    """A refused conditioning attempt is a verdict, not a lost line: the pinned profile speaks it."""
    worker, channel, backend = worker_with(tone(6.0))
    real = backend.prepare

    def only_once(voice):
        if len(backend.prepared):     # the pinned reference is already in; a variant may not go in
            raise RuntimeError("codec cache full")
        real(voice)

    backend.prepare = only_once
    done = synth(worker, channel, delivery=tone(1.0, level=0.3))
    assert done["delivery"] == "failed"
    assert done["audio_s"] > 0, "the unit is still spoken and still played out"


def test_loudest_window_picks_the_loud_part_and_leaves_short_audio_alone():
    short = tone(1.0, level=0.2)
    assert W.loudest_window(short, 3.0) is short
    loud = tone(1.0, level=0.4, f0=170.0)
    picked = W.loudest_window(concat(tone(2.0, level=0.004), loud, tone(2.0, level=0.004)), 1.0)
    assert len(picked) == int(RATE * 1.0)
    assert W.voiced_seconds(picked) > 0.9
    assert W.voiced_seconds(tone(1.0, level=0.0)) == 0.0
