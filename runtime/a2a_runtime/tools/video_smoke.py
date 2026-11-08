"""Interactive delayed-picture smoke: captures only a synthetic Tk window, never other apps."""

from __future__ import annotations

import json
import tkinter as tk

from ..video.capture import DelayedCapture
from ..video.windows import enumerate_windows


def main() -> None:
    root = tk.Tk()
    root.title("A2A synthetic video smoke")
    root.geometry("480x270+80+80")
    label = tk.Label(root, text="A2A synthetic frame", bg="#2463eb", fg="white",
                     font=("Segoe UI", 24))
    label.pack(fill="both", expand=True)
    root.update()
    choice = next(item for item in enumerate_windows(include_own=True) if item.title == root.title())
    capture = DelayedCapture(choice.hwnd, delay_ms=200)
    result = {"ok": False}
    capture.start()
    started = 0

    def check():
        nonlocal started
        started += 1
        frame = capture.take_due()
        if frame is not None and float(frame.std()) > 1.0:
            result.update(ok=True, shape=list(frame.shape), capture=capture.stats)
            finish()
        elif capture.error:
            result["error"] = capture.error
            finish()
        elif started >= 100:
            result["error"] = "no synthetic frame arrived within five seconds"
            finish()
        else:
            root.after(50, check)

    def finish():
        capture.stop()
        root.destroy()

    root.after(50, check)
    root.mainloop()
    print(json.dumps(result, sort_keys=True))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
