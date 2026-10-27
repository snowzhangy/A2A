"""Playback switching through the app helper's actual control pipe and mixer.

Synthetic input and fixture adapters only: no capture, models, or audio device.
"""

import json
import queue
import subprocess
import threading
import time

import numpy as np

from a2a_harness.audio import read_wav, write_wav


def test_playback_switch_controls_rendered_audio(pipeline_binary, tmp_path):
    rate = 48_000
    source = tmp_path / "source.wav"
    write_wav(source, np.full(rate * 3, 0.25, dtype=np.float32), rate)
    script = tmp_path / "asr.json"
    script.write_text(json.dumps({"hypotheses": []}))
    output = tmp_path / "output.wav"
    process = subprocess.Popen([
        str(pipeline_binary), "--input", str(source), "--clock", "host", "--pace", "realtime",
        "--asr", "scripted", "--asr-script", str(script), "--translation-only",
        "--control-stdin", "--events-stdout", "--tail-seconds", "0",
        "--trace", str(tmp_path / "trace.jsonl"), "--out", str(output),
    ], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    messages = queue.Queue()

    def collect_events():
        for line in process.stdout:
            messages.put(json.loads(line))

    reader = threading.Thread(target=collect_events, daemon=True)
    reader.start()

    def select(mode):
        process.stdin.write(mode + "\n")
        process.stdin.flush()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            event = messages.get(timeout=max(0.01, deadline - time.monotonic()))
            if event.get("ev") == "gain.ramp" and event.get("playback") == mode:
                return event
        raise AssertionError(f"No acknowledgment for playback {mode}")

    try:
        original = select("original")
        assert original["original"] == 1 and original["translated"] == 0
        time.sleep(0.4)
        translated = select("translated")
        assert translated["original"] == translated["translated"] == 0  # voice not ready
        assert process.wait(timeout=10) == 0, process.stderr.read()
        reader.join(timeout=2)
        rendered, actual_rate = read_wav(output)
        assert actual_rate == rate
        assert np.max(rendered) > 0.24  # the original bus really reached the render output
        assert np.max(np.abs(rendered[-rate // 4:])) == 0  # returns to English-only silence
    finally:
        process.stdin.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        reader.join(timeout=2)
        process.stdout.close()
        process.stderr.close()


def test_control_and_audio_cannot_share_stdin(pipeline_binary):
    result = subprocess.run([str(pipeline_binary), "--stdin", "--control-stdin"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "cannot share stdin with audio" in result.stderr
