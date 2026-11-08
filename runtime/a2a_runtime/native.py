"""ctypes binding to the native library: the shared RT shim (rings, timing records, two-bus
mixer) and, on Windows, the WASAPI host. Build it with runtime/native/build.py.

Only ordinary threads call into this module. The RT side lives entirely in C: the WASAPI
engine thread (Windows) or the caller's offline render loop (file mode).
"""

from __future__ import annotations

import ctypes as C
import os
import sys
from pathlib import Path

import numpy as np

from . import REPO_ROOT

_LIB_NAMES = {"win32": "a2a_rt.dll", "darwin": "liba2a_rt.dylib"}
_PLATFORM_DIRS = {"win32": "windows", "darwin": "macos"}


class NativeUnavailable(RuntimeError):
    pass


def library_path() -> Path:
    override = os.environ.get("A2A_NATIVE_LIB")
    if override:
        return Path(override)
    name = _LIB_NAMES.get(sys.platform, "liba2a_rt.so")
    return REPO_ROOT / ".local" / "native" / _PLATFORM_DIRS.get(sys.platform, sys.platform) / name


class EngineConfig(C.Structure):
    _fields_ = [(n, C.c_uint32) for n in ("channels", "capture_ring_frames", "max_delay_frames",
                                          "translated_ring_frames", "max_block_frames", "record_capacity",
                                          "rendered_ring_frames", "fade_frames")]


class RTRecord(C.Structure):
    _fields_ = [("kind", C.c_uint32), ("flags", C.c_uint32), ("host_ns", C.c_uint64), ("sample_index", C.c_int64),
                ("frames", C.c_uint32), ("aux", C.c_uint32), ("aux64", C.c_int64), ("rate_scalar", C.c_double)]


class AudioTimeStamp(C.Structure):
    """The portable (non-Apple) layout from A2ARTShim.h."""

    _fields_ = [("mSampleTime", C.c_double), ("mHostTime", C.c_uint64), ("mRateScalar", C.c_double),
                ("mWordClockTime", C.c_uint64), ("mFlags", C.c_uint32), ("mReserved", C.c_uint32)]


HOST_TIME_VALID = 1 << 1

REC_CAPTURE, REC_OUTPUT, REC_CAPTURE_OVERFLOW, REC_TRANSLATED_STARVED, REC_TIMESTAMP_INVALID, REC_DISCONTINUITY = range(1, 7)


class WasapiConfig(C.Structure):
    _fields_ = [(n, C.c_uint32) for n in ("target_pid", "loopback_mode", "render", "sample_rate", "channels",
                                          "capture_buffer_ms", "render_buffer_ms")] + [
        ("capture_gain", C.c_float), ("render_device_id", C.c_char_p)]


class EndpointInfo(C.Structure):
    _fields_ = [("id", C.c_char * 256), ("name", C.c_char * 256), ("is_default", C.c_int32), ("reserved", C.c_int32)]


class WasapiStats(C.Structure):
    _fields_ = [("state", C.c_int32), ("hresult", C.c_int32)] + [
        (n, C.c_uint64) for n in ("capture_packets", "capture_frames", "capture_silent_packets",
                                  "capture_discontinuities", "capture_timestamp_errors", "render_wakeups",
                                  "render_frames", "render_full_wakeups", "timeouts", "last_capture_ns")
    ] + [(n, C.c_uint32) for n in ("render_buffer_frames", "render_mix_rate", "render_mix_channels", "reserved")] + [
        ("render_latency_ms", C.c_double), ("message", C.c_char * 256), ("render_device", C.c_char * 256)]


class SessionInfo(C.Structure):
    _fields_ = [("pid", C.c_uint32), ("state", C.c_int32), ("muted", C.c_int32), ("volume", C.c_float),
                ("display_name", C.c_char * 128), ("endpoint", C.c_char * 128), ("session_id", C.c_char * 256)]


LOOPBACK_INCLUDE_TREE, LOOPBACK_EXCLUDE_TREE = 0, 1
WASAPI_STARTING, WASAPI_RUNNING, WASAPI_STOPPED, WASAPI_FAILED = 0, 1, 2, -1

_lib = None


def lib():
    global _lib
    if _lib is not None:
        return _lib
    path = library_path()
    if not path.exists():
        raise NativeUnavailable(f"native library not built: {path} "
                                "(run `uv run --project runtime python runtime/native/build.py`)")
    L = C.CDLL(str(path))
    P, U32, U64, I32, F = C.c_void_p, C.c_uint32, C.c_uint64, C.c_int32, C.c_float
    fp = C.POINTER(C.c_float)
    sig = {
        "a2a_time_init": (None, []),
        "a2a_now_ns": (U64, []),
        "a2a_host_ticks_to_ns": (U64, [U64]),
        "a2a_ring_create": (P, [U32, U32]),
        "a2a_ring_destroy": (None, [P]),
        "a2a_ring_channels": (U32, [P]),
        "a2a_ring_capacity": (U32, [P]),
        "a2a_ring_write": (U32, [P, fp, U32]),
        "a2a_ring_read": (U32, [P, fp, U32]),
        "a2a_ring_skip": (U32, [P, U32]),
        "a2a_ring_readable": (U32, [P]),
        "a2a_ring_writable": (U32, [P]),
        "a2a_ring_total_written": (U64, [P]),
        "a2a_ring_total_read": (U64, [P]),
        "a2a_ring_overflow_frames": (U64, [P]),
        "a2a_record_ring_create": (P, [U32]),
        "a2a_record_ring_destroy": (None, [P]),
        "a2a_record_ring_pop": (U32, [P, C.POINTER(RTRecord), U32]),
        "a2a_record_ring_dropped": (U64, [P]),
        "a2a_engine_create": (P, [C.POINTER(EngineConfig)]),
        "a2a_engine_destroy": (None, [P]),
        "a2a_engine_capture_ring": (P, [P]),
        "a2a_engine_translated_ring": (P, [P]),
        "a2a_engine_enable_background": (C.c_bool, [P, U32]),
        "a2a_engine_background_ring": (P, [P]),
        "a2a_engine_set_background_gain": (None, [P, F, U32]),
        "a2a_engine_current_background_gain": (F, [P]),
        "a2a_engine_rendered_ring": (P, [P]),
        "a2a_engine_records": (P, [P]),
        "a2a_engine_set_input_channel_offset": (None, [P, U32]),
        "a2a_engine_set_render_output": (None, [P, C.c_bool]),
        "a2a_engine_set_capture_rendered": (None, [P, C.c_bool]),
        "a2a_engine_set_delay_frames": (None, [P, U32]),
        "a2a_engine_set_gains": (None, [P, F, F, U32]),
        "a2a_engine_flush_translated": (None, [P]),
        "a2a_engine_current_original_gain": (F, [P]),
        "a2a_engine_current_translated_gain": (F, [P]),
        "a2a_engine_callbacks": (U64, [P]),
        "a2a_engine_captured_frames": (U64, [P]),
        "a2a_engine_output_frames": (U64, [P]),
        "a2a_engine_translated_consumed": (U64, [P]),
        "a2a_engine_translated_starved_frames": (U64, [P]),
        "a2a_engine_rejected_blocks": (U64, [P]),
        "a2a_engine_process": (None, [P, fp, U32, P, fp, U32, P]),
    }
    if sys.platform == "win32":
        sig.update({
            "a2a_wasapi_start": (P, [P, C.POINTER(WasapiConfig)]),
            "a2a_wasapi_get_stats": (None, [P, C.POINTER(WasapiStats)]),
            "a2a_wasapi_stop": (None, [P]),
            "a2a_wasapi_set_capture_gain": (None, [P, F]),
            "a2a_wasapi_list_endpoints": (I32, [C.POINTER(EndpointInfo), I32]),
            "a2a_wasapi_list_sessions": (I32, [C.POINTER(SessionInfo), I32]),
            "a2a_wasapi_set_session_mute": (I32, [C.POINTER(C.c_uint32), I32, I32]),
            "a2a_wasapi_set_session_volume": (I32, [C.POINTER(C.c_uint32), I32, F]),
            "a2a_wasapi_set_session_volume_by_id": (I32, [C.c_char_p, F]),
            "a2a_wasapi_set_session_mute_by_id": (I32, [C.c_char_p, I32]),
            "a2a_wasapi_default_render_id": (I32, [C.c_char_p, I32]),
        })
    for name, (restype, argtypes) in sig.items():
        f = getattr(L, name)
        f.restype = restype
        f.argtypes = argtypes
    L.a2a_time_init()
    _lib = L
    return L


def now_ns() -> int:
    return lib().a2a_now_ns()


def _fptr(a: np.ndarray):
    return a.ctypes.data_as(C.POINTER(C.c_float))


class Ring:
    """Non-owning view of an engine-owned SPSC ring."""

    def __init__(self, handle) -> None:
        if not handle:
            raise ValueError("null ring")
        self.h = handle
        self.channels = lib().a2a_ring_channels(handle)

    def write(self, samples: np.ndarray) -> int:
        a = np.ascontiguousarray(samples, dtype=np.float32)
        return lib().a2a_ring_write(self.h, _fptr(a), len(a) // self.channels)

    def read(self, frames: int) -> np.ndarray:
        out = np.empty(frames * self.channels, dtype=np.float32)
        n = lib().a2a_ring_read(self.h, _fptr(out), frames)
        return out[: n * self.channels]

    def read_into(self, out: np.ndarray) -> int:
        return lib().a2a_ring_read(self.h, _fptr(out), len(out) // self.channels)

    def skip(self, frames: int) -> int:
        return lib().a2a_ring_skip(self.h, frames)

    @property
    def readable(self) -> int:
        return lib().a2a_ring_readable(self.h)

    @property
    def total_written(self) -> int:
        return lib().a2a_ring_total_written(self.h)

    @property
    def total_read(self) -> int:
        return lib().a2a_ring_total_read(self.h)

    @property
    def overflow_frames(self) -> int:
        return lib().a2a_ring_overflow_frames(self.h)

    @property
    def capacity(self) -> int:
        return lib().a2a_ring_capacity(self.h)


class Engine:
    """Owns an a2a_engine. `process` is for offline rendering on the driver thread only;
    a live WASAPI session calls the engine from its own RT thread instead."""

    def __init__(self, channels=1, capture_ring_frames=96_000, max_delay_frames=0, translated_ring_frames=1_440_000,
                 max_block_frames=4_096, record_capacity=4_096, rendered_ring_frames=0, fade_frames=240) -> None:
        self.config = EngineConfig(channels, capture_ring_frames, max_delay_frames, translated_ring_frames,
                                   max_block_frames, record_capacity, rendered_ring_frames, fade_frames)
        self.h = lib().a2a_engine_create(C.byref(self.config))
        if not self.h:
            raise MemoryError("engine allocation failed")
        self.channels = channels
        self.capture = Ring(lib().a2a_engine_capture_ring(self.h))
        self.translated = Ring(lib().a2a_engine_translated_ring(self.h))
        rendered = lib().a2a_engine_rendered_ring(self.h)
        self.rendered = Ring(rendered) if rendered else None
        self.background = None
        self._records = lib().a2a_engine_records(self.h)

    def close(self) -> None:
        if self.h:
            lib().a2a_engine_destroy(self.h)
            self.h = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def process(self, inp: np.ndarray | None, out_frames: int, in_ts: AudioTimeStamp | None = None,
                out_ts: AudioTimeStamp | None = None) -> np.ndarray:
        out = np.zeros(out_frames * self.channels, dtype=np.float32)
        if inp is not None:
            inp = np.ascontiguousarray(inp, dtype=np.float32)
            in_ptr, in_frames = _fptr(inp), len(inp) // self.channels
        else:
            in_ptr, in_frames = None, 0
        lib().a2a_engine_process(self.h, in_ptr, in_frames, C.byref(in_ts) if in_ts is not None else None,
                                 _fptr(out) if out_frames else None, out_frames,
                                 C.byref(out_ts) if out_ts is not None else None)
        return out

    def enable_background(self, capacity_frames: int) -> Ring:
        if not lib().a2a_engine_enable_background(self.h, capacity_frames):
            raise RuntimeError("background bus allocation failed or audio already started")
        self.background = Ring(lib().a2a_engine_background_ring(self.h))
        return self.background

    def set_background_gain(self, gain: float, ramp_frames: int) -> None:
        lib().a2a_engine_set_background_gain(self.h, float(gain), ramp_frames)

    def set_render_output(self, enabled: bool) -> None:
        lib().a2a_engine_set_render_output(self.h, enabled)

    def set_capture_rendered(self, enabled: bool) -> None:
        lib().a2a_engine_set_capture_rendered(self.h, enabled)

    def set_delay_frames(self, frames: int) -> None:
        lib().a2a_engine_set_delay_frames(self.h, frames)

    def set_gains(self, original: float, translated: float, ramp_frames: int) -> None:
        lib().a2a_engine_set_gains(self.h, original, translated, ramp_frames)

    def flush_translated(self) -> None:
        lib().a2a_engine_flush_translated(self.h)

    @property
    def gains(self) -> tuple[float, float]:
        L = lib()
        return L.a2a_engine_current_original_gain(self.h), L.a2a_engine_current_translated_gain(self.h)

    def counter(self, name: str) -> int:
        return getattr(lib(), f"a2a_engine_{name}")(self.h)

    def pop_records(self, batch: int = 512) -> list[RTRecord]:
        buf = (RTRecord * batch)()
        out: list[RTRecord] = []
        while True:
            n = lib().a2a_record_ring_pop(self._records, buf, batch)
            if not n:
                return out
            out.extend(RTRecord.from_buffer_copy(buf[i]) for i in range(n))

    @property
    def records_dropped(self) -> int:
        return lib().a2a_record_ring_dropped(self._records)
