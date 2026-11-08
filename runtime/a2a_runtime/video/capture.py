"""Windows Graphics Capture -> bounded in-memory JPEG delay line.

No frame is written to disk or included in diagnostics. Capture callbacks run on the video
worker, never the audio RT thread. JPEG frames are independent keyframes, so evicting old
frames cannot break a later decode. The host clock is shared with the audio driver.
"""

from __future__ import annotations

import threading

import numpy as np

from .. import native
from .queue import DelayedFrame, DelayedFrameQueue


class DelayedCapture:
    def __init__(self, hwnd: int, delay_ms: int, fine_tune_ms: int = 0,
                 max_bytes: int = 128 * 1024 * 1024, clock=native.now_ns) -> None:
        if delay_ms < 0:
            raise ValueError("delay_ms must be nonnegative")
        self.hwnd = hwnd
        self.offset_ns = (delay_ms + fine_tune_ms) * 1_000_000
        self.clock = clock
        self.queue = DelayedFrameQueue(max_bytes)
        self._lock = threading.Lock()
        self._control = None
        self._error: str | None = None
        self._last: np.ndarray | None = None

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def stats(self) -> dict:
        with self._lock:
            return {"frames": self.queue.count, "bytes": self.queue.bytes,
                    "dropped_frames": self.queue.dropped_frames, "span_seconds": self.queue.span_seconds}

    def start(self) -> None:
        if self._control is not None:
            return
        try:
            import cv2
            from windows_capture import WindowsCapture
        except ImportError as error:
            raise RuntimeError("Windows video needs uv sync --project runtime --locked --extra video") from error
        capture = WindowsCapture(cursor_capture=False, draw_border=False, secondary_window=False,
                                 minimum_update_interval=15, dirty_region=False, window_hwnd=self.hwnd)

        @capture.event
        def on_frame_arrived(frame, _control):
            try:
                pixels = frame.frame_buffer
                if pixels.ndim != 3 or pixels.shape[2] != 4:
                    raise RuntimeError("unsupported Windows capture pixel format")
                rgb = cv2.cvtColor(pixels, cv2.COLOR_BGRA2RGB)
                ok, encoded = cv2.imencode(".jpg", rgb, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if not ok:
                    raise RuntimeError("could not compress captured frame")
                item = DelayedFrame(self.clock() + self.offset_ns, len(encoded), True, encoded.tobytes())
                with self._lock:
                    self.queue.push(item)
            except Exception as error:
                self._error = str(error)
                _control.stop()

        @capture.event
        def on_closed():
            self._error = "the captured window closed or stopped sharing frames"

        self._control = capture.start_free_threaded()

    def take_due(self) -> np.ndarray | None:
        """Latest frame due on the shared host clock; decoding happens on the UI thread."""
        due = None
        with self._lock:
            while True:
                frame = self.queue.peek_due(self.clock())
                if frame is None:
                    break
                due = self.queue.pop_first()
        if due is None:
            return self._last
        import cv2

        pixels = cv2.imdecode(np.frombuffer(due.payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if pixels is not None:
            self._last = cv2.cvtColor(pixels, cv2.COLOR_BGR2RGB)
        return self._last

    def stop(self) -> None:
        control, self._control = self._control, None
        if control is not None:
            control.stop()
            control.wait()
        with self._lock:
            self.queue.clear()
        self._last = None
