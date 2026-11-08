"""Language identification per closed unit using the already verified faster-whisper artifact."""

from __future__ import annotations

import queue
import threading

import numpy as np

from ..core.direction import TranslationLanguage
from ..media.resampler import StreamingResampler
from ..models.base import AdapterError
from .model_paths import installed_model
from .protocol import WorkerProcess


class WhisperLanguageIdentifier:
    name = "whisper-lid"

    def __init__(self, target_language: TranslationLanguage, model="faster-whisper-large-v3", device="auto") -> None:
        import sys

        self.target = "en" if target_language is TranslationLanguage.ENGLISH else "zh"
        self._failure = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._next = 0
        self._pending = {}
        self._events: queue.Queue = queue.Queue()
        path = installed_model(model, "asr")
        argv = [sys.executable, "-m", "a2a_runtime.workers.speech_worker", "asr",
                "--path", str(path), "--device", device, "--source", "zh", "--target", "en"]
        self._worker = WorkerProcess(argv, self._on_event, self._on_exit)
        if not self._ready.wait(600):
            self.shutdown()
            raise AdapterError("language identifier did not become ready")
        if self._failure:
            self.shutdown()
            raise AdapterError(self._failure)

    @property
    def failure(self):
        return self._failure

    def describe(self):
        return "faster-whisper language identification (different backend from Mac ECAPA)"

    def identify(self, span, samples) -> None:
        pcm = StreamingResampler.resample(samples, 48_000, 16_000).astype("<f4")
        with self._lock:
            if len(self._pending) >= 32:
                raise AdapterError("language identification queue is full")
            self._next += 1
            self._pending[self._next] = span
            ticket = self._next
        self._worker.send({"op": "lid", "id": ticket}, pcm.tobytes())

    def drain(self) -> list:
        out = []
        while True:
            try:
                span, language = self._events.get_nowait()
            except queue.Empty:
                return out
            out.append((span, language == self.target))

    def _on_event(self, event, _payload):
        kind = event.get("ev")
        if kind == "ready":
            self._ready.set()
        elif kind == "lid":
            with self._lock:
                span = self._pending.pop(int(event["id"]), None)
            if span is not None:
                self._events.put((span, str(event.get("language", ""))))
        elif kind == "error":
            self._fail(str(event.get("message", "language identification failed")))

    def _on_exit(self, message):
        self._fail(message)

    def _fail(self, message):
        self._failure = message
        self._ready.set()

    def shutdown(self):
        self._worker.shutdown()
