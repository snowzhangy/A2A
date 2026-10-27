"""The desktop client must control the real pipeline without persisting captions."""

import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from a2a_runtime import REPO_ROOT
from a2a_runtime.core.direction import TranslationLanguage
from a2a_runtime.models.base import AdapterError
from a2a_runtime.ui_session import (SessionOptions, choices_from_sessions, diagnostic_message, pipeline_command,
                                    voice_progress_text)
from a2a_runtime.workers import speech


def test_audio_sessions_resolve_to_browser_root_and_are_deduplicated():
    processes = [SimpleNamespace(pid=1, ppid=0, exe="chrome.exe"),
                 SimpleNamespace(pid=2, ppid=1, exe="chrome.exe"),
                 SimpleNamespace(pid=3, ppid=0, exe="player.exe"),
                 SimpleNamespace(pid=8, ppid=0, exe="python.exe")]
    sessions = [SimpleNamespace(pid=2, state="active"), SimpleNamespace(pid=1, state="inactive"),
                SimpleNamespace(pid=3, state="inactive"), SimpleNamespace(pid=8, state="active")]
    sources = choices_from_sessions(sessions, processes, own_pid=8)
    assert [(s.pid, s.name, s.active) for s in sources] == [(1, "chrome.exe", True), (3, "player.exe", False)]


def test_caption_and_voice_session_commands_preserve_control_and_privacy(tmp_path):
    from a2a_runtime.cli import parse

    voice = pipeline_command(SessionOptions(123, direction="en-zh", output_id="device-id", device="cpu"),
                             tmp_path / "voice.jsonl", parent_pid=999)
    assert voice[voice.index("--parent-pid") + 1] == "999"
    assert voice[voice.index("--direction") + 1] == "en-zh"
    assert voice[voice.index("--output-device") + 1] == "device-id"
    assert voice[voice.index("--tts") + 1] == "clone"
    assert "--translation-only" in voice
    assert "--control-stdin" in voice and "--events-stdout" in voice and "--research-text" not in voice
    captions = pipeline_command(SessionOptions(123, captions_only=True), tmp_path / "captions.jsonl")
    assert "--captions-only" in captions and captions[captions.index("--tts") + 1] == "tone"
    assert "--translation-only" not in captions
    assert "--output-device" not in captions
    options = parse(captions[4:])
    assert options.captions_only and options.control_stdin
    with pytest.raises(ValueError):
        pipeline_command(SessionOptions(0), tmp_path / "bad.jsonl")


def test_caption_controls_allow_stop_but_ignore_playback_changes():
    from a2a_runtime.driver import PlaybackControls

    controls = PlaybackControls.__new__(PlaybackControls)
    controls._stop = threading.Event()
    controls._lock = threading.Lock()
    controls._latest = None
    controls._allow_playback = False
    controls._apply("original")
    assert controls.take_latest() is None
    controls._apply("stop")
    assert controls._stop.is_set()


def test_voice_progress_explains_why_audio_is_waiting():
    assert "1.5 / 3.0" in voice_progress_text({"voice_status": "collecting", "seconds": 1.5, "required_seconds": 3})
    assert "one speaker" in voice_progress_text({"voice_status": "collecting", "reason": "mixed_speakers"})
    assert "Preparing" in voice_progress_text({"voice_status": "preparing"})


def test_worker_failure_explains_gpu_memory_without_echoing_model_output():
    assert "GPU memory exhausted" in diagnostic_message("RuntimeError: CUDA out of memory. private input")
    assert "recognition stopped" in diagnostic_message("a2a-pipeline: recognition stopped")
    assert diagnostic_message("arbitrary model output") is None


def test_stop_cancels_a_speech_worker_still_loading(monkeypatch):
    monkeypatch.setattr(speech, "installed_model", lambda *args: "model")
    instances = []

    class WaitingWorker:
        def __init__(self, *args):
            self.closed = False
            instances.append(self)

        def shutdown(self):
            self.closed = True

    monkeypatch.setattr(speech, "WorkerProcess", WaitingWorker)
    stop = threading.Event()
    stop.set()
    with pytest.raises(AdapterError, match="canceled"):
        speech.WhisperRecognizer("model", TranslationLanguage.MANDARIN, stop_event=stop)
    assert instances[0].closed


def test_pipeline_stop_while_voice_worker_loads(tmp_path):
    # A worker that never sends `ready` makes cancellation independent of GPUs and models.
    worker = tmp_path / "waiting.py"
    worker.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    cmd = [sys.executable, "-u", "-m", "a2a_runtime.cli", "--input", "unused.wav", "--clock", "host",
           "--asr-script", "unused.json", "--tts", "clone", "--clone-fake", "--clone-worker", str(worker),
           "--control-stdin", "--trace", str(tmp_path / "trace.jsonl")]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=REPO_ROOT, creationflags=0x08000000 if os.name == "nt" else 0)
    try:
        time.sleep(1.0)
        stdout, stderr = proc.communicate(input=b"stop\n", timeout=12)
        assert proc.returncode == 0, stderr.decode("utf-8", "replace")
        assert not (tmp_path / "trace.jsonl").exists(), "capture must not start after cancellation"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_ui_process_reads_captions_and_sends_playback_and_stop(monkeypatch, tmp_path):
    from a2a_runtime import ui_session

    fake = tmp_path / "fake_pipeline.py"
    fake.write_text(
        "import json, sys\n"
        "print(json.dumps({'ev':'commit.target','caption':'target.final','text':'Hello'}), flush=True)\n"
        "print('loading model 100%\\ra2a-pipeline: translator ready', file=sys.stderr, flush=True)\n"
        "for line in sys.stdin:\n"
        "    print('a2a-pipeline: command ' + line.strip(), file=sys.stderr, flush=True)\n"
        "    if line.strip() == 'stop': break\n", encoding="utf-8")
    monkeypatch.setattr(ui_session, "pipeline_command", lambda *_: [sys.executable, "-u", str(fake)])
    monkeypatch.setattr(ui_session, "trace_path", lambda: tmp_path / "unused.jsonl")
    session = ui_session.PipelineProcess(SessionOptions(1))
    events = []
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and not any(kind == "event" for kind, _ in events):
            try:
                events.append(session.events.get(timeout=.1))
            except queue.Empty:
                pass
        assert any(kind == "event" and value.get("text") == "Hello" for kind, value in events)
        assert session.send("original")
        session.stop()
        assert session.process.wait(timeout=8) == 0
        deadline = time.monotonic() + 2
        while ("status", "command original") not in events and time.monotonic() < deadline:
            try:
                events.append(session.events.get(timeout=.1))
            except queue.Empty:
                pass
        assert ("status", "command original") in events
        assert ("status", "translator ready") in events
        assert not (tmp_path / "unused.jsonl").exists()
    finally:
        if session.poll() is None:
            session.process.kill()
            session.process.wait()
