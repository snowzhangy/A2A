"""Portable host side of the shared per-line delivery worker protocol."""

import threading

import numpy as np
import pytest

from a2a_runtime.cli import parse
from a2a_runtime.core.types import PreparedVoice, SpeakerID
from a2a_runtime.models.base import DeliveryClip
from a2a_runtime.workers.clone import CloneVoiceSynthesizer


class Sender:
    def __init__(self):
        self.calls = []

    def send(self, header, payload=b""):
        self.calls.append((header, payload))
        return True


def worker():
    instance = CloneVoiceSynthesizer.__new__(CloneVoiceSynthesizer)
    instance._failure = None
    instance._lock = threading.Lock()
    instance._next_id = 0
    instance._streams = {}
    instance._units = {}
    instance._stats = {}
    instance._worker = Sender()
    return instance


def test_delivery_clip_is_resampled_and_sent_as_the_request_payload():
    instance = worker()
    voice = PreparedVoice(SpeakerID("S1"), 1, 1)
    source = np.linspace(-0.5, 0.5, 480, dtype=np.float32)
    stream = instance.synthesize(1, "Hello", voice, delivery=DeliveryClip(source, 48_000, "源文本"))
    header, payload = instance._worker.calls[0]
    assert header["delivery"] is True and header["delivery_text"] == "源文本"
    pcm = np.frombuffer(payload, dtype="<f4")
    assert len(pcm) == 240 and np.isfinite(pcm).all()
    instance._on_event({"ev": "done", "id": stream.id, "delivery": "line",
                        "delivery_s": 0.01, "delivery_prep_s": 0.002}, b"")
    stats = instance.take_stats(1)
    assert stats.delivery == "line" and stats.delivery_seconds == 0.01 and stats.delivery_prep_seconds == 0.002


def test_delivery_requires_a_clone_adapter():
    with pytest.raises(SystemExit):
        parse(["--input", "x.wav", "--clone-delivery", "line"])
    options = parse(["--input", "x.wav", "--clock", "host", "--asr", "whisper", "--mt", "llm",
                     "--tts", "clone", "--clone-fake", "--clone-delivery", "line"])
    assert options.clone_delivery == "line"
