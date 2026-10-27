"""Trace format v0. Port of apple/Sources/A2ACore/TraceEvent.swift.

Serialization is deterministic (fixed header order, fields in insertion order) and
byte-identical to the Swift writer, so golden traces compare across implementations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum

from .types import SourceSpan, SpeakerID, UnitID

SCHEMA_VERSION = 0


class TraceKind(str, Enum):
    SESSION_START = "session.start"
    BUF_CAPTURE = "buf.capture"
    BUF_OUTPUT = "buf.output"
    STATE_CHANGE = "state.change"
    VAD_START = "vad.start"
    VAD_END = "vad.end"
    SPK_ASSIGN = "spk.assign"
    SPK_CHANGE = "spk.change"
    PROFILE_READY = "profile.ready"
    ASR_PARTIAL = "asr.partial"
    ASR_FINAL = "asr.final"
    COMMIT_SOURCE = "commit.source"
    MT_REQUEST = "mt.request"
    MT_RESULT = "mt.result"
    COMMIT_TARGET = "commit.target"
    TTS_REQUEST = "tts.request"
    TTS_FIRST_CHUNK = "tts.first_chunk"
    TTS_CHUNK = "tts.chunk"
    PLAY_SCHEDULED = "play.scheduled"
    PLAY_START = "play.start"
    PLAY_END = "play.end"
    UNIT_CANCEL = "unit.cancel"
    SOURCE_DISPOSITION = "source.disposition"
    QUEUE_DEPTH = "queue.depth"
    CLOCK_DRIFT = "clock.drift"
    RESOURCE_SAMPLE = "resource.sample"
    FALLBACK_ENTER = "fallback.enter"
    FALLBACK_EXIT = "fallback.exit"
    TRACE_DROP = "trace.drop"
    ERROR = "error"
    # Extensions beyond the required set; the schema allows them explicitly.
    COMMIT_HOLD = "commit.hold"
    COMMIT_REVISION = "commit.revision"
    PCM_REJECT = "pcm.reject"
    GAIN_RAMP = "gain.ramp"
    FINALIZE_REQUEST = "asr.finalize_request"
    CATCH_UP = "catch_up"


class _Null:
    __slots__ = ()

    def __repr__(self) -> str:
        return "NULL"


NULL = _Null()


@dataclass(frozen=True, slots=True)
class Double:
    """Marks a value serialized as a Swift Double (always with a decimal point)."""

    value: float


# A field value: int, Double, str, bool, SourceSpan, or NULL. Plain floats serialize as Double.
Field = tuple[str, object]


def f_int(key: str, v: int) -> Field:
    return (key, int(v))


def f_double(key: str, v: float) -> Field:
    return (key, Double(float(v)))


def f_str(key: str, v: str) -> Field:
    return (key, str(v))


def f_bool(key: str, v: bool) -> Field:
    return (key, bool(v))


def f_span(v: SourceSpan) -> Field:
    return ("span", v)


def f_unit(v: UnitID) -> Field:
    return ("unit", str(v))


def f_speaker(v: SpeakerID) -> Field:
    return ("spk", v.raw_value)


@dataclass(frozen=True, slots=True)
class TraceEvent:
    t_ns: int
    session: str
    epoch: int
    gen: int
    kind: TraceKind
    fields: tuple[Field, ...] = ()

    def value(self, key: str):
        for k, v in self.fields:
            if k == key:
                return v
        return None

    def json_line(self) -> str:
        out = [f'{{"v":{SCHEMA_VERSION},"t_ns":{self.t_ns},"session":']
        out.append(json_string(self.session))
        out.append(f',"epoch":{self.epoch},"gen":{self.gen},"ev":')
        out.append(json_string(self.kind.value))
        for key, value in self.fields:
            out.append(",")
            out.append(json_string(key))
            out.append(":")
            out.append(json_value(value))
        out.append("}")
        return "".join(out)


def format_double(v: float) -> str:
    """Swift's Double description: shortest round trip; integral values keep `.0`."""
    if not math.isfinite(v):
        return "null"
    if v == round(v) and abs(v) < 1e15:
        return f"{int(v)}.0"
    return repr(v)


def json_value(value) -> str:
    if value is NULL or value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Double):
        return format_double(value.value)
    if isinstance(value, float):
        return format_double(value)
    if isinstance(value, str):
        return json_string(value)
    if isinstance(value, SourceSpan):
        return f"[{value.start},{value.end}]"
    raise TypeError(f"unsupported trace value {value!r}")


def json_string(s: str) -> str:
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)
