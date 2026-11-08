"""Tk presentation of delayed frames. No capture, timing, or playout policy lives here."""

from __future__ import annotations

import tkinter as tk


class DelayedPictureWindow:
    def __init__(self, parent: tk.Tk, capture, on_error=None) -> None:
        self.capture = capture
        self.on_error = on_error
        self.closed = False
        self._reported = False
        self._photo = None
        self.window = tk.Toplevel(parent)
        self.window.title("A2A — Delayed picture")
        self.window.geometry("960x600")
        self.window.minsize(480, 300)
        self.window.configure(bg="black")
        self.window.protocol("WM_DELETE_WINDOW", self.stop)
        self.label = tk.Label(self.window, bg="black", fg="#a4b1c7",
                              text="Waiting for the delayed picture…", font=("Segoe UI", 12))
        self.label.pack(fill="both", expand=True)
        capture.start()
        self.window.after(15, self._present)

    def _present(self) -> None:
        if self.closed:
            return
        if self.capture.error and not self._reported:
            self._reported = True
            if self.on_error:
                self.on_error(self.capture.error)
        frame = self.capture.take_due()
        if frame is not None:
            import cv2
            from PIL import Image, ImageTk

            width = max(1, self.label.winfo_width())
            height = max(1, self.label.winfo_height())
            scale = min(width / frame.shape[1], height / frame.shape[0], 1.0)
            if scale < 1.0:
                frame = cv2.resize(frame, (max(1, int(frame.shape[1] * scale)),
                                           max(1, int(frame.shape[0] * scale))), interpolation=cv2.INTER_AREA)
            self._photo = ImageTk.PhotoImage(Image.fromarray(frame))
            self.label.configure(image=self._photo, text="")
        self.window.after(15, self._present)

    def stop(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.capture.stop()
        self._photo = None
        self.window.destroy()
