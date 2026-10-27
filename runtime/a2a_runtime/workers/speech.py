"""Resident, offline speech workers for the portable pipeline."""

from __future__ import annotations

import collections
import sys
import threading
import time

import numpy as np

from ..core.types import ASRHypothesis, ASRToken, SourceSpan, SpeechEnd, SpeechStart, TranslationCandidate
from ..models.base import AdapterError
from .model_paths import installed_model
from .protocol import WorkerProcess


class _Worker:
    def __init__(self, stage, model, device, source, target, ready_timeout=600, research_text=False, stop_event=None):
        path = installed_model(model, stage)
        self._ready = threading.Event()
        self._lock = threading.RLock()
        self._failure = None
        self.device = None
        self.note = None
        argv = [sys.executable, "-m", "a2a_runtime.workers.speech_worker", stage,
                                      "--path", str(path), "--device", device,
                                      "--source", source, "--target", target]
        if research_text:
            argv.append("--debug-errors")
        self._worker = WorkerProcess(argv, self._event, self._exit)
        deadline = time.monotonic() + ready_timeout
        while not self._ready.wait(min(0.1, max(0, deadline - time.monotonic()))):
            if stop_event is not None and stop_event.is_set():
                self._exit("worker startup canceled")
                break
            if time.monotonic() >= deadline:
                self._exit("worker startup timed out")
                break
        if self.failure:
            self.shutdown()
            raise AdapterError(self.failure)

    @property
    def failure(self):
        with self._lock:
            return self._failure

    def describe(self):
        return (self.device or "unknown device") + (f" ({self.note})" if self.note else "")

    def _event(self, event, payload):
        if event.get("ev") == "ready":
            self.device = event.get("device")
            self.note = event.get("note")
            self._ready.set()
        elif event.get("ev") == "error" and event.get("id") is None:
            self._exit(event.get("message", "worker failed"))
        else:
            self._result(event)

    def _exit(self, message):
        with self._lock:
            if self._failure is None:
                self._failure = message
            self._ready.set()
            self._wake()

    def _wake(self):
        pass

    def shutdown(self):
        self._worker.shutdown()


class WhisperRecognizer(_Worker):
    name = "whisper"
    # One active request and at most four completed utterances awaiting inference.
    MAX_FINALS = 4
    MAX_HYPOTHESES = 128
    MAX_SAMPLES = 30 * 16_000
    PARTIAL_SAMPLES = 16_000

    def __init__(self, model, language, device="auto", research_text=False, fluent=False, stop_event=None):
        self._chunks = collections.deque()
        self._samples = 0
        self._frontier = 0
        self._utterance = 1
        self._speech = False
        self._has_speech = False
        self._next_id = 0
        self._active = None
        self._finals = collections.deque()
        self._partial = None
        self._hypotheses = collections.deque()
        self._since_partial = 0
        super().__init__("asr", model, device, language.whisper_code, language.whisper_code, research_text=research_text,
                         stop_event=stop_event)

    @property
    def pending(self):
        with self._lock:
            return bool(self._active or self._finals or self._hypotheses)

    def on_vad_event(self, event):
        if isinstance(event, SpeechStart):
            self._speech = True
            self._has_speech = True
        elif isinstance(event, SpeechEnd):
            self._speech = False
            # A short utterance can end before the first partial has returned. The
            # reducer cannot request finalization until it has seen a hypothesis.
            self.request_finalize()

    def accept(self, frame):
        with self._lock:
            audio = np.asarray(frame.samples, dtype="<f4").copy()
            self._chunks.append(audio)
            self._samples += len(audio)
            self._frontier = frame.span.end
            # Retain up to two seconds of silence before the next speech onset.
            limit = self.MAX_SAMPLES if self._speech else 2 * 16_000
            if self._speech and self._samples > self.MAX_SAMPLES:
                self._exit("speech utterance exceeds the 30-second recognition limit")
                return
            while self._samples > limit and self._chunks:
                self._samples -= len(self._chunks.popleft())
            if self._speech:
                self._since_partial += len(audio)
                if self._since_partial >= self.PARTIAL_SAMPLES:
                    self._since_partial = 0
                    self._partial = self._snapshot(False)
                    self._dispatch()

    def _snapshot(self, final):
        samples = np.concatenate(tuple(self._chunks)) if self._chunks else np.empty(0, dtype="<f4")
        start = max(0, self._frontier - len(samples) * 3)
        self._next_id += 1
        return {"id": self._next_id, "utterance": self._utterance, "frontier": self._frontier,
                "start": start, "final": final, "audio": samples.tobytes()}

    def request_finalize(self):
        with self._lock:
            if not self._samples or not self._has_speech:
                return
            if len(self._finals) >= self.MAX_FINALS:
                self._exit("recognition fell behind: final queue is full")
                return
            self._finals.append(self._snapshot(True))
            self._utterance += 1
            self._chunks.clear()
            self._samples = self._since_partial = 0
            self._has_speech = False
            self._partial = None
            self._dispatch()

    def _dispatch(self):
        if self._active or self._failure:
            return
        job = self._finals.popleft() if self._finals else self._partial
        if job is None:
            return
        if job is self._partial:
            self._partial = None
        self._active = job
        self._worker.send({"op": "transcribe", "id": job["id"]}, job["audio"])

    def _result(self, event):
        with self._lock:
            job = self._active
            if not job or event.get("id") != job["id"]:
                self._exit("recognition worker returned an unexpected response")
                return
            self._active = None
            if event.get("ev") == "error":
                self._exit("recognition worker failed: " + str(event.get("message", "error")))
                return
            tokens = []
            for word in event.get("words", []):
                start = min(job["frontier"], max(job["start"], job["start"] + int(float(word["start"]) * 48_000)))
                end = min(job["frontier"], max(start, job["start"] + int(float(word["end"]) * 48_000)))
                tokens.append(ASRToken(str(word["text"]), SourceSpan(start, end)))
            if tokens:
                if len(self._hypotheses) >= self.MAX_HYPOTHESES:
                    self._exit("recognition consumer fell behind")
                    return
                self._hypotheses.append(ASRHypothesis(job["utterance"], tuple(tokens), job["final"], job["frontier"]))
            self._dispatch()

    def drain_hypotheses(self):
        with self._lock:
            out = list(self._hypotheses)
            self._hypotheses.clear()
            return out

    def reset(self):
        # The driver tears down on a discontinuity; stale replies cannot be used in a new epoch.
        with self._lock:
            if self._active:
                self._exit("recognizer reset while inference was active")
            self._chunks.clear()
            self._finals.clear()
            self._partial = None
            self._hypotheses.clear()
            self._samples = self._since_partial = 0
            self._has_speech = self._speech = False


class LLMTranslator(_Worker):
    name = "llm"
    supports_prefix = False
    supports_context = True

    def __init__(self, model, direction, device="auto", research_text=False, stop_event=None):
        self._response = threading.Event()
        self._result_value = None
        self._request_id = 0
        super().__init__("mt", model, device, direction.source.display_name, direction.target.display_name,
                         research_text=research_text, stop_event=stop_event)

    def _wake(self):
        self._response.set()

    def _result(self, event):
        with self._lock:
            if event.get("id") != self._request_id:
                self._exit("translation worker returned an unexpected response")
            else:
                self._result_value = event
                self._response.set()

    def translate(self, request, timeout=120):
        with self._lock:
            if self._failure:
                raise AdapterError(self._failure)
            if request.committed_target_prefix:
                raise AdapterError("translation worker does not support target prefix continuation")
            self._request_id += 1
            self._result_value = None
            self._response.clear()
            self._worker.send({"op": "translate", "id": self._request_id, "text": request.uncommitted_source,
                               "context": [(p.source, p.target) for p in request.context], "glossary": list(request.glossary)})
        if not self._response.wait(timeout):
            self._exit("translation worker timed out")
        if self.failure:
            raise AdapterError(self.failure)
        result = self._result_value
        if result.get("ev") == "error":
            raise AdapterError("translation worker failed: " + str(result.get("message", "error")))
        return TranslationCandidate(request.unit_id, result.get("text", ""), result.get("complete", False))
