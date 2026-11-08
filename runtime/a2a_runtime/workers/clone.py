"""Reference-conditioned synthesis through the resident TTS worker (research/tts_worker.py).
Port of apple/Sources/A2AModels/CloneVoiceSynthesizer.swift.

The worker keeps one verified model resident (MLX on macOS, PyTorch on CUDA/MPS/CPU with
`--backend torch`) and streams 24 kHz PCM per unit. Reference audio and text travel in
memory only. The model entries are research-only: summaries report
`tts_identity_qualified: false`, and runs never count as identity or gate evidence.
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import numpy as np

from ..core.direction import TranslationLanguage
from ..core.types import PCMFormat, PreparedVoice, VoiceProfile
from ..media.resampler import StreamingResampler
from ..models.base import AdapterError, DeliveryClip, SynthesisCapabilities, SynthesizedAudio
from .protocol import WorkerProcess

SAMPLE_RATE = 24_000


@dataclass(frozen=True)
class CloneSynthesisStats:
    """Worker-side pacing of one completed unit: numbers only, no text or audio."""

    # WSOLA pacing applied to fit the source span: above 1 a long take was sped up, below 1 a
    # short one was slowed to end with the actor (fill), 1 = no pacing.
    pace: float
    # Shaped audio before pacing and quiet-run compaction.
    native_seconds: float
    # Audio actually sent to the host.
    audio_seconds: float
    quiet_removed_seconds: float
    # Character-count duration budget the worker enforced, and whether it stopped generation.
    budget_seconds: float = 0.0
    truncated: bool = False
    # Takes discarded because they produced no voiced audio before the silent-take limit.
    retries: int = 0
    # Per-line conditioning verdict and its appended-audio/preparation cost (ADR-0015).
    delivery: str = "absent"
    delivery_seconds: float = 0.0
    delivery_prep_seconds: float = 0.0


@dataclass(frozen=True)
class CloneWorkerInfo:
    model: str
    load_seconds: float
    warmup_seconds: float
    streams_audio: bool
    device: str | None = None


class _Stream:
    def __init__(self, owner: CloneVoiceSynthesizer, job: int) -> None:
        self.owner = owner
        self.id = job
        self.q: queue.Queue = queue.Queue(maxsize=512)
        self.done = False

    def __iter__(self):
        while True:
            kind, value = self.q.get()
            if kind == "chunk":
                yield value
            elif kind == "done":
                self.done = True
                return
            else:
                self.done = True
                raise AdapterError(value)

    def cancel(self) -> None:
        if not self.done and self.owner._streams.pop(self.id, None) is not None:
            self.owner._worker.send({"op": "cancel", "id": self.id})
        self.done = True


class CloneVoiceSynthesizer:
    name = "clone"
    # Takes `allow_fill`: a slotted host may let the worker slow a short take (ADR-0013).
    supports_fill = True

    def __init__(self, argv: list[str], conditioning: str = "icl",
                 target_language: TranslationLanguage = TranslationLanguage.ENGLISH, ready_timeout: float = 600,
                 stop_event: threading.Event | None = None, load_gate: threading.Event | None = None) -> None:
        """`load_gate`: the worker starts now (imports, weight hashes) but loads its weights
        onto the device only once the gate is set (see cli.load_stages)."""
        self.conditioning = conditioning
        self.target_language = target_language
        self.info: CloneWorkerInfo | None = None
        self._failure: str | None = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._prepares: dict[str, list] = {}
        self._streams: dict[int, _Stream] = {}
        self._units: dict[int, int] = {}  # stream id -> unit
        self._stats: dict[int, CloneSynthesisStats] = {}  # unit -> pacing of its completed take
        self._next_id = 0
        self._worker = WorkerProcess(argv + (["--wait-load"] if load_gate is not None else []), self._on_event,
                                     self._on_exit)
        deadline = time.monotonic() + ready_timeout
        while not self._ready.wait(min(0.1, max(0, deadline - time.monotonic()))):
            if load_gate is not None and load_gate.is_set():
                self._worker.send({"op": "load"})
                load_gate = None
            if stop_event is not None and stop_event.is_set():
                self._fail("worker startup canceled")
                break
            if time.monotonic() >= deadline:
                self._fail(f"worker did not become ready within {ready_timeout:.0f} s")
                break
        if self._failure:
            self.shutdown()
            raise AdapterError(self._failure)

    @property
    def failure(self) -> str | None:
        return self._failure

    @property
    def capabilities(self) -> SynthesisCapabilities:
        streams = self.info.streams_audio if self.info else True
        return SynthesisCapabilities(audio_output_streaming=streams, incremental_text_input=False,
                                     continuation_across_units=False, preserves_speaker_identity=True)

    def shutdown(self) -> None:
        self._worker.shutdown()

    @staticmethod
    def key(speaker, version: int) -> str:
        return f"{speaker.raw_value}/{version}"

    def prepare(self, profile: VoiceProfile, timeout: float = 120) -> PreparedVoice:
        pcm = np.asarray(profile.reference_pcm, dtype=np.float32)
        if profile.channel_count > 1:
            pcm = pcm.reshape(-1, profile.channel_count).mean(axis=1).astype(np.float32)
        if profile.sample_rate != SAMPLE_RATE:
            pcm = StreamingResampler.resample(pcm, profile.sample_rate, SAMPLE_RATE)
        key = self.key(profile.speaker_id, profile.version)
        done = threading.Event()
        slot: list = [done, None]
        with self._lock:
            if self._failure:
                raise AdapterError(self._failure)
            self._prepares[key] = slot
        self._worker.send({"op": "prepare", "voice": key, "text": profile.reference_text, "mode": self.conditioning,
                           "rate": SAMPLE_RATE, "language": self.target_language.clone_language},
                          pcm.astype("<f4").tobytes())
        if not done.wait(timeout):
            raise AdapterError("voice preparation timed out")
        if slot[1] is not None:
            raise AdapterError(slot[1])
        return PreparedVoice(profile.speaker_id, profile.version, profile.version)

    def synthesize(self, unit: int, text: str, voice: PreparedVoice, target_seconds: float | None = None,
                   allow_fill: bool = False, lane: int = 0, delivery: DeliveryClip | None = None) -> _Stream:
        with self._lock:
            if self._failure:
                raise AdapterError(self._failure)
            self._next_id += 1
            stream = _Stream(self, self._next_id)
            self._streams[stream.id] = stream
            self._units[stream.id] = unit
        request = {"op": "synth", "id": stream.id, "voice": self.key(voice.speaker_id, voice.profile_version), "text": text}
        if target_seconds is not None and target_seconds > 0:
            request["target_s"] = target_seconds
            # Only a slotted host may slow a take; see SynthesisJob.allow_fill.
            if allow_fill:
                request["fill"] = True
        payload = b""
        if delivery is not None and len(delivery.pcm):
            request["delivery"] = True
            if delivery.source_text:
                request["delivery_text"] = delivery.source_text
            pcm = (delivery.pcm if delivery.sample_rate == SAMPLE_RATE else
                   StreamingResampler.resample(delivery.pcm, delivery.sample_rate, SAMPLE_RATE))
            payload = np.asarray(pcm, dtype="<f4").tobytes()
        self._worker.send(request, payload)
        return stream

    def take_stats(self, unit: int) -> CloneSynthesisStats | None:
        """Pacing of a unit whose synthesis completed, once; None when canceled or unknown."""
        with self._lock:
            return self._stats.pop(unit, None)

    # MARK: Protocol events (reader thread)

    def _on_event(self, event: dict, payload: bytes) -> None:
        kind = event.get("ev")
        if kind == "ready":
            self.info = CloneWorkerInfo(event.get("model", ""), float(event.get("load_s", 0)),
                                        float(event.get("warmup_s", 0)), bool(event.get("streaming", True)),
                                        event.get("device"))
            self._ready.set()
        elif kind == "prepared":
            slot = self._prepares.pop(str(event.get("voice")), None)
            if slot:
                slot[0].set()
        elif kind == "chunk":
            stream = self._streams.get(int(event["id"]))
            if stream is None:
                return  # canceled
            samples = np.frombuffer(payload, dtype="<f4").astype(np.float32)
            try:
                stream.q.put_nowait(("chunk", SynthesizedAudio(samples, PCMFormat(SAMPLE_RATE, 1))))
            except queue.Full:
                self._streams.pop(stream.id, None)
                stream.q.put(("error", "synthesis consumer fell behind"))
        elif kind == "done":
            job = int(event["id"])
            with self._lock:
                unit = self._units.pop(job, None)
                if unit is not None and not event.get("canceled"):
                    # Bounded: the driver takes each entry when the unit finishes.
                    if len(self._stats) >= 64:
                        self._stats.clear()
                    self._stats[unit] = CloneSynthesisStats(
                        pace=float(event.get("pace") or 1.0), native_seconds=float(event.get("native_s") or 0),
                        audio_seconds=float(event.get("audio_s") or 0),
                        quiet_removed_seconds=float(event.get("quiet_samples_removed") or 0) / SAMPLE_RATE,
                        budget_seconds=float(event.get("budget_s") or 0), truncated=bool(event.get("truncated")),
                        retries=int(event.get("retries") or 0), delivery=str(event.get("delivery") or "absent"),
                        delivery_seconds=float(event.get("delivery_s") or 0),
                        delivery_prep_seconds=float(event.get("delivery_prep_s") or 0))
            stream = self._streams.pop(job, None)
            if stream:
                stream.q.put(("done", None))
        elif kind == "error":
            message = str(event.get("message", "worker error"))
            if event.get("id") is not None:
                with self._lock:
                    self._units.pop(int(event["id"]), None)
                stream = self._streams.pop(int(event["id"]), None)
                if stream:
                    stream.q.put(("error", message))
            elif event.get("voice") is not None:
                slot = self._prepares.pop(str(event["voice"]), None)
                if slot:
                    slot[1] = message
                    slot[0].set()
            else:
                self._fail(message)

    def _on_exit(self, message: str) -> None:
        self._fail(message)

    def _fail(self, message: str) -> None:
        with self._lock:
            if self._failure is None:
                self._failure = message
            prepares, self._prepares = list(self._prepares.values()), {}
            streams, self._streams = list(self._streams.values()), {}
            self._units.clear()
        for slot in prepares:
            slot[1] = message
            slot[0].set()
        for stream in streams:
            stream.q.put(("error", message))
        self._ready.set()
