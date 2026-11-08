"""End-to-end direct-audio driver replay through the shared fake Hy worker."""

import json

import numpy as np

from a2a_runtime import REPO_ROOT
from a2a_runtime.cli import main
from a2a_runtime.media.wav import WAVAudio, write


def run_case(tmp_path, capsys, streamed=True, background=False):
    rate = 48_000
    rng = np.random.default_rng(4)
    parts = []
    for speech, silence in [(1.2, 0.5), (1.1, 0.5)]:
        t = np.arange(int(speech * rate)) / rate
        parts.append((0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))
        parts.append(np.zeros(int(silence * rate), dtype=np.float32))
    source = tmp_path / "source.wav"
    model = tmp_path / "fake.gguf"
    model.write_bytes(b"fake")
    write(WAVAudio(rate, 1, np.concatenate(parts)), source)
    trace = tmp_path / ("stream.jsonl" if streamed else "whole.jsonl")
    args = ["--input", str(source), "--clock", "host", "--pace", "asap",
            "--asr", "audio", "--mt", "hy-local",
            "--hy-worker", str(REPO_ROOT / "research" / "hy_worker" / "fake_worker.py"),
            "--hy-model", str(model), "--tts", "tone", "--tail-seconds", "2",
            "--trace", str(trace), "--out", str(tmp_path / "out.wav"), "--research-text"]
    if not streamed:
        args.append("--no-hy-stream")
    if background:
        args += ["--background", "--fluent", "--presentation-delay-ms", "1000"]
    assert main(args) == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
    commits = [event for event in events if event["ev"] == "commit.source"]
    results = [event for event in events if event["ev"] == "mt.result"]
    assert (len(commits) >= 1 if background else len(commits) == 2)
    assert all(event["reason"] == "audio_segment" for event in commits)
    assert len(results) == len(commits) and all(event["ok"] for event in results), results
    assert all(event["worker_turns"] == 1 for event in results)
    assert summary["runtime_failed"] is False and summary["asr"] == "audio" and summary["mt"] == "hy-local"
    if background:
        assert summary["background"]["enabled"] and not summary["background"]["failed"]
        assert summary["background"]["frames"] > 0


def test_streamed_direct_audio_replay(tmp_path, capsys):
    run_case(tmp_path, capsys, streamed=True)


def test_whole_span_direct_audio_replay(tmp_path, capsys):
    run_case(tmp_path, capsys, streamed=False)


def test_direct_audio_with_delayed_rnnoise_background(tmp_path, capsys):
    import sys
    from pathlib import Path
    from a2a_runtime import REPO_ROOT

    python = REPO_ROOT / "runtime" / "separator" / ".venv" / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not Path(python).exists():
        import pytest

        pytest.skip("separator worker environment not installed")
    run_case(tmp_path, capsys, streamed=True, background=True)
