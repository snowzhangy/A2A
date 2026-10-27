"""Integration: a2a-pipeline --tts clone with the standard-library fake worker.

Covers the driver wiring (worker start-up, reference acquisition from scripted token
timing, prepared-profile hand-off, shutdown) and the reporting rule that a cloning run is
never identity-qualified. No model, MLX, or audio device is involved.
"""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest
import numpy as np

from a2a_harness import REPO_ROOT, trace
from a2a_harness.audio import read_wav, write_wav
from a2a_harness.fixture import load

PYTHON = shutil.which("python3", path="/usr/bin")


@pytest.mark.skipif(PYTHON is None, reason="needs /usr/bin/python3 for the fake worker")
def test_clone_acquires_reference_and_never_qualifies_identity(synthetic_fixture, pipeline_binary, tmp_path):
    fx = load(synthetic_fixture)
    cmd = [str(pipeline_binary), "--input", str(fx.audio), "--clock", "host", "--pace", "asap",
           "--asr", "scripted", "--asr-script", str(fx.asr_script),
           "--mt", "dictionary", "--mt-dictionary", str(fx.mt_dictionary),
           "--tts", "clone", "--clone-fake", "--clone-python", PYTHON,
           "--clone-worker", str(REPO_ROOT / "research" / "tts_worker.py"),
           "--trace", str(tmp_path / "trace.jsonl"), "--out", str(tmp_path / "out.wav")]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    assert summary["tts"] == "clone" and summary["tts_preserves_speaker_identity"]
    assert summary["tts_identity_qualified"] is False
    clone = summary["clone"]
    assert clone["model"] == "fake-tone" and clone["reference"] == "acquired"
    versions = [p["version"] for p in clone["profiles"]]
    assert versions and versions == sorted(versions) and versions[0] == 1
    assert all(3.0 <= p["reference_seconds"] <= 11.0 for p in clone["profiles"])
    events = trace.load(tmp_path / "trace.jsonl")
    ready = [e for e in events if e["ev"] == "profile.ready"]
    assert [e["prof"] for e in ready] == versions
    # Nothing is voiced before the first profile (whether any unit is voiced at --pace asap
    # depends on host speed; the fixed-reference test below requires it).
    requests = [e for e in events if e["ev"] == "tts.request"]
    assert all(e["t_ns"] >= ready[0]["t_ns"] for e in requests)


@pytest.mark.skipif(PYTHON is None, reason="needs /usr/bin/python3 for the fake worker")
@pytest.mark.parametrize("model", ["qwen", "omnivoice", "chatterbox"])
def test_clone_fixed_reference_is_ready_before_the_first_unit(synthetic_fixture, pipeline_binary, tmp_path, model):
    fx = load(synthetic_fixture)
    reference = tmp_path / "reference.wav"
    samples, rate = read_wav(fx.audio)
    write_wav(reference, samples[: rate * 4], rate)
    mode = "xvector" if model == "qwen" else "icl"
    reference_text = tmp_path / "reference.txt"
    reference_text.write_text("Synthetic reference.")
    cmd = [str(pipeline_binary), "--input", str(fx.audio), "--clock", "host", "--pace", "asap",
           "--asr", "scripted", "--asr-script", str(fx.asr_script),
           "--mt", "dictionary", "--mt-dictionary", str(fx.mt_dictionary),
           "--tts", "clone", "--clone-fake", "--clone-model", model, "--clone-python", PYTHON, "--clone-mode", mode,
           "--voice-ref-text", str(reference_text),
           "--clone-worker", str(REPO_ROOT / "research" / "tts_worker.py"), "--voice-ref", str(reference),
           "--trace", str(tmp_path / "trace.jsonl"), "--out", str(tmp_path / "out.wav")]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    assert summary["clone"]["reference"] == "fixed" and summary["clone"]["conditioning"] == mode
    assert summary["clone"]["selected_model"] == model
    assert summary["clone"]["audio_output_streaming"] == (model == "qwen")
    events = trace.load(tmp_path / "trace.jsonl")
    ready = [e for e in events if e["ev"] == "profile.ready"]
    commits = [e for e in events if e["ev"] == "commit.source"]
    requests = [e for e in events if e["ev"] == "tts.request"]
    assert [e["prof"] for e in ready] == [1] and commits and requests
    assert ready[0]["t_ns"] <= commits[0]["t_ns"]


def test_clone_options_are_validated(pipeline_binary, tmp_path):
    done = subprocess.run([str(pipeline_binary), "--input", "x.wav", "--clock", "host", "--tts", "system",
                           "--voice-ref", "ref.wav", "--trace", str(tmp_path / "t.jsonl")],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 2 and "need --tts clone" in done.stderr
    done = subprocess.run([str(pipeline_binary), "--input", "x.wav", "--clock", "host", "--tts", "clone",
                           "--voice-ref", "ref.wav", "--trace", str(tmp_path / "t.jsonl")],
                          capture_output=True, text=True, timeout=30)
    assert done.returncode == 2 and "--voice-ref-text" in done.stderr


@pytest.mark.parametrize("args,message", [
    (["--clone-model", "auto"], "--clone-model is qwen, omnivoice, or chatterbox"),
    (["--tts", "system", "--clone-model", "omnivoice"], "need --tts clone"),
    (["--tts", "clone", "--clone-model", "chatterbox", "--clone-mode", "xvector"], "xvector requires"),
])
def test_invalid_model_combinations_fail_before_startup(pipeline_binary, args, message):
    done = subprocess.run([str(pipeline_binary), "--stdin"] + args,
                          capture_output=True, text=True, timeout=5)
    assert done.returncode == 2 and message in done.stderr


@pytest.mark.skipif(PYTHON is None, reason="needs /usr/bin/python3 for the fake worker")
def test_remembered_voice_survives_restart_and_is_reused(pipeline_binary, tmp_path):
    rate = 48_000
    source = tmp_path / "source.wav"
    write_wav(source, (0.1 * np.sin(2 * np.pi * 140 * np.arange(rate * 10) / rate)).astype(np.float32), rate)
    script = tmp_path / "asr.json"
    script.write_text(json.dumps({"hypotheses": [
        {"at_sample": 201600, "utt": 1, "text": "我们下周开会。", "final": True,
         "tokens": [{"text": "我们下周开会。", "start": 24000, "end": 192000}]}]}))
    dictionary = tmp_path / "mt.json"
    dictionary.write_text(json.dumps({"我们下周开会。": "We will meet next week."}))
    summaries = []
    for attempt in ["cold", "warm"]:
        run = tmp_path / attempt
        run.mkdir()
        cmd = [str(pipeline_binary), "--input", str(source), "--clock", "host", "--pace", "asap",
               "--asr", "scripted", "--asr-script", str(script), "--mt", "dictionary", "--mt-dictionary", str(dictionary),
               "--tts", "clone", "--clone-fake", "--clone-python", PYTHON,
               "--clone-worker", str(REPO_ROOT / "research/tts_worker.py"), "--speakers", "auto",
               "--voice-memory", str(tmp_path / "memory"), "--events-stdout", "--trace", str(run / "trace.jsonl")]
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr[-2000:]
        summaries.append(json.loads(done.stdout.splitlines()[-1]))
    assert list((tmp_path / "memory").glob("*.voice"))
    assert summaries[1]["clone"]["reference_stats"].get("reference_memory_reused", 0) >= 1
    assert any(p.get("reused") for p in summaries[1]["clone"]["profiles"])
