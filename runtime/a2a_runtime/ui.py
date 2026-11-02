"""A small Windows desktop client for the experimental portable pipeline."""

from __future__ import annotations

import argparse
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from .ui_session import PipelineProcess, SessionOptions, enumerate_devices, preflight, voice_progress_text

BG = "#0e1421"
INK = "#e9eef8"
MUTED = "#a4b1c7"
ACCENT = "#6595ff"
CARD = "#192235"
BORDER = "#344158"
CONTROL = "#27344a"
PRIMARY = "#3267d6"


class A2AWindow:
    def __init__(self, root: tk.Tk, *, enumerate_on_start: bool = True) -> None:
        self.root = root
        self.root.title("A2A — Live translation")
        self.root.geometry("960x770")
        self.root.minsize(820, 660)
        self.root.configure(bg=BG)
        self.root.option_add("*TCombobox*Listbox.background", CONTROL)
        self.root.option_add("*TCombobox*Listbox.foreground", INK)
        self.root.option_add("*TCombobox*Listbox.selectBackground", PRIMARY)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar(value="System default")
        self.direction = tk.StringVar(value="Mandarin → English")
        self.mode = tk.StringVar(value="voice")
        self.playback = tk.StringVar(value="translated")
        self.device = tk.StringVar(value="Automatic")
        self.status = tk.StringVar(value="Choose an application that is playing audio.")
        self.mode_hint = tk.StringVar()
        self.loading_started: float | None = None
        self.loading_phase = ""
        self.sources = []
        self.outputs = []
        self.session: PipelineProcess | None = None
        self.stopping = False
        self.closing = False
        self.discovery: queue.Queue = queue.Queue()
        self._styles()
        self._layout()
        self._update_controls()
        self.root.after(100, self._poll)
        if enumerate_on_start:
            self.root.after(50, self.refresh)

    def _styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TCombobox", padding=(8, 7), font=("Segoe UI", 10), fieldbackground=CONTROL,
                        background=CONTROL, foreground=INK, arrowcolor=MUTED, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER)
        style.map("TCombobox", fieldbackground=[("disabled", CARD), ("readonly", CONTROL)],
                  foreground=[("disabled", MUTED)], background=[("disabled", CARD), ("readonly", CONTROL)])
        style.configure("TProgressbar", background=ACCENT, troughcolor=CONTROL, borderwidth=0, thickness=4)
        style.configure("Vertical.TScrollbar", background=CONTROL, troughcolor=CARD, arrowcolor=MUTED,
                        bordercolor=CARD, lightcolor=CONTROL, darkcolor=CONTROL)

    def _label(self, parent, text: str, *, size=10, color=INK, weight="normal", bg=CARD, **kwargs):
        return tk.Label(parent, text=text, font=("Segoe UI", size, weight), fg=color, bg=bg, **kwargs)

    def _button(self, parent, text, command, *, primary=False, **kwargs):
        return tk.Button(parent, text=text, command=command, font=("Segoe UI", 10, "bold"),
                         bg=PRIMARY if primary else CONTROL, fg="white" if primary else INK,
                         activebackground="#4079ed" if primary else "#35445d",
                         activeforeground="white" if primary else INK, relief="flat", bd=0,
                         padx=17, pady=9, cursor="hand2", disabledforeground="#7f8ca1", **kwargs)

    def _section(self, parent, title: str, hint: str = ""):
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(fill="x", pady=(0, 12))
        inner = tk.Frame(card, bg=CARD)
        inner.pack(fill="both", padx=20, pady=15)
        self._label(inner, title, size=11, weight="bold", anchor="w").pack(fill="x")
        if hint:
            self._label(inner, hint, size=9, color=MUTED, anchor="w", justify="left").pack(fill="x", pady=(3, 0))
        return inner

    def _layout(self) -> None:
        outer = tk.Frame(self.root, bg=BG)
        outer.pack(fill="both", expand=True, padx=30, pady=22)
        heading = tk.Frame(outer, bg=BG)
        heading.pack(fill="x", pady=(0, 16))
        self._label(heading, "A2A", size=23, weight="bold", bg=BG).pack(side="left")
        self._label(heading, "LIVE TRANSLATION", size=9, color=ACCENT, weight="bold", bg=BG).pack(side="left", padx=12, pady=(8, 0))
        self._label(heading, "Windows preview", size=9, color=MUTED, bg=BG).pack(side="right")

        source = self._section(outer, "Audio source", "Play audio in an app, then choose its session below.")
        line = tk.Frame(source, bg=CARD)
        line.pack(fill="x", pady=(10, 0))
        self.source_box = ttk.Combobox(line, state="readonly", textvariable=self.source_var)
        self.source_box.pack(side="left", fill="x", expand=True)
        self.source_box.bind("<<ComboboxSelected>>", lambda _event: self._update_controls())
        self.refresh_button = self._button(line, "Refresh", self.refresh)
        self.refresh_button.pack(side="left", padx=(10, 0))

        settings = self._section(outer, "Translation")
        row = tk.Frame(settings, bg=CARD)
        row.pack(fill="x", pady=(10, 0))
        left = tk.Frame(row, bg=CARD)
        left.pack(side="left", fill="x", expand=True, padx=(0, 9))
        right = tk.Frame(row, bg=CARD)
        right.pack(side="left", fill="x", expand=True, padx=(9, 0))
        self._label(left, "Direction", size=9, color=MUTED, anchor="w").pack(fill="x", pady=(0, 5))
        self.direction_box = ttk.Combobox(left, state="readonly", textvariable=self.direction,
                                        values=("Mandarin → English", "English → Mandarin"))
        self.direction_box.pack(fill="x")
        self._label(right, "Output device", size=9, color=MUTED, anchor="w").pack(fill="x", pady=(0, 5))
        self.output_box = ttk.Combobox(right, state="readonly", textvariable=self.output_var,
                                     values=("System default",))
        self.output_box.pack(fill="x")

        row2 = tk.Frame(settings, bg=CARD)
        row2.pack(fill="x", pady=(12, 0))
        self.voice_radio = tk.Radiobutton(row2, text="Translated voice", variable=self.mode, value="voice",
                                          command=self._update_controls, bg=CARD, fg=INK, activebackground=CARD,
                                          selectcolor=CARD, disabledforeground=MUTED,
                                          font=("Segoe UI", 10), highlightthickness=0)
        self.voice_radio.pack(side="left")
        self.caption_radio = tk.Radiobutton(row2, text="Captions only", variable=self.mode, value="captions",
                                            command=self._update_controls, bg=CARD, fg=INK, activebackground=CARD,
                                            selectcolor=CARD, disabledforeground=MUTED,
                                            font=("Segoe UI", 10), highlightthickness=0)
        self.caption_radio.pack(side="left", padx=15)
        self._label(row2, "Compute", size=9, color=MUTED).pack(side="left", padx=(20, 7))
        self.device_box = ttk.Combobox(row2, state="readonly", width=12, textvariable=self.device,
                                     values=("Automatic", "NVIDIA GPU", "CPU"))
        self.device_box.pack(side="left")
        self._label(settings, "", textvariable=self.mode_hint, size=9, color=MUTED, anchor="w").pack(fill="x", pady=(8, 0))

        actions = tk.Frame(outer, bg=BG)
        actions.pack(fill="x", pady=(0, 8))
        self.start_button = self._button(actions, "Start translation", self.start, primary=True)
        self.start_button.pack(side="left")
        self.stop_button = self._button(actions, "Stop", self.stop)
        self.stop_button.pack(side="left", padx=(8, 0))
        self._label(actions, "Listen to", size=9, color=MUTED, bg=BG).pack(side="left", padx=(24, 7))
        self.listen_box = ttk.Combobox(actions, state="readonly", width=13, textvariable=self.playback,
                                     values=("translated", "original"))
        self.listen_box.pack(side="left")
        self.listen_box.bind("<<ComboboxSelected>>", self.set_playback)

        statusline = tk.Frame(outer, bg=BG)
        statusline.pack(fill="x", pady=(0, 8))
        self._label(statusline, "●", color=ACCENT, size=10, bg=BG).pack(side="left")
        self._label(statusline, "", textvariable=self.status, color=MUTED, size=9, bg=BG,
                    anchor="w").pack(side="left", fill="x", expand=True, padx=(7, 0))
        self.progress = ttk.Progressbar(outer, mode="indeterminate", style="TProgressbar")
        self.progress.pack(fill="x", pady=(0, 12))

        caption_card = tk.Frame(outer, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        caption_card.pack(fill="both", expand=True)
        head = tk.Frame(caption_card, bg=CARD)
        head.pack(fill="x", padx=20, pady=(12, 4))
        self._label(head, "Live captions", size=11, weight="bold").pack(side="left")
        self._label(head, "Cleared when the session stops", size=9, color=MUTED).pack(side="right")
        caption_frame = tk.Frame(caption_card, bg=CARD)
        caption_frame.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        scroll = ttk.Scrollbar(caption_frame)
        scroll.pack(side="right", fill="y")
        self.captions = tk.Text(caption_frame, wrap="word", bg=CARD, fg=INK, bd=0, relief="flat",
                                font=("Segoe UI", 11), cursor="arrow", yscrollcommand=scroll.set,
                                state="disabled", highlightthickness=0, spacing3=7)
        self.captions.pack(fill="both", expand=True)
        scroll.config(command=self.captions.yview)
        self.captions.tag_configure("source", foreground=MUTED)
        self.captions.tag_configure("target", foreground=INK, font=("Segoe UI", 11, "bold"))

    def refresh(self) -> None:
        if self.session or self.closing:
            return
        self.refresh_button.config(state="disabled")
        self.status.set("Looking for audio sessions…")

        def work():
            try:
                self.discovery.put((enumerate_devices(), None))
            except Exception as error:
                self.discovery.put((None, str(error)))

        threading.Thread(target=work, daemon=True).start()

    def _apply_discovery(self, data, error) -> None:
        self.refresh_button.config(state="normal" if not self.session else "disabled")
        if error:
            self.status.set("Could not list audio sources: " + error)
            return
        old_source = self.sources[self.source_box.current()].pid if 0 <= self.source_box.current() < len(self.sources) else None
        old_output = self.outputs[self.output_box.current()].id if 0 <= self.output_box.current() < len(self.outputs) else ""
        self.sources, self.outputs = data
        self.source_box["values"] = [s.label for s in self.sources]
        self.output_box["values"] = [o.name for o in self.outputs]
        selected = next((i for i, s in enumerate(self.sources) if s.pid == old_source), -1)
        if selected >= 0:
            self.source_box.current(selected)
        else:
            self.source_var.set("")
        self.output_box.current(next((i for i, o in enumerate(self.outputs) if o.id == old_output), 0))
        self.status.set("Select an application, then start translation." if self.sources
                        else "No audio sessions found. Start playback in an app and press Refresh.")
        self._update_controls()

    def _update_controls(self) -> None:
        busy = self.session is not None
        for box in (self.source_box, self.direction_box, self.output_box, self.device_box):
            box.configure(state="disabled" if busy else "readonly")
        for radio in (self.voice_radio, self.caption_radio):
            radio.configure(state="disabled" if busy else "normal")
        valid_source = 0 <= self.source_box.current() < len(self.sources)
        self.start_button.configure(state="disabled" if busy or not valid_source else "normal")
        self.stop_button.configure(state="normal" if busy and not self.stopping else "disabled")
        self.refresh_button.configure(state="disabled" if busy else "normal")
        self.listen_box.configure(state="disabled" if self.mode.get() == "captions" or self.stopping else "readonly")
        self.mode_hint.set("Captions only: original audio stays on; no translated speech is played."
                           if self.mode.get() == "captions" else
                           "Translated voice: initial silence while voices are prepared. Speaker consent is required.")

    def start(self) -> None:
        index = self.source_box.current()
        if self.session or not (0 <= index < len(self.sources)):
            return
        captions_only = self.mode.get() == "captions"
        try:
            preflight(captions_only)
            output_index = self.output_box.current()
            output = self.outputs[output_index].id if 0 <= output_index < len(self.outputs) else ""
            options = SessionOptions(pid=self.sources[index].pid,
                                     direction="zh-en" if self.direction.get().startswith("Mandarin") else "en-zh",
                                     captions_only=captions_only, output_id=output, playback=self.playback.get(),
                                     device={"Automatic": "auto", "NVIDIA GPU": "cuda", "CPU": "cpu"}[self.device.get()])
            self.session = PipelineProcess(options)
        except Exception as error:
            self.status.set("Unable to start. Check setup and try again.")
            messagebox.showerror("Cannot start A2A", str(error), parent=self.root)
            return
        self._clear_captions()
        self.stopping = False
        self.loading_started = time.monotonic()
        self.loading_phase = "Loading local models"
        self.status.set("Loading local models. This can take a few minutes…")
        self.progress.start(12)
        self._update_controls()

    def set_playback(self, _event=None) -> None:
        if self.session and not self.stopping and self.mode.get() != "captions":
            self.session.send(self.playback.get())
            if self.loading_started is None:
                self.status.set(self._live_status())

    def stop(self) -> None:
        if not self.session or self.stopping:
            return
        self.stopping = True
        self.loading_started = None
        self.status.set("Stopping and restoring source audio…")
        self.session.stop()
        self._clear_captions()
        self._update_controls()

    def close(self) -> None:
        if self.session:
            self.closing = True
            self.stop()
        else:
            self.root.destroy()

    def _clear_captions(self) -> None:
        self.captions.configure(state="normal")
        self.captions.delete("1.0", "end")
        self.captions.configure(state="disabled")

    def _caption(self, event: dict) -> None:
        kind, text = event.get("caption", ""), event.get("text", "")
        if not isinstance(text, str) or not text or kind not in ("source.final", "target.final"):
            return
        self.captions.configure(state="normal")
        self.captions.insert("end", text + "\n", "target" if kind == "target.final" else "source")
        # Captions are session-only and bounded even for a long-running session.
        if int(self.captions.index("end-1c").split(".")[0]) > 200:
            self.captions.delete("1.0", "51.0")
        self.captions.see("end")
        self.captions.configure(state="disabled")

    def _live_status(self) -> str:
        if self.mode.get() == "captions":
            return "Captions live · Original audio remains on."
        if self.playback.get() == "original":
            return "Listening to original audio · Translation continues."
        return "Listening to translated voice · Waiting for each speaker's voice if needed."

    def _status_update(self, message: str) -> None:
        if message.startswith("starting the translation worker"):
            self.loading_phase = "Loading translation model"
        elif message.startswith("starting the speech recognition worker"):
            self.loading_phase = "Loading speech recognition model"
        elif message.startswith("starting the") and "voice worker" in message:
            self.loading_phase = "Loading voice model"
        elif message.startswith("live:"):
            self.loading_started = None
            self.progress.stop()
            self.progress["value"] = 0
            self.status.set(self._live_status())
        elif message.startswith("voice profile"):
            self.status.set("Voice prepared · Translation is running.")
        elif self.loading_started is None:
            self.status.set(message)

    def _poll(self) -> None:
        try:
            while True:
                data, error = self.discovery.get_nowait()
                self._apply_discovery(data, error)
        except queue.Empty:
            pass
        if self.session:
            try:
                while True:
                    kind, value = self.session.events.get_nowait()
                    if self.stopping:
                        continue
                    if kind == "status":
                        self._status_update(str(value))
                    elif kind == "event":
                        if value.get("caption"):
                            self._caption(value)
                        elif value.get("voice_status"):
                            self.status.set(voice_progress_text(value))
                        elif value.get("ev") == "error":
                            self.status.set("Audio or model error: " + str(value.get("message", "unknown error")))
            except queue.Empty:
                pass
            code = self.session.poll()
            if code is not None:
                self.progress.stop()
                self.loading_started = None
                self._clear_captions()
                failure = self.session.last_failure
                self.session = None
                self.status.set("Session stopped." if code == 0 else
                                failure or "Session ended with an error. Check the source volume and model setup.")
                self.stopping = False
                if self.closing:
                    self.root.destroy()
                    return
                self._update_controls()
            elif self.loading_started is not None and not self.stopping:
                elapsed = int(time.monotonic() - self.loading_started)
                self.status.set(f"{self.loading_phase}… {elapsed}s · First startup may take a few minutes.")
        self.root.after(100, self._poll)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true", help="open and close the window without capture or models")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("The Windows desktop UI requires Windows; use the native A2A app on macOS.")
    root = tk.Tk()
    window = A2AWindow(root, enumerate_on_start=not args.smoke)
    if args.smoke:
        # Exercise the caption widget without touching audio devices or model workers.
        from types import SimpleNamespace

        assert root.cget("background") == BG and window.captions.cget("background") == CARD
        window._caption({"caption": "target.final", "text": "Synthetic caption."})
        assert "Synthetic caption." in window.captions.get("1.0", "end")
        window._clear_captions()
        assert not window.captions.get("1.0", "end").strip()
        window.session = SimpleNamespace(events=queue.Queue(), last_failure="Synthetic failure", poll=lambda: 1)
        window._poll()
        assert window.status.get() == "Synthetic failure"
        root.after(500, root.destroy)
    root.mainloop()
    if args.smoke and sys.stdout is not None:
        print("Windows UI smoke passed")


if __name__ == "__main__":
    main()
