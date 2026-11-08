"""Visible top-level window discovery for an explicit delayed-picture source."""

from __future__ import annotations

import ctypes as C
import ctypes.wintypes as W
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowChoice:
    hwnd: int
    title: str
    pid: int

    @property
    def label(self) -> str:
        return f"{self.title} (pid {self.pid})"


def enumerate_windows(include_own: bool = False) -> list[WindowChoice]:
    if os.name != "nt":
        return []
    user32 = C.WinDLL("user32", use_last_error=True)
    choices: list[WindowChoice] = []
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)

    @callback_type
    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        pid = W.DWORD()
        user32.GetWindowThreadProcessId(hwnd, C.byref(pid))
        if pid.value == os.getpid() and not include_own:
            return True
        buffer = C.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        title = buffer.value.strip()
        if title:
            choices.append(WindowChoice(int(hwnd), title, int(pid.value)))
        return True

    user32.EnumWindows(visit, 0)
    return sorted(choices, key=lambda choice: (choice.title.lower(), choice.pid, choice.hwnd))
