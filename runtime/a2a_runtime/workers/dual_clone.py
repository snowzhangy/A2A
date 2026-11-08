"""Two resident clone workers, one per lane: the portable counterpart of
apple/Sources/A2AModels/DualCloneVoiceSynthesizer.swift (ADR-0007).

Fluent slot playout wants the next phrase synthesized while the current one is still
playing. With one worker the second phrase waits for the first; with two, each has its own
worker and its own speaker-conditioning cache, so the driver can overlap them.

The cost is a second copy of the model in VRAM. On a 24 GB card alongside Qwen and Whisper
that usually does not fit, so `admit` refuses when free memory is short and the caller
falls back to one worker instead of failing the run.
"""

from __future__ import annotations

import threading

from ..core.types import PreparedVoice
from ..models.base import AdapterError, SynthesisCapabilities
from .clone import CloneVoiceSynthesizer
from .speech_worker import cuda_free_bytes

# A 0.6B FP16 voice needs ~1.5 GB of weights plus CUDA context and activation headroom.
DEFAULT_WORKER_BYTES = int(1.6 * 1024**3)


class DualCloneVoiceSynthesizer:
    """One CloneVoiceSynthesizer per lane; the driver never runs two jobs on one lane."""

    name = "clone"
    lanes = 2
    supports_fill = True

    def __init__(self, argv: list[str], conditioning: str = "icl", target_language=None, ready_timeout: float = 600,
                 stop_event: threading.Event | None = None) -> None:
        self._workers = [CloneVoiceSynthesizer(argv, conditioning=conditioning, target_language=target_language,
                                               ready_timeout=ready_timeout, stop_event=stop_event)
                         for _ in range(self.lanes)]

    @staticmethod
    def admit(free_bytes: int | None, per_worker: int = DEFAULT_WORKER_BYTES) -> str | None:
        """None when two workers fit, else why the caller should keep one."""
        if free_bytes is None:
            return None  # an unknown reading is not a refusal; the operator asked for two
        if free_bytes < 2 * per_worker:
            return (f"two voice workers need about {2 * per_worker / 1024**3:.1f} GB but only "
                    f"{free_bytes / 1024**3:.1f} GB is free; using one worker")
        return None

    @property
    def capabilities(self) -> SynthesisCapabilities:
        return self._workers[0].capabilities

    @property
    def failure(self) -> str | None:
        return next((w.failure for w in self._workers if w.failure), None)

    @property
    def info(self):
        return self._workers[0].info

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    def prepare_lanes(self, profile, timeout: float = 120) -> list[PreparedVoice]:
        """One prepared hand-off per lane: a handle is only meaningful to the worker that
        produced it, and lane 1's job must be voiced by worker 1."""
        ready: list = [None] * len(self._workers)
        errors: list[BaseException | None] = [None] * len(self._workers)

        def run(i: int) -> None:
            try:
                ready[i] = self._workers[i].prepare(profile, timeout=timeout)
            except BaseException as error:  # surfaced on the driver's thread below
                errors[i] = error

        threads = [threading.Thread(target=run, args=(i,), name=f"a2a.clone.prepare{i}", daemon=True)
                   for i in range(len(self._workers))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        failure = next((e for e in errors if e is not None), None)
        if failure is not None:
            raise failure
        first = ready[0]
        if any(v.speaker_id != first.speaker_id or v.profile_version != first.profile_version for v in ready):
            raise AdapterError("voice worker profiles disagree")
        return list(ready)

    def prepare(self, profile, timeout: float = 120) -> PreparedVoice:
        return self.prepare_lanes(profile, timeout)[0]

    def synthesize(self, unit: int, text: str, voice: PreparedVoice, target_seconds: float | None = None,
                   allow_fill: bool = False, lane: int = 0, delivery=None):
        if not 0 <= lane < len(self._workers):
            raise AdapterError(f"lane {lane} is not one of the {len(self._workers)} workers")
        # The driver prepares the voice per lane, but a handle is only meaningful to the
        # worker that produced it; both workers were conditioned identically in prepare().
        return self._workers[lane].synthesize(unit, text, voice, target_seconds, allow_fill=allow_fill,
                                              delivery=delivery)

    def take_stats(self, unit: int):
        for worker in self._workers:
            stats = worker.take_stats(unit)
            if stats is not None:
                return stats
        return None

    def shutdown(self) -> None:
        for w in self._workers:
            w.shutdown()


def free_gpu_bytes() -> int | None:
    return cuda_free_bytes()
