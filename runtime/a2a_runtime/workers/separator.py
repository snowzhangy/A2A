"""Host adapter for the isolated RNNoise separator worker."""

from __future__ import annotations

import queue
import threading

import numpy as np

from ..models.base import AdapterError
from .protocol import WorkerProcess


class RNNoiseSeparator:
    name = "rnnoise"
    latency_seconds = 0.01

    def __init__(self, argv: list[str]) -> None:
        self._failure = None
        self._events: queue.Queue = queue.Queue(maxsize=256)
        self._worker = WorkerProcess(argv, self._on_event, self._on_exit)
        self._ready = threading.Event()
        self._worker_thread = threading.Thread(target=self._wait_ready, daemon=True)
        self._worker_thread.start()
        self._ready.wait(30)
        if self._failure:
            self.shutdown()
            raise AdapterError(self._failure)
        if not self._ready.is_set():
            self.shutdown()
            raise AdapterError("separator worker did not become ready")

    def _wait_ready(self):
        self._ready.wait(30)

    @property
    def failure(self):
        return self._failure

    def describe(self):
        return "rnnoise (experimental voice enhancement; bed quality unqualified)"

    def separate(self, samples) -> None:
        pcm = np.asarray(samples, dtype="<f4")
        if len(pcm) % 480:
            raise AdapterError("separator needs whole 10 ms frames")
        if not self._worker.send({"op": "separate", "id": 0}, pcm.tobytes()):
            raise AdapterError(self._failure or "separator input closed")

    def drain(self) -> list[tuple[np.ndarray, np.ndarray]]:
        out = []
        while True:
            try:
                voice, bed = self._events.get_nowait()
            except queue.Empty:
                return out
            out.append((voice, bed))

    def reset(self) -> None:
        self._worker.send({"op": "reset"})
        while not self._events.empty():
            self._events.get_nowait()

    def _on_event(self, event, payload):
        kind = event.get("ev")
        if kind == "ready":
            self._ready.set()
        elif kind == "separated":
            pcm = np.frombuffer(payload, dtype="<f4")
            self._events.put((pcm[0::2].copy(), pcm[1::2].copy()))
        elif kind == "error":
            self._fail(str(event.get("message", "separator failed")))

    def _on_exit(self, message):
        self._fail(message)

    def _fail(self, message):
        self._failure = message
        self._ready.set()

    def shutdown(self):
        self._worker.shutdown()
