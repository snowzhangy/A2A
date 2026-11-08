"""Hy-SimulMT direct-audio adapters: resident framed worker or explicit remote server.

The local adapter shares research/hy_worker's protocol 2. Streaming writes use a bounded
queue so a model turn cannot stall the capture driver. Audio stays in process memory; the
remote adapter is selected explicitly and sends each span to the configured server.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import urllib.request
import uuid
from dataclasses import dataclass

import numpy as np

from ..core.types import TranslationCandidate, TranslationWorkerStats, UnitID
from ..media.wav import WAVAudio, encode_float32
from ..models.base import AdapterError
from .protocol import WorkerProcess

INPUT_SAMPLE_RATE = 48_000


def request_timeout(samples: int) -> float:
    return max(4.0, 2.0 * samples / INPUT_SAMPLE_RATE)


@dataclass(frozen=True)
class HyWorkerInfo:
    model: str
    load_seconds: float
    warmup_seconds: float
    streams_spans: bool


@dataclass(frozen=True)
class HySpanTicket:
    id: int
    samples: int


class HyLocalTranslator:
    name = "hy-local"

    def __init__(self, argv: list[str], direction: str, ready_timeout: float = 120,
                 stop_event: threading.Event | None = None) -> None:
        if direction not in ("zh-en", "en-zh"):
            raise AdapterError("invalid Hy-SimulMT direction")
        self.direction = direction
        self.info: HyWorkerInfo | None = None
        self._failure: str | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._next_id = 0
        self._waiters: dict[int, list] = {}
        self._answers: dict[int, tuple] = {}
        self._outbox: queue.Queue = queue.Queue(maxsize=128)
        self._closed = False
        self._worker = WorkerProcess(argv, self._on_event, self._on_exit)
        self._writer = threading.Thread(target=self._write_loop, name="a2a.hy-writer", daemon=True)
        self._writer.start()
        deadline = time.monotonic() + ready_timeout
        while not self._ready.wait(min(0.1, max(0.0, deadline - time.monotonic()))):
            if stop_event is not None and stop_event.is_set():
                self._fail("Hy-SimulMT worker startup canceled")
                break
            if time.monotonic() >= deadline:
                self._fail(f"Hy-SimulMT worker did not become ready within {ready_timeout:.0f} s")
                break
        if self.info is None:
            self.shutdown()
            raise AdapterError(self._failure or "Hy-SimulMT worker did not start")

    @property
    def failure(self) -> str | None:
        return self._failure

    def describe(self) -> str:
        return "hy-local (local audio)"

    def _enqueue(self, header: dict, payload: bytes = b"") -> None:
        if self._closed or self._failure:
            raise AdapterError(self._failure or "Hy-SimulMT worker is closed")
        try:
            self._outbox.put_nowait((header, payload))
        except queue.Full:
            self._fail("Hy-SimulMT worker input queue overflow")
            raise AdapterError(self._failure) from None

    def _write_loop(self) -> None:
        while True:
            header, payload = self._outbox.get()
            if header.get("op") == "close-host":
                return
            if not self._worker.send(header, payload):
                self._fail("Hy-SimulMT worker input closed")
                return

    def stream(self, samples) -> None:
        pcm = np.asarray(samples, dtype="<f4")
        if len(pcm):
            self._enqueue({"op": "append", "id": 0, "direction": self.direction,
                           "rate": INPUT_SAMPLE_RATE}, pcm.tobytes())

    def close(self, remaining=(), span_samples: int = 0) -> HySpanTicket:
        if not 0 < span_samples <= INPUT_SAMPLE_RATE * 30:
            raise AdapterError("Hy-SimulMT segment is empty or exceeds 30 seconds")
        with self._lock:
            self._next_id += 1
            ticket = HySpanTicket(self._next_id, span_samples)
        pcm = np.asarray(remaining, dtype="<f4")
        if len(pcm):
            self._enqueue({"op": "append", "id": ticket.id, "direction": self.direction,
                           "rate": INPUT_SAMPLE_RATE}, pcm.tobytes())
        self._enqueue({"op": "flush", "id": ticket.id})
        return ticket

    def discard(self) -> None:
        self._enqueue({"op": "discard"})

    def reset(self) -> None:
        self._enqueue({"op": "reset"})

    def translate(self, unit: UnitID, samples) -> TranslationCandidate:
        pcm = np.asarray(samples, dtype=np.float32)
        return self.answer(self.close(pcm, len(pcm)), unit)

    def answer(self, ticket: HySpanTicket, unit: UnitID, timeout: float | None = None) -> TranslationCandidate:
        if not 0 < ticket.samples <= INPUT_SAMPLE_RATE * 30:
            raise AdapterError("Hy-SimulMT segment is empty or exceeds 30 seconds")
        waiter = [threading.Event(), None]
        with self._lock:
            answer = self._answers.pop(ticket.id, None)
            if answer is None and self._failure:
                raise AdapterError(self._failure)
            if answer is None:
                self._waiters[ticket.id] = waiter
        if answer is None:
            if not waiter[0].wait(timeout or request_timeout(ticket.samples)):
                with self._lock:
                    self._waiters.pop(ticket.id, None)
                raise AdapterError("Hy-SimulMT worker answer timed out")
            answer = waiter[1]
        if isinstance(answer, BaseException):
            raise AdapterError(str(answer))
        text, stats = answer
        return TranslationCandidate(unit, text, worker_stats=stats)

    def _deliver(self, ticket: int, answer) -> None:
        with self._lock:
            waiter = self._waiters.pop(ticket, None)
            if waiter is not None:
                waiter[1] = answer
                waiter[0].set()
            else:
                if len(self._answers) >= 32:
                    self._answers.pop(next(iter(self._answers)))
                self._answers[ticket] = answer

    def _on_event(self, event: dict, payload: bytes) -> None:
        kind = event.get("ev")
        if kind == "ready":
            self.info = HyWorkerInfo(str(event.get("model", "")), float(event.get("load_s", 0)),
                                     float(event.get("warmup_s", 0)), int(event.get("protocol", 1)) >= 2)
            self._ready.set()
        elif kind == "done" and event.get("id") is not None:
            stats = None
            if event.get("flush_ms") is not None:
                stats = TranslationWorkerStats(int(event["flush_ms"]), int(event.get("turns", 0)),
                                               bool(event.get("st", False)))
            self._deliver(int(event["id"]), (str(event.get("text", "")), stats))
        elif kind == "error":
            message = str(event.get("message", "Hy-SimulMT worker error"))
            if event.get("id") not in (None, 0):
                self._deliver(int(event["id"]), AdapterError(message))
            elif event.get("id") is None:
                self._fail(message)

    def _on_exit(self, message: str) -> None:
        if not self._closed:
            self._fail(message)

    def _fail(self, message: str) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = message
            waiters = list(self._waiters.values())
            self._waiters.clear()
            self._answers.clear()
        for waiter in waiters:
            waiter[1] = AdapterError(message)
            waiter[0].set()
        self._ready.set()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._outbox.put_nowait(({"op": "close-host"}, b""))
        except queue.Full:
            pass
        self._worker.shutdown()


class HySimulTranslator:
    name = "hy"

    def __init__(self, server: str, direction: str) -> None:
        if direction not in ("zh-en", "en-zh") or not server.startswith(("http://", "https://")):
            raise AdapterError("invalid Hy-SimulMT server URL or direction")
        self.endpoint = server.rstrip("/") + "/v1/audio/translations?stream=true"
        self.direction = direction

    def describe(self) -> str:
        return "hy server (source audio sent over network)"

    def translate(self, unit: UnitID, samples) -> TranslationCandidate:
        pcm = np.asarray(samples, dtype=np.float32)
        if not 0 < len(pcm) <= INPUT_SAMPLE_RATE * 30:
            raise AdapterError("Hy-SimulMT segment is empty or exceeds 30 seconds")
        boundary = "A2A-" + uuid.uuid4().hex
        crlf = chr(13) + chr(10)
        head = (f'--{boundary}{crlf}Content-Disposition: form-data; name="direction"{crlf * 2}'
                f'{self.direction}{crlf}--{boundary}{crlf}'
                'Content-Disposition: form-data; name="file"; filename="segment.wav"' + crlf
                + "Content-Type: audio/wav" + crlf * 2).encode("utf-8")
        body = (head + encode_float32(WAVAudio(INPUT_SAMPLE_RATE, 1, pcm))
                + f"{crlf}--{boundary}--{crlf}".encode("ascii"))
        request = urllib.request.Request(self.endpoint, data=body, method="POST",
                                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(request, timeout=request_timeout(len(pcm))) as response:
                data = response.read(1_048_577)
        except Exception as error:
            raise AdapterError(f"Hy-SimulMT server request failed: {error}") from None
        if len(data) > 1_048_576:
            raise AdapterError("Hy-SimulMT server returned an oversized response")
        try:
            return self.parse_events(data.decode("utf-8"), unit)
        except UnicodeDecodeError:
            raise AdapterError("Hy-SimulMT server returned an invalid response") from None

    @staticmethod
    def parse_events(stream: str, unit: UnitID) -> TranslationCandidate:
        parts, final_text, completed = [], None, False
        for line in stream.splitlines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except (ValueError, TypeError):
                raise AdapterError("Hy-SimulMT returned malformed events") from None
            if event.get("error") is not None or event.get("code", 0) != 0:
                raise AdapterError("Hy-SimulMT server failed to translate audio")
            delta = event.get("delta")
            if isinstance(delta, str) and not delta.startswith("run_turn_stream:"):
                parts.append(delta)
            if event.get("done") is True:
                completed = True
                text = event.get("text")
                if isinstance(text, str) and not text.startswith("run_turn_stream:"):
                    final_text = text
        text = " ".join(parts).strip()
        if not text and final_text:
            text = final_text.split("run_turn_stream:")[0].strip()
        if not completed:
            raise AdapterError("Hy-SimulMT returned no completed translation")
        return TranslationCandidate(unit, text)
