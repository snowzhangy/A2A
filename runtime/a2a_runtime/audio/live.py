"""Live tap host for the portable driver: the Windows counterpart of A2AAudio.TapSession as
the pipeline uses it (ADR-0007).

One engine (stereo, 48 kHz) is shared by the WASAPI engine thread and the driver: the RT
thread captures and renders, the driver reads the capture ring and writes the translated
ring (each ring single-producer/single-consumer). The source is ducked rather than muted
(session mute also silences process-loopback capture), and the capture gain restores it.
"""

from __future__ import annotations

import math
import sys
import threading
import time

from .. import native
from ..core.pipeline import PlaybackMode

EDGE_FADE_SECONDS = 0.005


class LiveTap:
    sample_rate = 48_000
    channels = 2

    def __init__(self, source, *, render: bool, silence: bool, duck_db: float, output_device: str | None,
                 parent_pid: int | None, translated_ring_seconds: float = 30.0, rendered_ring_seconds: float = 0.0,
                 original_delay_ms: float = 30.0, max_delay_ms: float = 200.0) -> None:
        if sys.platform != "win32":
            raise SystemExit("a2a-pipeline (portable): live capture is implemented for Windows; on macOS use "
                             "apple/ a2a-pipeline --tap-bundle")
        from . import windows as win

        self.win = win
        self.source = source
        # The whole output mix: this runtime's own tree is excluded rather than one app captured.
        self.global_mix = bool(getattr(source, "global_mix", False))
        self.render = render
        self.parent_pid = parent_pid
        rate = self.sample_rate
        # A picture delay (ADR-0012) delays the original bus by D, so the delay line holds D + 1 s.
        max_delay_frames = int(max(max_delay_ms, original_delay_ms) * rate / 1000)
        self.engine = native.Engine(channels=self.channels, capture_ring_frames=rate * 4, max_delay_frames=max_delay_frames,
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
        self._discontinuity: str | None = None
        # Written by the watcher thread, read by the driver (single assignments).
        self._source_alive = True
        self._parent_alive = True
        self._default_output: str | None = None
        self._watch_failure: str | None = None
        self._watch_stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self.device_changes = 0
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
        self.session = self.win.WasapiSession(self.engine, self.source.root, exclude_tree=self.global_mix,
                                              render=self.render, capture_gain=gain,
                                              render_device_id=self.endpoint.id if self.endpoint else None)
        self._watcher = threading.Thread(target=self._watch, name="a2a.tap-watch", daemon=True)
        self._watcher.start()

    def _watch(self) -> None:
        """Process and session enumeration costs about 70 ms (a Toolhelp snapshot of every
        process, then every endpoint's audio sessions), so it runs here and not on the driver
        thread, which must keep draining the 4 s capture ring: polled there every 100 ms, it
        once slowed the driver to half speed and the ring overflowed. Newly opened source
        sessions are ducked within about 100 ms (a new stream is audible until then); the
        source, the controlling app, and the default output are checked every 250 ms."""
        win = self.win
        last_health = 0.0
        while True:
            started = time.perf_counter()
            try:
                if self.silencer is not None:
                    self.silencer.refresh()
                if started - last_health >= 0.25:
                    last_health = started
                    # The whole mix has no source app that could exit.
                    self._source_alive = self.global_mix or any(win.is_alive(pid) for pid in self.source.members)
                    self._parent_alive = not self.parent_pid or win.is_alive(self.parent_pid)
                    if self.initial_output is not None:
                        self._default_output = win.default_render_id()
            except Exception as error:  # reported by the next health check, which stops the session
                self._watch_failure = f"{type(error).__name__}: {error}"
                return
            if self._watch_stop.wait(max(0.0, 0.1 - (time.perf_counter() - started))):
                return

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
        """Every 250 ms, check the engine and the watcher's findings (the source, the
        controlling app, the default output device). Cheap enough for every driver read.
        Returns a reason to stop, or None."""
        now = time.perf_counter()
        if now - self._last_health < 0.25:
            return None
        self._last_health = now
        if self._watch_failure:
            return f"audio session watch failed ({self._watch_failure}); restart the session"
        stats = self.session.stats()
        if stats["state"] != native.WASAPI_RUNNING:
            return f"audio engine stopped: {stats['message'] or 'unknown error'}; restart the session"
        if stats["capture_frames"] != self._last_captured_frames:
            self._last_captured_frames = stats["capture_frames"]
            self._last_capture = now
        if not self._source_alive:
            return "source app exited; restart the session after reopening it"
        if not self._parent_alive:
            return "controlling app exited"
        if self.initial_output is not None:
            current = self._default_output
            if current is not None and current != self.initial_output:
                if not current:
                    return "no output device; restart the session"
                return self._follow_default(current)
        return None

    def _follow_default(self, device_id: str) -> str | None:
        """The default output changed (headphones connected, Bluetooth reconnected): restart
        both clients on the same engine and report a discontinuity, so the reducer cancels
        in-flight units and re-arms instead of the session ending."""
        old = self.session
        self._final_stats = old.stats()
        old.stop()
        try:
            self.session = self.win.WasapiSession(self.engine, self.source.root, exclude_tree=self.global_mix,
                                                  render=self.render,
                                                  capture_gain=self.silencer.capture_gain if self.silencer else 1.0)
        except OSError as error:
            self.session = None
            return f"output device changed and could not be reopened ({error}); restart the session"
        self.initial_output = device_id
        self.device_changes += 1
        self._discontinuity = f"output device changed to {self.output_device}"
        self._last_capture = time.perf_counter()
        return None

    def take_discontinuity(self) -> str | None:
        reason, self._discontinuity = self._discontinuity, None
        return reason

    def capture_stalled(self) -> bool:
        # Process loopback delivers silence while the app is quiet, so a stall is a real fault.
        # The whole mix may legitimately go without frames while nothing plays (a paused video);
        # a stopped engine is reported by the health check instead.
        if self.global_mix:
            return False
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
        if getattr(self, "_watcher", None) is not None:
            # Join before restoring, so a refresh in progress cannot duck again afterwards.
            self._watch_stop.set()
            self._watcher.join(timeout=2.0)
            self._watcher = None
        if getattr(self, "session", None) is not None:
            self._final_stats = self.session.stats()
            self.session.stop()
            self.session = None
        if self.silencer is not None:
            self.silencer.restore()

    def summary(self) -> dict:
        stats = getattr(self, "_final_stats", None) or (self.session.stats() if self.session else {})
        # `muted` keeps live_check's meaning (the source is not heard directly): on Windows the
        # session is ducked, because muting it would also silence process-loopback capture.
        return {"exe": self.source.exe, "global": self.global_mix, "pids": sorted(self.source.members),
                "muted": self.silencer is not None,
                "silencing": "session duck" if self.silencer is not None else "none", "ducked_db": (
                    None if self.silencer is None else round(20 * math.log10(self.silencer.level), 1)),
                "foreign_volume_changes": self.silencer.foreign_changes if self.silencer else 0,
                "output_device_changes": self.device_changes,
                "device_rate": self.sample_rate, "callbacks": self.engine.counter("callbacks"),
                "captured_frames": self.engine.counter("captured_frames"),
                "capture_overflow_frames": self.engine.capture.overflow_frames,
                "output_frames": self.engine.counter("output_frames"), "record_drops": self.engine.records_dropped,
                "output_device": stats.get("render_device", ""), "render_latency_ms": stats.get("render_latency_ms"),
                "render_full_wakeups": stats.get("render_full_wakeups"),
                "capture_discontinuities": stats.get("capture_discontinuities"),
                "capture_timestamp_errors": stats.get("capture_timestamp_errors")}


_ = PlaybackMode
