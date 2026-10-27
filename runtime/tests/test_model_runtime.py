"""Opt-in installed-model smoke tests; no downloads, capture, or quality claims."""

import os
import sys
import time

import numpy as np
import pytest

from a2a_runtime.core.direction import TranslationDirection, TranslationLanguage
from a2a_runtime.core.types import AudioFrame16k, SourceSpan, SpeechEnd, SpeechStart, TranslationRequest, UnitID
from a2a_runtime.workers.speech import LLMTranslator, WhisperRecognizer
from a2a_runtime import REPO_ROOT
from a2a_runtime.workers.clone import CloneVoiceSynthesizer

pytestmark = pytest.mark.skipif(os.environ.get("A2A_RUN_MODEL_TESTS") != "1", reason="installed model test is opt-in")


def test_whisper_worker_loads_and_decodes():
    worker = WhisperRecognizer("faster-whisper-large-v3", TranslationLanguage.MANDARIN, device="auto")
    try:
        worker.on_vad_event(SpeechStart(0))
        worker.accept(AudioFrame16k(SourceSpan(0, 48_000), np.zeros(16_000, np.float32)))
        worker.on_vad_event(SpeechEnd(48_000))
        worker.request_finalize()
        deadline = time.monotonic() + 120
        while worker.pending and time.monotonic() < deadline:
            worker.drain_hypotheses()
            time.sleep(.05)
        assert not worker.failure, worker.failure
        assert not worker.pending, "recognition did not finish"
    finally:
        worker.shutdown()


def test_translation_worker_loads_and_translates():
    worker = LLMTranslator("qwen3-4b-instruct-2507", TranslationDirection.CHINESE_TO_ENGLISH, device="auto",
                           research_text=True)
    try:
        result = worker.translate(TranslationRequest(UnitID(1), (), (), "我们下周开会。"))
        assert result.complete and result.text
    finally:
        worker.shutdown()


def test_omnivoice_worker_loads():
    worker = CloneVoiceSynthesizer([sys.executable, str(REPO_ROOT / "research" / "tts_worker.py"),
                                    "--model", "omnivoice", "--backend", "torch", "--device", "auto"])
    try:
        assert worker.info and worker.info.model and not worker.failure
    finally:
        worker.shutdown()
