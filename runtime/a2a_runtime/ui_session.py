"""Windows desktop session control. The audio and policy remain in the pipeline process."""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import REPO_ROOT, native
from .models.base import AdapterError
from .workers.model_paths import installed_model


@dataclass(frozen=True)
class SourceChoice:
    pid: int
    name: str
    active: bool
    # The whole output mix except A2A itself: the Mac app's only source (`--tap-global`).
    global_mix: bool = False

    @property
    def label(self) -> str:
        if self.global_mix:
            return "System audio  ·  everything this PC plays except A2A"
        return f"{self.name}  ·  PID {self.pid}" + ("  ·  active" if self.active else "")


SYSTEM_AUDIO = SourceChoice(0, "System audio", True, global_mix=True)


@dataclass(frozen=True)
class OutputChoice:
    id: str
    name: str


@dataclass(frozen=True)
class SessionOptions:
    pid: int
    direction: str = "zh-en"
    captions_only: bool = False
    output_id: str = ""
    playback: str = "translated"
    device: str = "auto"
    global_mix: bool = False
    # "clone": each speaker's own voice (research-only model); "system": a Windows voice per speaker.
    voice: str = "clone"
    # Whole phrases before playback, with a 20 s debt and lag budget (the Mac app's toggle).
    quality_first: bool = False
    # Delayed Windows Graphics Capture, in milliseconds; 0 disables the picture window.
    picture_delay_ms: int = 0
    video_hwnd: int | None = None
    background: bool = False


def choices_from_sessions(sessions, processes, own_pid: int) -> list[SourceChoice]:
    """Group audio sessions under the topmost same-executable process (e.g. browsers)."""
    by_pid = {p.pid: p for p in processes}
    grouped: dict[int, SourceChoice] = {}
    for session in sessions:
        p = by_pid.get(session.pid)
        if not p or p.pid == own_pid or not p.exe or session.state == "expired":
            continue
        visited = set()
        while p.ppid in by_pid and p.pid not in visited:
            visited.add(p.pid)
            parent = by_pid[p.ppid]
            if parent.pid == own_pid or parent.exe.lower() != p.exe.lower():
                break
            p = parent
        if p.pid == own_pid:
            continue
        old = grouped.get(p.pid)
        grouped[p.pid] = SourceChoice(p.pid, p.exe, session.state == "active" or bool(old and old.active))
    return sorted(grouped.values(), key=lambda p: (not p.active, p.name.lower(), p.pid))


def enumerate_devices() -> tuple[list[SourceChoice], list[OutputChoice]]:
    from .audio import windows as win

    sessions, processes = win.audio_sessions(), win.processes()
    sources = [SYSTEM_AUDIO] + choices_from_sessions(sessions, processes, os.getpid())
    outputs = [OutputChoice("", "System default")]
    endpoints = win.endpoints()
    for e in endpoints:
        duplicate = sum(other.name == e.name for other in endpoints) > 1
        name = f"{e.name} · {e.id[-9:-1]}" if duplicate else e.name
        outputs.append(OutputChoice(e.id, name))
    return sources, outputs


def preflight(captions_only: bool, voice: str = "clone", picture: bool = False, background: bool = False) -> None:
    if sys.platform != "win32":
        raise RuntimeError("This desktop interface requires Windows. On macOS, use the A2A app.")
    if not native.library_path().exists():
        raise RuntimeError("The Windows audio engine is not built. Run: uv run --project runtime python runtime/native/build.py")
    if picture:
        missing_video = [name for name in ("windows_capture", "cv2", "PIL") if importlib.util.find_spec(name) is None]
        if missing_video:
            raise RuntimeError("Missing delayed-picture dependencies: " + ", ".join(missing_video)
                               + ". Run: uv sync --project runtime --locked --extra video")
    if background and not (REPO_ROOT / "runtime" / "separator" / ".venv" / "Scripts" / "python.exe").is_file():
        raise RuntimeError("Background audio needs: uv sync --project runtime/separator --locked")
    dependencies = ["torch", "faster_whisper", "ctranslate2", "transformers"]
    clone = not captions_only and voice == "clone"
    if clone:
        dependencies.append("omnivoice")
    missing = [name for name in dependencies if importlib.util.find_spec(name) is None]
    if missing:
        raise RuntimeError("Missing inference dependencies: " + ", ".join(missing)
                           + ". Run: uv sync --project runtime --locked --extra gpu")
    try:
        installed_model("faster-whisper-large-v3", "asr")
        installed_model("qwen3-4b-instruct-2507", "mt")
        if clone:
            installed_model("omnivoice-0.6b-torch", "tts")
    except AdapterError as error:
        raise RuntimeError(str(error)) from error


def pipeline_command(options: SessionOptions, trace_path: Path, parent_pid: int | None = None) -> list[str]:
    if (not options.global_mix and options.pid <= 0) or options.direction not in ("zh-en", "en-zh"):
        raise ValueError("Select a valid audio source and translation direction")
    if options.device not in ("auto", "cuda", "cpu") or options.playback not in ("translated", "original"):
        raise ValueError("Invalid device or playback selection")
    if options.voice not in ("clone", "system"):
        raise ValueError("Invalid voice selection")
    if options.picture_delay_ms and (options.captions_only or not options.video_hwnd
                                     or not 1_000 <= options.picture_delay_ms <= 30_000):
        raise ValueError("Delayed picture needs a source window and a 1–30 second delay")
    source = ["--tap-global"] if options.global_mix else ["--tap-pid", str(options.pid)]
    cmd = [sys.executable, "-u", "-m", "a2a_runtime.cli", *source,
           "--direction", options.direction, "--asr", "whisper", "--mt", "llm", "--device", options.device,
           "--events-stdout", "--control-stdin", "--trace", str(trace_path), "--tail-seconds", "3",
           "--parent-pid", str(parent_pid if parent_pid is not None else os.getpid())]
    if options.captions_only:
        cmd += ["--captions-only", "--tts", "tone"]
        return cmd
    clone = options.voice == "clone"
    # The Mac app's session (SessionModel.pipelineArguments), with this host's models.
    cmd += (["--tts", "clone", "--clone-model", "omnivoice", "--clone-backend", "torch"] if clone else ["--tts", "system"])
    cmd += ["--translation-only", "--fluent", "--speakers", "auto", "--playback", options.playback]
    if options.picture_delay_ms:
        cmd += ["--presentation-delay-ms", str(options.picture_delay_ms)]
    if options.background:
        cmd.append("--background")
    if options.quality_first:
        cmd.append("--quality-first")
    else:
        cmd += ["--adaptive-prebuffer", "--max-debt-seconds", "8", "--max-source-lag-seconds", "20" if clone else "8"]
    if options.output_id:
        cmd += ["--output-device", options.output_id]
    return cmd


def trace_path() -> Path:
    path = REPO_ROOT / ".local" / "runs" / "windows-ui"
    path.mkdir(parents=True, exist_ok=True)
    return path / f"{uuid.uuid4().hex}.jsonl"


class LoadingStages:
    """Which models are still loading, from the pipeline's fixed start-up lines. The stages
    load together, so the status names every stage still pending rather than the last one
    that started."""

    ORDER = ("speech recognition", "translation", "voice")
    STARTS = (("starting the speech recognition worker", "speech recognition"),
              ("starting the translation worker", "translation"), ("starting Hy-SimulMT worker", "translation"),
              ("starting the Windows system voice", "voice"))
    READY = (("recognizer ready", "speech recognition"), ("translator ready", "translation"),
             ("Hy-SimulMT worker ready", "translation"), ("voice worker ready", "voice"),
             ("system voice ready", "voice"))

    def __init__(self) -> None:
        self.pending: set[str] = set()
        self.started = False

    def update(self, message: str) -> bool:
        """True when `message` was a start-up line."""
        for prefix, stage in self.STARTS:
            if message.startswith(prefix):
                self.pending.add(stage)
                self.started = True
                return True
        if message.startswith("starting the") and "voice worker" in message:
            self.pending.add("voice")
            self.started = True
            return True
        for prefix, stage in self.READY:
            if message.startswith(prefix):
                self.pending.discard(stage)
                return True
        return False

    @property
    def text(self) -> str:
        names = [stage for stage in self.ORDER if stage in self.pending]
        if not names:
            return "Starting audio" if self.started else "Loading local models"
        if len(names) == 1:
            return f"Loading {names[0]} model"
        return "Loading " + ", ".join(names[:-1]) + (", and " if len(names) > 2 else " and ") + names[-1] + " models"


def voice_progress_text(event: dict) -> str:
    if event.get("voice_status") == "preparing":
        return "Preparing the speaker's voice…"
    try:
        seconds = max(0.0, float(event.get("seconds", 0)))
        required = max(0.0, float(event.get("required_seconds", 3)))
    except (TypeError, ValueError):
        return "Waiting for clear speech to prepare a voice."
    reason = {
        "no_timing": "waiting for timed speech",
        "mixed_speakers": "waiting for one speaker at a time",
        "unlabeled": "waiting for a clear speaker match",
        "expired": "recognition is behind; waiting for a newer phrase",
        "not_voice": "isolated speech is unavailable; try turning off Keep music and effects",
        "quiet": "waiting for clearer audio",
        "clipped": "audio is clipped; lower the source volume",
        "too_long": "waiting for a shorter phrase",
        "too_short": "waiting for a longer phrase",
    }.get(event.get("reason"))
    status = f"Collecting voice sample: {seconds:.1f} / {required:.1f} seconds"
    return status + (f" · {reason}" if reason else "")


def diagnostic_message(line: str) -> str | None:
    """Recognize fixed diagnostics without displaying arbitrary model output or transcripts."""
    lower = line.lower()
    if any(s in lower for s in ("cuda out of memory", "cuda_error_out_of_memory", "cublas_status_alloc_failed")):
        return "GPU memory exhausted. Stop other GPU applications or select Automatic."
    if any(s in lower for s in ("could not load library cudnn", "cudnn_status", "cublas64_")):
        return "NVIDIA inference libraries failed to load. Check the CUDA runtime installation."
    if line.startswith("a2a-pipeline:"):
        for label in ("recognition", "translation", "voice worker"):
            if line.removeprefix("a2a-pipeline:").strip().startswith(label + " stopped"):
                return f"The {label} stopped unexpectedly. Try Automatic compute and check GPU memory."
    return None


class PipelineProcess:
    """Readers drain both pipes away from Tk. Events are bounded and carry no disk output."""

    def __init__(self, options: SessionOptions) -> None:
        self.events: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=512)
        self.last_failure: str | None = None
        self.process = subprocess.Popen(pipeline_command(options, trace_path()), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(REPO_ROOT),
                                        creationflags=0x08000000 if sys.platform == "win32" else 0)
        self._lock = threading.Lock()
        for stream, kind in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr")):
            threading.Thread(target=self._read, args=(stream, kind), daemon=True).start()

    def _offer(self, kind: str, item: object) -> None:
        try:
            self.events.put_nowait((kind, item))
        except queue.Full:
            pass

    def _read(self, stream, kind: str) -> None:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if kind == "stdout":
                try:
                    event = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(event, dict) and (event.get("caption") or event.get("voice_status") or event.get("ev") in
                                                ("state.change", "fallback.enter", "fallback.exit", "error")):
                    self._offer("event", event)
            else:
                # Model libraries print progress with carriage returns, sometimes sharing
                # a line with a later pipeline status. Do not lose the status in that output.
                for part in line.split("\r"):
                    diagnostic = diagnostic_message(part)
                    if diagnostic and (self.last_failure is None or diagnostic.startswith(("GPU memory", "NVIDIA"))):
                        self.last_failure = diagnostic
                    if part.startswith("a2a-pipeline:"):
                        self._offer("status", part.removeprefix("a2a-pipeline:").strip())

    def send(self, command: str) -> bool:
        if command not in ("stop", "original", "translated"):
            raise ValueError("unknown session control")
        with self._lock:
            if self.process.poll() is not None:
                return False
            try:
                self.process.stdin.write((command + "\n").encode("ascii"))
                self.process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def stop(self) -> None:
        self.send("stop")

    def poll(self) -> int | None:
        return self.process.poll()
