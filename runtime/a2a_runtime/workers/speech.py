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


def derived_translation(model_id: str, quantization: str | None = None, doc: dict | None = None):
    """The verified CTranslate2 conversion of `model_id` (with `quantization`, the one stored
    that way), or None when it was never converted (the worker then decodes with PyTorch, or
    quantizes at load, and says so). A stale or modified conversion is an error: it must be
    rebuilt rather than silently used."""
    from ..models import derive

    spec = derive.for_source(model_id, "ctranslate2", doc, quantization)
    if spec is None:
        return None
    try:
        return derive.check_stamp(spec, doc)
    except FileNotFoundError:
        return None
    except ValueError as error:
        raise AdapterError(str(error)) from None


def derived_translations(model_id: str):
    """(bfloat16 conversion, stored-int8 conversion or None). The int8 one is an optional
    load-time shortcut, so it is only offered next to the bfloat16 one."""
    from ..models import derive

    doc = derive._manifest()
    primary = derived_translation(model_id, None, doc)
    return primary, derived_translation(model_id, "int8_bfloat16", doc) if primary is not None else None


class _Worker:
    def __init__(self, stage, model, device, source, target, ready_timeout=600, research_text=False, stop_event=None,
                 reserve_bytes=0, load_gate=None, sized_event=None):
        """`load_gate` (MT): the worker starts now but sizes itself against free GPU memory
        only once the gate is set; `sized_event` is set when it has (see cli.load_stages)."""
        path = installed_model(model, stage)
        self._ready = threading.Event()
        self._sized_event = sized_event
        self._lock = threading.RLock()
        self._failure = None
        self.device = None
        self.note = None
        argv = [sys.executable, "-m", "a2a_runtime.workers.speech_worker", stage,
                                      "--path", str(path), "--device", device,
                                      "--source", source, "--target", target]
        if research_text:
            argv.append("--debug-errors")
        if reserve_bytes > 0:
            argv += ["--reserve-mb", str(reserve_bytes // 1024**2)]
        if load_gate is not None:
            argv.append("--wait-load")
        if stage == "mt":
            derived, derived_int8 = derived_translations(model)
            if derived is not None:
                argv += ["--derived", str(derived)]
            if derived_int8 is not None:
                argv += ["--derived-int8", str(derived_int8)]
        self._worker = WorkerProcess(argv, self._event, self._exit)
        deadline = time.monotonic() + ready_timeout
        while not self._ready.wait(min(0.1, max(0, deadline - time.monotonic()))):
            if load_gate is not None and load_gate.is_set():
                self._worker.send({"op": "load"})
                load_gate = None
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
        if event.get("ev") == "sized":
            if self._sized_event is not None:
                self._sized_event.set()
        elif event.get("ev") == "ready":
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
    # Continuous speech (or speech over music that keeps VAD open) is split into utterances of
    # at most this length at the quietest point of their last SPLIT_SEARCH samples.
    MAX_SAMPLES = 20 * 16_000
    SPLIT_SEARCH = 3 * 16_000
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
        self.dropped_finals = 0
        self.forced_splits = 0
        super().__init__("asr", model, device, language.whisper_code, language.whisper_code, research_text=research_text,
                         stop_event=stop_event)
        # Re-decode the growing utterance twice a second on a GPU; LocalAgreement-2 commits a
        # key once two consecutive hypotheses agree on it, so cadence sets commit latency.
        self.partial_samples = 8_000 if (self.device or "").startswith("cuda") else self.PARTIAL_SAMPLES

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
            if self._speech and self._samples > self.MAX_SAMPLES:
                self._split_long_utterance()
            elif not self._speech:
                while self._samples > 2 * 16_000 and self._chunks:
                    self._samples -= len(self._chunks.popleft())
            if self._speech:
                self._since_partial += len(audio)
                if self._since_partial >= self.partial_samples:
                    self._since_partial = 0
                    self._partial = self._snapshot(False)
                    self._dispatch()

    def _snapshot(self, final):
        samples = np.concatenate(tuple(self._chunks)) if self._chunks else np.empty(0, dtype="<f4")
        start = max(0, self._frontier - len(samples) * 3)
        self._next_id += 1
        return {"id": self._next_id, "utterance": self._utterance, "frontier": self._frontier,
                "start": start, "final": final, "audio": samples.tobytes()}

    def _split_long_utterance(self):
        """Finalize everything before the quietest 100 ms of the last three seconds and keep
        the rest as the start of the next utterance, so no word is cut or repeated."""
        samples = np.concatenate(tuple(self._chunks))
        tail = samples[-self.SPLIT_SEARCH:]
        hop = 1_600
        energies = [float(np.mean(tail[i:i + hop] ** 2)) for i in range(0, len(tail) - hop + 1, hop // 2)]
        cut = len(samples) - len(tail) + (int(np.argmin(energies)) * (hop // 2) + hop // 2 if energies else 0)
        head, rest = samples[:cut], samples[cut:]
        frontier = self._frontier
        self._chunks = collections.deque([head])
        self._samples = len(head)
        self._frontier = frontier - len(rest) * 3
        self._queue_final()
        self._frontier = frontier
        self._chunks = collections.deque([rest])
        self._samples = len(rest)
        self._has_speech = True
        self.forced_splits += 1

    def _queue_final(self):
        if len(self._finals) >= self.MAX_FINALS:
            # Falling behind loses the oldest queued utterance (reported), not the session.
            self._finals.popleft()
            self.dropped_finals += 1
        self._finals.append(self._snapshot(True))
        self._utterance += 1
        self._chunks.clear()
        self._samples = self._since_partial = 0
        self._has_speech = False
        self._partial = None
        self._dispatch()

    def request_finalize(self):
        with self._lock:
            if not self._samples or not self._has_speech:
                return
            self._queue_final()

    def _dispatch(self):
        if self._active or self._failure:
            return
        job = self._finals.popleft() if self._finals else self._partial
        if job is None:
            return
        if job is self._partial:
            self._partial = None
        self._active = job
        self._worker.send({"op": "transcribe", "id": job["id"], "final": job["final"]}, job["audio"])

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

    def __init__(self, model, direction, device="auto", research_text=False, stop_event=None, reserve_bytes=0,
                 load_gate=None, sized_event=None):
        # One waiter per request: the driver may abandon a call (a cancelled unit) and start
        # the next while the worker is still generating, so replies are matched by id and a
        # reply nobody waits for any more is dropped rather than treated as a protocol error.
        self._waiters: dict[int, list] = {}
        self._unit_requests: dict = {}
        self._request_id = 0
        super().__init__("mt", model, device, direction.source.display_name, direction.target.display_name,
                         research_text=research_text, stop_event=stop_event, reserve_bytes=reserve_bytes,
                         load_gate=load_gate, sized_event=sized_event)

    def _wake(self):
        for waiter in list(self._waiters.values()):
            waiter[0].set()

    def _result(self, event):
        with self._lock:
            waiter = self._waiters.get(event.get("id"))
        if waiter is not None:
            waiter[1] = event
            waiter[0].set()

    def cancel(self, request_id: int) -> None:
        """Tell the worker to skip a request it has not started (a cancelled unit)."""
        with self._lock:
            waiter = self._waiters.pop(request_id, None)
        if waiter is not None:
            self._worker.send({"op": "cancel", "id": request_id})
            waiter[0].set()

    def cancel_unit(self, unit) -> None:
        with self._lock:
            request_id = self._unit_requests.pop(unit, None)
        if request_id is not None:
            self.cancel(request_id)

    def translate(self, request, timeout=120):
        with self._lock:
            if self._failure:
                raise AdapterError(self._failure)
            if request.committed_target_prefix:
                raise AdapterError("translation worker does not support target prefix continuation")
            self._request_id += 1
            request_id = self._request_id
            waiter = [threading.Event(), None]
            self._waiters[request_id] = waiter
            self._unit_requests[request.unit_id] = request_id
            self._worker.send({"op": "translate", "id": request_id, "text": request.uncommitted_source,
                               "context": [(p.source, p.target) for p in request.context],
                               "glossary": list(request.glossary)})
        try:
            if not waiter[0].wait(timeout):
                self._exit("translation worker timed out")
        finally:
            with self._lock:
                self._waiters.pop(request_id, None)
                if self._unit_requests.get(request.unit_id) == request_id:
                    del self._unit_requests[request.unit_id]
        if self.failure:
            raise AdapterError(self.failure)
        result = waiter[1]
        if result is None:
            raise AdapterError("translation canceled")
        if result.get("ev") == "error":
            raise AdapterError("translation worker failed: " + str(result.get("message", "error")))
        return TranslationCandidate(request.unit_id, result.get("text", ""), result.get("complete", False))
