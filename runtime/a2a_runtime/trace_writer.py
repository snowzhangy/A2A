"""Non-RT trace writer and RT record drain. Port of apple/Sources/A2ATrace.

RT threads never call this module; they push fixed-size records into native rings, and
`RTRecordDrain` converts those on the driver thread. Invalid timestamps are reported,
never replaced with fabricated times.
"""

from __future__ import annotations

import threading
from pathlib import Path

from . import native
from .core.trace import TraceEvent, TraceKind, f_bool, f_int, f_span, f_str
from .core.types import CANONICAL_SAMPLE_RATE, SourceSpan


class TraceWriter:
    def __init__(self, path: str | Path, flush_every: int = 64) -> None:
        self._fh = open(path, "w", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()
        self._buffer: list[str] = []
        self._flush_every = flush_every
        self.event_count = 0
        self._closed = False

    def emit(self, event: TraceEvent) -> None:
        line = event.json_line()
        with self._lock:
            self._buffer.append(line)
            self.event_count += 1
            if len(self._buffer) >= self._flush_every:
                self._write_locked()

    def _write_locked(self) -> None:
        if self._buffer and not self._closed:
            self._fh.write("\n".join(self._buffer) + "\n")
            self._buffer.clear()

    def flush(self) -> None:
        with self._lock:
            self._write_locked()

    def close(self) -> None:
        with self._lock:
            self._write_locked()
            if not self._closed:
                self._closed = True
                self._fh.close()


class RTRecordDrain:
    """Converts native timing records to trace events on an ordinary thread.

    `report_starvation=False` only counts translated-bus starvation: the engine cannot tell
    an idle gap between units from a unit that ran dry, so a driver that tracks units (and
    reports per-unit underruns itself) turns the per-callback errors off."""

    def __init__(self, engine: native.Engine, report_starvation: bool = True) -> None:
        self.engine = engine
        self.report_starvation = report_starvation
        self._reported_drops = 0
        self.capture_blocks = 0
        self.output_blocks = 0
        self.invalid_timestamps = 0
        self.overflow_frames = 0
        self.starved_frames = 0

    def drain(self, session: str, epoch: int, generation: int, device_rate: float, sink: TraceWriter) -> int:
        records = self.engine.pop_records()
        for r in records:
            self._convert(r, session, epoch, generation, device_rate, sink)
        dropped = self.engine.records_dropped
        if dropped > self._reported_drops:
            sink.emit(TraceEvent(native.now_ns(), session, epoch, generation, TraceKind.TRACE_DROP,
                                 (f_int("count", dropped - self._reported_drops), f_str("ring", "rt_timing"))))
            self._reported_drops = dropped
        return len(records)

    def _convert(self, r, session, epoch, gen, device_rate, sink) -> None:
        t = r.host_ns if r.host_ns else native.now_ns()
        scale = CANONICAL_SAMPLE_RATE / device_rate

        def ev(kind, fields):
            sink.emit(TraceEvent(t, session, epoch, gen, kind, tuple(fields)))

        if r.kind == native.REC_CAPTURE:
            self.capture_blocks += 1
            start = int(round_half_away(r.sample_index * scale))
            end = int(round_half_away((r.sample_index + r.frames) * scale))
            ev(TraceKind.BUF_CAPTURE, [f_span(SourceSpan(start, end)), f_int("frames", r.frames),
                                       f_int("device_index", r.sample_index), f_int("ts_flags", r.flags),
                                       f_bool("host_time_valid", r.host_ns != 0)])
        elif r.kind == native.REC_OUTPUT:
            self.output_blocks += 1
            ev(TraceKind.BUF_OUTPUT, [f_int("out_sample", r.sample_index), f_int("frames", r.frames),
                                      f_int("translated_consumed", r.aux64), f_int("ts_flags", r.flags),
                                      f_int("rate", int(device_rate)), f_bool("host_time_valid", r.host_ns != 0)])
        elif r.kind == native.REC_CAPTURE_OVERFLOW:
            self.overflow_frames += r.frames
            ev(TraceKind.ERROR, [f_str("where", "capture_ring"), f_str("message", "overflow"), f_int("frames", r.frames),
                                 f_int("device_index", r.sample_index)])
        elif r.kind == native.REC_TRANSLATED_STARVED:
            self.starved_frames += r.frames
            if self.report_starvation:
                ev(TraceKind.ERROR, [f_str("where", "translated_bus"), f_str("message", "starved"),
                                     f_int("frames", r.frames), f_int("out_sample", r.sample_index)])
        elif r.kind == native.REC_TIMESTAMP_INVALID:
            self.invalid_timestamps += 1
            where = "capture_timestamp" if r.aux == native.REC_CAPTURE else "output_timestamp"
            ev(TraceKind.ERROR, [f_str("where", where), f_str("message", "host time invalid"), f_int("ts_flags", r.flags)])
        else:
            ev(TraceKind.ERROR, [f_str("where", "rt_record"), f_int("kind", r.kind)])


def round_half_away(x: float) -> float:
    import math

    return math.floor(x + 0.5) if x >= 0 else -math.floor(-x + 0.5)
