"""Exercise worker scheduling, finalization, and framed transport without model weights."""

import io
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from a2a_runtime.core.direction import TranslationDirection, TranslationLanguage
from a2a_runtime.core.types import AudioFrame16k, SourceSpan, SpeechEnd, SpeechStart, TranslationRequest, UnitID
from a2a_runtime.models.base import AdapterError
from a2a_runtime.workers import protocol, speech
from a2a_runtime.workers.speech_worker import asr_auto_device, mt_auto_cuda


class FakeWorker:
    def __init__(self, argv, on_event, on_exit):
        self.on_event = on_event
        self.sent = []
        on_event({"ev": "ready", "device": "fake"}, b"")

    def send(self, header, payload=b""):
        self.sent.append((header, payload))
        return True

    def shutdown(self):
        pass

    def reply(self, **event):
        self.on_event(event, b"")


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(speech, "installed_model", lambda *args: "local-model")
    monkeypatch.setattr(speech, "WorkerProcess", FakeWorker)


def test_asr_final_is_queued_after_inflight_partial_and_preserves_source_time(fake):
    recognizer = speech.WhisperRecognizer("asr", TranslationLanguage.MANDARIN)
    recognizer.on_vad_event(SpeechStart(48_000))
    recognizer.accept(AudioFrame16k(SourceSpan(48_000, 96_000), np.zeros(16_000, np.float32)))
    partial_id = recognizer._worker.sent[0][0]["id"]
    recognizer.on_vad_event(SpeechEnd(96_000))
    recognizer.request_finalize()
    assert recognizer.pending
    recognizer._worker.reply(ev="result", id=partial_id, words=[{"text": "你好", "start": .2, "end": .5}])
    final_id = recognizer._worker.sent[1][0]["id"]
    recognizer._worker.reply(ev="result", id=final_id, words=[{"text": "你好", "start": .2, "end": .5}])
    partial, final = recognizer.drain_hypotheses()
    assert partial.utterance == final.utterance == 1
    assert not partial.is_final and final.is_final
    assert final.tokens[0].span == SourceSpan(57_600, 72_000)
    assert final.audio_frontier == 96_000
    assert not recognizer.pending


def test_asr_reports_queue_overflow_instead_of_losing_a_final(fake):
    recognizer = speech.WhisperRecognizer("asr", TranslationLanguage.MANDARIN)
    for i in range(6):
        recognizer.on_vad_event(SpeechStart(i * 48_000))
        recognizer.accept(AudioFrame16k(SourceSpan(i * 48_000, (i + 1) * 48_000), np.zeros(16_000, np.float32)))
        recognizer.on_vad_event(SpeechEnd((i + 1) * 48_000))
        recognizer.request_finalize()
    assert "queue is full" in recognizer.failure


def test_short_utterance_finalizes_without_a_prior_partial(fake):
    recognizer = speech.WhisperRecognizer("asr", TranslationLanguage.MANDARIN)
    recognizer.on_vad_event(SpeechStart(0))
    recognizer.accept(AudioFrame16k(SourceSpan(0, 12_000), np.zeros(4_000, np.float32)))
    assert not recognizer._worker.sent
    recognizer.on_vad_event(SpeechEnd(12_000))
    assert recognizer.pending and len(recognizer._worker.sent) == 1
    job_id = recognizer._worker.sent[0][0]["id"]
    recognizer._worker.reply(ev="result", id=job_id, words=[{"text": "好", "start": 0, "end": .2}])
    assert recognizer.drain_hypotheses()[0].is_final


def test_translation_worker_returns_completion_and_rejects_prefix(fake):
    translator = speech.LLMTranslator("mt", TranslationDirection.CHINESE_TO_ENGLISH)
    request = TranslationRequest(UnitID(1), (), (), "你好")
    result = []
    t = threading.Thread(target=lambda: result.append(translator.translate(request, timeout=3)))
    t.start()
    for _ in range(1000):
        if translator._worker.sent:
            break
        threading.Event().wait(.001)
    job_id = translator._worker.sent[0][0]["id"]
    translator._worker.reply(ev="result", id=job_id, text="Hello", complete=True)
    t.join(3)
    assert result[0].text == "Hello" and result[0].complete
    with pytest.raises(AdapterError, match="prefix"):
        translator.translate(TranslationRequest(UnitID(2), (), (), "你好", "Hi"))


def test_transport_handles_short_writes_and_rejects_oversize():
    class ShortWriter(io.BytesIO):
        def write(self, data):
            return super().write(data[:3])

    target = ShortWriter()
    encoded = protocol.encode({"op": "x"}, b"abc")
    protocol._write_all(target, encoded)
    assert protocol.read_frame(io.BytesIO(target.getvalue())) == ({"op": "x"}, b"abc")
    with pytest.raises(ValueError, match="limits"):
        protocol.encode({"x": "a" * protocol.MAX_HEADER})


def test_recognition_auto_respects_free_gpu_memory():
    model = 3 * 1024**3
    assert asr_auto_device(True, 8 * 1024**3, model) == ("cuda", None)
    device, reason = asr_auto_device(True, 4 * 1024**3, model)
    assert device == "cpu" and "insufficient free GPU memory" in reason
    assert asr_auto_device(False, None, model) == ("cpu", None)
    assert asr_auto_device(True, None, model) == ("cuda", None)


def test_translation_auto_respects_free_gpu_memory():
    model = 8 * 1024**3
    assert mt_auto_cuda(12 * 1024**3, model) == ("cuda", None)
    device, reason = mt_auto_cuda(9 * 1024**3, model)
    assert device == "cpu" and "insufficient free GPU memory" in reason
    assert mt_auto_cuda(None, model) == ("cuda", None)


def test_cli_stage_device_overrides():
    from a2a_runtime.cli import clone_worker_command, parse

    options = parse(["--input", "clip.wav", "--asr-script", "script.json", "--device", "cuda", "--asr-device", "cpu"])
    assert (options.asr_device, options.mt_device, options.tts_device) == ("cpu", "cuda", "cuda")
    options = parse(["--input", "clip.wav", "--clock", "host", "--asr-script", "script.json", "--tts", "clone",
                     "--clone-fake", "--device", "cuda", "--tts-device", "cpu"])
    command = clone_worker_command(options)
    assert command[command.index("--device") + 1] == "cpu"


def test_tail_limit_fails_when_recognition_has_not_finished():
    from a2a_runtime.driver import Driver

    driver = Driver.__new__(Driver)
    driver.recognizer = SimpleNamespace(pending=True)
    driver.runtime_failed = False
    driver.check_recognition_tail(99, 100)
    assert not driver.runtime_failed
    driver.check_recognition_tail(100, 100)
    assert driver.runtime_failed
