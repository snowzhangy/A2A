"""Length-prefixed worker protocol shared by every inference worker (ADR-0002, ADR-0007):

    <u32 header bytes, little endian> <u32 payload bytes> <header JSON> <payload>

The host owns the worker's stdin; closing it ends the worker, so a killed host never leaves
a model resident. Payloads are f32le PCM. This module holds both ends: `WorkerProcess`
(host side, a reader thread dispatching events) and `WorkerChannel` (worker side).
"""

from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
from typing import Callable

MAX_HEADER = 1 << 20
MAX_PAYLOAD = 64 << 20


def encode(header: dict, payload: bytes = b"") -> bytes:
    data = json.dumps(header, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_HEADER or len(payload) > MAX_PAYLOAD:
        raise ValueError("frame exceeds limits")
    return struct.pack("<II", len(data), len(payload)) + data + payload


def _write_all(stream, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = stream.write(remaining)
        if written is None or written <= 0:
            raise BrokenPipeError("worker pipe closed during write")
        remaining = remaining[written:]


def _read_exact(stream, n: int) -> bytes | None:
    chunks, remaining = [], n
    while remaining:
        block = stream.read(remaining)
        if not block:
            return None
        chunks.append(block)
        remaining -= len(block)
    return b"".join(chunks)


def read_frame(stream) -> tuple[dict, bytes] | None:
    prefix = _read_exact(stream, 8)
    if prefix is None:
        return None
    header_len, payload_len = struct.unpack("<II", prefix)
    if header_len > MAX_HEADER or payload_len > MAX_PAYLOAD:
        raise ValueError("frame exceeds limits")
    header = _read_exact(stream, header_len)
    payload = _read_exact(stream, payload_len) if payload_len else b""
    if header is None or payload is None:
        return None
    return json.loads(header), payload


class WorkerProcess:
    """Host side. `on_event(header, payload)` runs on the reader thread; `on_exit(message)`
    runs once when the worker exits or breaks protocol."""

    def __init__(self, argv: list[str], on_event: Callable[[dict, bytes], None],
                 on_exit: Callable[[str], None], env: dict | None = None, cwd: str | None = None) -> None:
        creation = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None,
                                     env=env, cwd=cwd, creationflags=creation, bufsize=0)
        self._lock = threading.Lock()
        self._closed = False
        self._on_event = on_event
        self._on_exit = on_exit
        self._reader = threading.Thread(target=self._read_loop, name=f"a2a.worker.{os.path.basename(argv[-1])}",
                                        daemon=True)
        self._reader.start()

    def send(self, header: dict, payload: bytes = b"") -> bool:
        frame = encode(header, payload)
        with self._lock:
            if self._closed:
                return False
            try:
                _write_all(self.proc.stdin, frame)
                self.proc.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                self._closed = True
        self._on_exit("worker input closed")
        return False

    def _read_loop(self) -> None:
        message = "worker exited"
        try:
            while True:
                frame = read_frame(self.proc.stdout)
                if frame is None:
                    break
                self._on_event(*frame)
        except Exception as error:  # malformed frame: the stream cannot be resynchronized
            message = f"worker protocol error: {type(error).__name__}"
        self._on_exit(message)

    def shutdown(self, timeout: float = 5.0) -> None:
        """Closes the worker's stdin; it exits on EOF without finishing queued work."""
        with self._lock:
            if not self._closed:
                self._closed = True
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


class WorkerChannel:
    """Worker side. The protocol owns the original stdout; anything a library prints goes
    to stderr. Call before importing model libraries."""

    def __init__(self) -> None:
        fd = os.dup(1)
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        if sys.platform == "win32":
            import msvcrt

            msvcrt.setmode(fd, os.O_BINARY)
            msvcrt.setmode(0, os.O_BINARY)
        self._out = os.fdopen(fd, "wb", buffering=0)
        self._lock = threading.Lock()
        self._in = sys.stdin.buffer

    def send(self, header: dict, payload: bytes = b"") -> None:
        frame = encode(header, payload)
        with self._lock:
            _write_all(self._out, frame)

    def receive(self) -> tuple[dict, bytes] | None:
        return read_frame(self._in)
