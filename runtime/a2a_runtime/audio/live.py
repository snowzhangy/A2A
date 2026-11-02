"""Live tap host for the portable driver: the Windows counterpart of A2AAudio.TapSession as
the pipeline uses it (ADR-0007).

One engine (stereo, 48 kHz) is shared by the WASAPI engine thread and the driver: the RT
thread captures and renders, the driver reads the capture ring and writes the translated
ring (each ring single-producer/single-consumer). The source is ducked rather than muted
(session mute also silences process-loopback capture), and the capture gain restores it.
"""

from __future__ import annotations

import sys
import time

from .. import native
from ..core.pipeline import PlaybackMode

EDGE_FADE_SECONDS = 0.005


class LiveTap:
    sample_rate = 48_000
    channels = 2

    def __init__(self, source, *, render: bool, silence: bool, duck_db: float, output_device: str | None,
                 parent_pid: int | None, translated_ring_seconds: float = 30.0, rendered_ring_seconds: float = 0.0,
                 original_delay_ms: float = 30.0) -> None:
        if sys.platform != "win32":
            raise SystemExit("a2a-pipeline (portable): live capture is implemented for Windows; on macOS use "
                             "apple/ a2a-pipeline --tap-bundle")
        from . import windows as win

        self.win = win
        self.source = source
        self.render = render
        self.parent_pid = parent_pid
        rate = self.sample_rate
        self.engine = native.Engine(channels=self.channels, capture_ring_frames=rate * 4, max_delay_frames=rate // 5,
                                    translated_ring_frames=int(rate * translated_ring_seconds), max_block_frames=8_192,
                                    record_capacity=16_384, rendered_ring_frames=int(rate * rendered_ring_seconds),
                                    fade_frames=int(rate * EDGE_FADE_SECONDS))
        self.engine.set_render_output(render)
        # Capture and render are separate WASAPI streams: a short delay absorbs their jitter.
        self.engine.set_delay_frames(int(original_delay_ms * rate / 1000))
        self.endpoint = win.resolve_endpoint(output_device)
        self.silencer = win.SourceSilencer(source, duck_db=duck_db) if silence else None
        self.session = None
        self.initial_output = win.default_render_id() if self.endpoint is None else None
        self._last_health = 0.0
        self._last_capture = time.perf_counter()
        self._last_captured_frames = 0
        self._deadline = float("inf")

    def open(self) -> None:
        """Duck the source (journaled, with a guard for host crashes), then start the clients.
        Ducking first means the user never hears the original twice."""
        if self.silencer is not None:
            self.win.spawn_guard()
            self.silencer.refresh()
        gain = self.silencer.capture_gain if self.silencer is not None else 1.0
        self.session = self.win.WasapiSession(self.engine, self.source.root, render=self.render, capture_gain=gain,
                                              render_device_id=self.endpoint.id if self.endpoint else None)

    @property
    def output_device(self) -> str:
        return self.session.stats()["render_device"] if self.session else ""

    def start_clock(self, seconds: float | None) -> None:
        self._last_capture = time.perf_counter()
        if seconds:
            self._deadline = time.perf_counter() + seconds

    def deadline_passed(self) -> bool:
        return time.perf_counter() >= self._deadline

    def health_check(self) -> str | None:
        """Every 250 ms: source still running, output device unchanged, engine healthy,
        controlling app alive, and any newly opened source session ducked."""
        now = time.perf_counter()
        if now - self._last_health < 0.25:
            return None
        self._last_health = now
        win = self.win
        stats = self.session.stats()
        if stats["state"] != native.WASAPI_RUNNING:
            return f"audio engine stopped: {stats['message'] or 'unknown error'}; restart the session"
        if stats["capture_frames"] != self._last_captured_frames:
            self._last_captured_frames = stats["capture_frames"]
            self._last_capture = now
        if not any(win.is_alive(pid) for pid in self.source.members):
            return "source app exited; restart the session after reopening it"
        if self.parent_pid and not win.is_alive(self.parent_pid):
            return "controlling app exited"
        if self.initial_output is not None and win.default_render_id() != self.initial_output:
            return "output device changed; restart the session"
        if self.silencer is not None:
            self.silencer.refresh()
        return None

    def capture_stalled(self) -> bool:
        # Process loopback delivers silence while the app is quiet, so a stall is a real fault.
        return time.perf_counter() - self._last_capture >= 2.0

    def settle_gains(self, target: tuple[float, float], timeout_ms: int) -> None:
        """The device keeps rendering until the closing ramp lands, so speech still playing
        fades out instead of being cut off when the device stops."""
        deadline = time.perf_counter() + timeout_ms / 1000
        while time.perf_counter() < deadline:
            o, t = self.engine.gains
            if abs(o - target[0]) < 1e-3 and abs(t - target[1]) < 1e-3:
                return
            time.sleep(0.002)

    def stop(self) -> None:
        if self.session is not None:
            self._final_stats = self.session.stats()
            self.session.stop()
            self.session = None
        if self.silencer is not None:
            self.silencer.restore()

    def summary(self) -> dict:
        stats = getattr(self, "_final_stats", None) or (self.session.stats() if self.session else {})
        return {"exe": self.source.exe, "pids": sorted(self.source.members), "ducked_db": (
                    None if self.silencer is None else round(20 * __import__("math").log10(self.silencer.level), 1)),
                "foreign_volume_changes": self.silencer.foreign_changes if self.silencer else 0,
                "device_rate": self.sample_rate, "callbacks": self.engine.counter("callbacks"),
                "captured_frames": self.engine.counter("captured_frames"),
                "capture_overflow_frames": self.engine.capture.overflow_frames,
                "output_frames": self.engine.counter("output_frames"), "record_drops": self.engine.records_dropped,
                "output_device": stats.get("render_device", ""), "render_latency_ms": stats.get("render_latency_ms"),
                "render_full_wakeups": stats.get("render_full_wakeups"),
                "capture_discontinuities": stats.get("capture_discontinuities"),
                "capture_timestamp_errors": stats.get("capture_timestamp_errors")}


_ = PlaybackMode
