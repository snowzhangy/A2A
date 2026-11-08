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


def test_translation_tokenizer_without_torch_matches_auto_tokenizer():
    # The worker loads PreTrainedTokenizerFast so the CTranslate2 path never imports torch;
    # its chat-template ids and decodes must be AutoTokenizer's.
    import warnings

    with warnings.catch_warnings():
        # The third-party `regex` import calls locale.getpreferredencoding(); the workers run
        # this in a subprocess, where it is not an error.
        warnings.simplefilter("ignore", EncodingWarning)
        from transformers import AutoTokenizer, PreTrainedTokenizerFast

    from a2a_runtime.workers.model_paths import installed_model

    path = installed_model("qwen3-4b-instruct-2507", "mt")
    auto = AutoTokenizer.from_pretrained(path, local_files_only=True)
    fast = PreTrainedTokenizerFast.from_pretrained(path, local_files_only=True)
    conversations = [
        [{"role": "user", "content": "我们下周开会。"}],
        [{"role": "system", "content": "You are a simultaneous interpreter."},
         {"role": "user", "content": "预算需要增加百分之二十。"},
         {"role": "assistant", "content": "The budget needs to increase by 20%."},
         {"role": "system", "content": "Terminology: 张伟 = Zhang Wei"},
         {"role": "user", "content": "请在星期五之前把报告发给我，好吗？<|im_end|> 😀\tend\n"}],
    ]

    def ids(tokenizer, messages):
        out = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
        return list(out["input_ids"] if hasattr(out, "keys") else out)

    for messages in conversations:
        expected = ids(auto, messages)
        assert ids(fast, messages) == expected
        assert fast.convert_ids_to_tokens(expected) == auto.convert_ids_to_tokens(expected)
        assert fast.decode(expected, skip_special_tokens=True) == auto.decode(expected, skip_special_tokens=True)


def test_language_identifier_loads_and_returns_a_verdict():
    from a2a_runtime.workers.lid import WhisperLanguageIdentifier

    worker = WhisperLanguageIdentifier(TranslationLanguage.ENGLISH, device="auto")
    try:
        worker.identify(SourceSpan(0, 48_000), np.zeros(48_000, dtype=np.float32))
        deadline = time.monotonic() + 120
        verdicts = []
        while not verdicts and time.monotonic() < deadline:
            verdicts = worker.drain()
            time.sleep(0.05)
        assert verdicts and isinstance(verdicts[0][1], bool)
        assert not worker.failure
    finally:
        worker.shutdown()


def test_omnivoice_worker_loads():
    worker = CloneVoiceSynthesizer([sys.executable, str(REPO_ROOT / "research" / "tts_worker.py"),
                                    "--model", "omnivoice", "--backend", "torch", "--device", "auto"])
    try:
        assert worker.info and worker.info.model and not worker.failure
    finally:
        worker.shutdown()
