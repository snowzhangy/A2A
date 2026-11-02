"""Client for A2A's resident TTS worker.

The worker (`research/tts_worker.py`) is the product-path engine: it keeps one verified model
resident on MLX, streams PCM per unit, calibrates level against the reference, trims silence and
paces to a target duration. Rather than run a second implementation of all that, this lab drives
it over its length-prefixed frame protocol:

    <u32 header bytes><u32 payload bytes><header JSON><payload>
    in :  prepare {voice, text, mode, rate, language} + f32le mono 24 kHz reference
          synth   {id, voice, text, target_s?}     cancel {id}     quit {}
    out :  ready {model, load_s, warmup_s}  prepared {voice, seconds, prepare_s}
           chunk {id, rate} + f32le mono   done {id, audio_s, gen_s, first_s}  error {message}

If that file changes underneath this lab, `selftest` below is what fails first.
"""
from __future__ import annotations

import json
import os
import pathlib
import struct
import subprocess
import typing

import numpy as np

REPO = pathlib.Path(__file__).resolve().parents[2]
WORKER = REPO / "research/tts_worker.py"
PYTHON = REPO / ".local/live-dub/venv/bin/python"
RATE = 24_000


class TtsWorker:
    def __init__(self, model: str = "qwen", temperature: float | None = None,
                 research_text: bool = True, log=None):
        cmd = [str(PYTHON), str(WORKER), "--model", model]
        if temperature is not None:
            cmd += ["--temperature", str(temperature)]
        if research_text:
            cmd.append("--research-text")
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        self.log = log
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=(self.log or subprocess.DEVNULL), env=env)
        self._next_id = 0
        self.ready: dict = {}
        self.rate = RATE

    # --- framing ---------------------------------------------------------------

    def _send(self, header: dict, payload: bytes = b"") -> None:
        data = json.dumps(header, ensure_ascii=False).encode()
        assert self.proc.stdin is not None
        self.proc.stdin.write(struct.pack("<II", len(data), len(payload)) + data + payload)
        self.proc.stdin.flush()

    def _read(self, n: int) -> bytes:
        assert self.proc.stdout is not None
        buf = self.proc.stdout.read(n)
        if buf is None or len(buf) != n:
            raise RuntimeError("the TTS worker stopped")
        return buf

    def _event(self) -> tuple[dict, bytes]:
        hlen, plen = struct.unpack("<II", self._read(8))
        return json.loads(self._read(hlen)), self._read(plen) if plen else b""

    def wait_ready(self, timeout: float = 300.0) -> dict:
        import time
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            header, _ = self._event()
            if header.get("ev") == "ready":
                self.ready = header
                return header
            if header.get("ev") == "error":
                raise RuntimeError(f"worker failed to start: {header.get('message')}")
        raise TimeoutError("the TTS worker never reported ready")

    # --- requests --------------------------------------------------------------

    def prepare(self, voice: str, reference: np.ndarray, text: str = "",
                language: str = "chinese", mode: str = "icl") -> dict:
        pcm = np.asarray(reference, dtype=np.float32).reshape(-1)
        self._send({"op": "prepare", "voice": voice, "text": text, "mode": mode,
                    "rate": self.rate, "language": language}, pcm.tobytes())
        while True:
            header, _ = self._event()
            if header.get("ev") in ("prepared", "error"):
                if header.get("ev") == "error":
                    raise RuntimeError(f"prepare {voice}: {header.get('message')}")
                return header

    def synth(self, voice: str, text: str,
              target_s: float | None = None) -> typing.Iterator[np.ndarray]:
        """Yield mono 24 kHz chunks as they are made; `.stats` holds the done event afterwards."""
        self._next_id += 1
        job = self._next_id
        header = {"op": "synth", "id": job, "voice": voice, "text": text}
        if target_s:
            header["target_s"] = round(target_s, 3)
        self._send(header)
        self.stats: dict = {}
        while True:
            event, payload = self._event()
            kind = event.get("ev")
            if kind == "chunk" and event.get("id") == job:
                self.rate = int(event.get("rate", RATE))
                yield np.frombuffer(payload, dtype=np.float32).copy()
            elif kind == "done" and event.get("id") == job:
                self.stats = event
                return
            elif kind == "error":
                self.stats = {"error": event.get("message")}
                return

    def synthesize(self, voice: str, text: str, target_s: float | None = None) -> np.ndarray:
        chunks = list(self.synth(voice, text, target_s))
        return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)

    def close(self) -> None:
        try:
            self._send({"op": "quit"})
        except Exception:
            pass
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.wait(timeout=10)

    def __enter__(self):
        self.wait_ready()
        return self

    def __exit__(self, *exc):
        self.close()
