"""Integration: a2a-pipeline --tts clone with the standard-library fake worker.

Covers the driver wiring (worker start-up, reference acquisition from scripted token
timing, prepared-profile hand-off, shutdown) and the reporting rule that a cloning run is
never identity-qualified. No model, MLX, or audio device is involved.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap

import pytest
import numpy as np

from a2a_harness import REPO_ROOT, metrics, trace
from a2a_harness.audio import read_wav, write_wav
from a2a_harness.fixture import load

# The fake worker needs only the standard library: the system Python on macOS, this
# interpreter elsewhere (Windows has no /usr/bin/python3).
PYTHON = shutil.which("python3", path="/usr/bin") or sys.executable


@pytest.mark.skipif(PYTHON is None, reason="needs a Python for the fake worker")
def test_dual_clone_prefetches_one_phrase_without_reordering(pipeline_binary, tmp_path):
    rate = 48_000
    source = tmp_path / "source.wav"
    write_wav(source, (0.1 * np.sin(2 * np.pi * 140 * np.arange(rate * 5) / rate)).astype(np.float32), rate)
    script = tmp_path / "asr.json"
    script.write_text(json.dumps({"hypotheses": [
        {"at_sample": 48000, "utt": 1, "text": "第一句话。", "final": True,
         "tokens": [{"text": "第一句话。", "start": 0, "end": 48000}]},
        {"at_sample": 72000, "utt": 2, "text": "第二句话。", "final": True,
         "tokens": [{"text": "第二句话。", "start": 48000, "end": 72000}]},
        {"at_sample": 96000, "utt": 3, "text": "第三句话。", "final": True,
         "tokens": [{"text": "第三句话。", "start": 72000, "end": 96000}]},
    ]}), encoding="utf-8")
    dictionary = tmp_path / "mt.json"
    dictionary.write_text(json.dumps({"第一句话。": "The first sentence.", "第二句话。": "The second sentence.",
                                      "第三句话。": "The third sentence."}), encoding="utf-8")
    fake = tmp_path / "slow_worker.py"
    fake.write_text(textwrap.dedent(f"""
        import array, math, sys, time
        sys.path.insert(0, {str(REPO_ROOT / 'research')!r})
        import tts_worker
        def generate(self, voice, text, interval, attempt=0, speed=1.0):
            for _ in range(12):
                time.sleep(0.20)
                yield array.array('f', (0.1 * math.sin(i / 10) for i in range(7680)))
        tts_worker.FakeBackend.generate = generate
        tts_worker.main()
    """), encoding="utf-8")
    cmd = [str(pipeline_binary), "--input", str(source), "--clock", "host", "--pace", "realtime",
           "--asr", "scripted", "--asr-script", str(script), "--mt", "dictionary", "--mt-dictionary", str(dictionary),
           "--tts", "clone", "--clone-fake", "--clone-python", PYTHON, "--clone-worker", str(fake),
           "--clone-mode", "xvector", "--voice-ref", str(source), "--fluent", "--dual-clone", "--clone-model", "qwen",
           "--max-debt-seconds", "60", "--max-source-lag-seconds", "120", "--tail-seconds", "15",
           "--trace", str(tmp_path / "trace.jsonl")]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    assert summary["clone"]["workers"] == 2
    events = trace.load(tmp_path / "trace.jsonl")
    times = {(e["ev"], e.get("unit")): e["t_ns"] for e in events}
    assert times[("play.start", "u1")] < times[("tts.first_chunk", "u2")] < times[("tts.done", "u1")]
    assert times[("play.end", "u1")] <= times[("play.start", "u2")]
    assert times[("play.start", "u2")] < times[("tts.first_chunk", "u3")]
    assert times[("play.end", "u2")] <= times[("play.start", "u3")]


@pytest.mark.skipif(PYTHON is None, reason="needs a Python for the fake worker")
def test_clone_acquires_reference_and_never_qualifies_identity(synthetic_fixture, pipeline_binary, tmp_path):
    fx = load(synthetic_fixture)
    cmd = [str(pipeline_binary), "--input", str(fx.audio), "--clock", "host", "--pace", "asap",
           "--asr", "scripted", "--asr-script", str(fx.asr_script),
           "--mt", "dictionary", "--mt-dictionary", str(fx.mt_dictionary),
           "--tts", "clone", "--clone-fake", "--clone-python", PYTHON,
           "--clone-worker", str(REPO_ROOT / "research" / "tts_worker.py"),
           "--trace", str(tmp_path / "trace.jsonl"), "--out", str(tmp_path / "out.wav")]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    summary = json.loads(done.stdout.strip().splitlines()[-1])
    assert summary["tts"] == "clone" and summary["tts_preserves_speaker_identity"]
    assert summary["tts_identity_qualified"] is False
    clone = summary["clone"]
    assert clone["model"] == "fake-tone" and clone["reference"] == "acquired"
    versions = [p["version"] for p in clone["profiles"]]
    assert versions == [1]
    assert clone["session_voice_locked"]
    assert all(3.0 <= p["reference_seconds"] <= 11.0 for p in clone["profiles"])
    events = trace.load(tmp_path / "trace.jsonl")
    ready = [e for e in events if e["ev"] == "profile.ready"]
    assert [e["prof"] for e in ready] == versions
    # Nothing is voiced before the first profile (whether any unit is voiced at --pace asap
    # depends on host speed; the fixed-reference test below requires it).
    requests = [e for e in events if e["ev"] == "tts.request"]
    assert all(e["t_ns"] >= ready[0]["t_ns"] for e in requests)


@pytest.mark.skipif(PYTHON is None, reason="needs a Python for the fake worker")
@pytest.mark.parametrize("model", ["qwen", "omnivoice", "chatterbox"])
def test_clone_fixed_reference_is_ready_before_the_first_unit(synthetic_fixture, pipeline_binary, tmp_path, model):
    fx = load(synthetic_fixture)
    reference = tmp_path / "reference.wav"
    samples, rate = read_wav(fx.audio)
    write_wav(reference, samples[: rate * 4], rate)
    mode = "xvector" if model == "qwen" else "icl"
    reference_text = tmp_path / "reference.txt"
    reference_text.write_text("Synthetic reference.", encoding="utf-8")
    cmd = [str(pipeline_binary), "--input", str(fx.audio), "--clock", "host", "--pace", "asap",
           "--asr", "scripted", "--asr-script", str(fx.asr_script),
           "--mt", "dictionary", "--mt-dictionary", str(fx.mt_dictionary),
           "--tts", "clone", "--clone-fake", "--clone-model", model, "--clone-python", PYTHON, "--clone-mode", mode,
           "--voice-ref-text", str(reference_text),
           "--clone-worker", str(REPO_ROOT / "research" / "tts_worker.py"), "--voice-ref", str(reference),
           "--trace", str(tmp_path / "trace.jsonl"), "--out", str(tmp_path / "out.wav")]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=180)
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
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 2 and "need --tts clone" in done.stderr
    done = subprocess.run([str(pipeline_binary), "--input", "x.wav", "--clock", "host", "--tts", "clone",
                           "--voice-ref", "ref.wav", "--trace", str(tmp_path / "t.jsonl")],
                          capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert done.returncode == 2 and "--voice-ref-text" in done.stderr


@pytest.mark.parametrize("args,message", [
    (["--clone-model", "auto"], "--clone-model is qwen, audio8, omnivoice, or chatterbox"),
    (["--tts", "system", "--clone-model", "omnivoice"], "need --tts clone"),
    (["--tts", "clone", "--clone-model", "chatterbox", "--dual-clone"], "--dual-clone requires"),
])
def test_invalid_model_combinations_fail_before_startup(pipeline_binary, args, message):
    done = subprocess.run([str(pipeline_binary), "--stdin"] + args,
                          capture_output=True, text=True, encoding="utf-8", timeout=5)
    assert done.returncode == 2 and message in done.stderr


@pytest.mark.skipif(PYTHON is None, reason="needs a Python for the fake worker")
def test_remembered_voice_survives_restart_and_is_reused(pipeline_binary, tmp_path):
    rate = 48_000
    source = tmp_path / "source.wav"
    write_wav(source, (0.1 * np.sin(2 * np.pi * 140 * np.arange(rate * 10) / rate)).astype(np.float32), rate)
    script = tmp_path / "asr.json"
    script.write_text(json.dumps({"hypotheses": [
        {"at_sample": 201600, "utt": 1, "text": "我们下周开会。", "final": True,
         "tokens": [{"text": "我们下周开会。", "start": 24000, "end": 192000}]}]}), encoding="utf-8")
    dictionary = tmp_path / "mt.json"
    dictionary.write_text(json.dumps({"我们下周开会。": "We will meet next week."}), encoding="utf-8")
    summaries = []
    for attempt in ["cold", "warm"]:
        run = tmp_path / attempt
        run.mkdir()
        cmd = [str(pipeline_binary), "--input", str(source), "--clock", "host", "--pace", "asap",
               "--asr", "scripted", "--asr-script", str(script), "--mt", "dictionary", "--mt-dictionary", str(dictionary),
               "--tts", "clone", "--clone-fake", "--clone-python", PYTHON,
               "--clone-worker", str(REPO_ROOT / "research/tts_worker.py"), "--speakers", "auto",
               "--voice-memory", str(tmp_path / "memory"), "--events-stdout", "--trace", str(run / "trace.jsonl")]
        done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert done.returncode == 0, done.stderr[-2000:]
        summaries.append(json.loads(done.stdout.splitlines()[-1]))
    assert list((tmp_path / "memory").glob("*.voice"))
    assert summaries[1]["clone"]["reference_stats"].get("reference_memory_reused", 0) >= 1
    assert any(p.get("reused") for p in summaries[1]["clone"]["profiles"])


@pytest.mark.skipif(PYTHON is None, reason="needs /usr/bin/python3 for the fake worker")
def test_pronunciation_map_changes_spoken_text_and_traces_pacing(synthetic_fixture, pipeline_binary, tmp_path):
    fx = load(synthetic_fixture)
    reference = tmp_path / "reference.wav"
    samples, rate = read_wav(fx.audio)
    write_wav(reference, samples[: rate * 4], rate)
    reference_text = tmp_path / "reference.txt"
    reference_text.write_text("Synthetic reference.", encoding="utf-8")
    pronounce = tmp_path / "pronounce.json"
    pronounce.write_text(json.dumps({"the": "thuh", "we": "wee"}), encoding="utf-8")   # in most fixture translations
    cmd = [str(pipeline_binary), "--input", str(fx.audio), "--clock", "host", "--pace", "asap",
           "--asr", "scripted", "--asr-script", str(fx.asr_script),
           "--mt", "dictionary", "--mt-dictionary", str(fx.mt_dictionary),
           "--tts", "clone", "--clone-fake", "--clone-python", PYTHON,
           "--clone-worker", str(REPO_ROOT / "research" / "tts_worker.py"),
           "--voice-ref", str(reference), "--voice-ref-text", str(reference_text), "--pronounce", str(pronounce),
           # --pace asap outruns real time; generous limits let synthesis finish instead of falling back.
           "--max-debt-seconds", "60", "--max-source-lag-seconds", "120",
           "--trace", str(tmp_path / "trace.jsonl"), "--out", str(tmp_path / "out.wav")]
    done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=180)
    assert done.returncode == 0, done.stderr[-2000:]
    events = trace.load(tmp_path / "trace.jsonl")
    finished = [e for e in events if e["ev"] == "tts.done"]
    assert finished, "no unit completed synthesis"
    assert all(e["pace"] >= 1 and e["audio_ms"] > 0 and "text" not in e for e in finished)
    assert any(e["respelled"] for e in finished)


def test_a_phrase_that_plays_in_a_slot_is_filled_out_to_the_actors_time(synthetic_fixture, pipeline_binary,
                                                                        tmp_path):
    """Turn fill (ADR-0013) through the real driver, worker protocol, playout and metrics.

    The worker slows a take only when the host says the phrase plays in a slot, because only there
    does a longer line cost nothing: it ends at its actor's own last sound, never later. Live
    playback plays as soon as audio is ready, where a short line is saved latency instead.

    The standard-library tone voice stands in for a model, so this is the mechanism and its
    measurement end to end, not a claim about how stretched speech sounds.
    """
    fx = load(synthetic_fixture)
    common = [str(pipeline_binary), "--input", str(fx.audio), "--clock", "host", "--pace", "realtime",
              "--asr", "scripted", "--asr-script", str(fx.asr_script),
              "--mt", "dictionary", "--mt-dictionary", str(fx.mt_dictionary),
              "--tts", "clone", "--clone-fake",
              # The pacer needs NumPy, which the standard-library worker run elsewhere avoids.
              "--clone-python", sys.executable,
              "--clone-worker", str(REPO_ROOT / "research" / "tts_worker.py"),
              "--clone-mode", "xvector", "--fluent", "--translation-only",
              # Nothing may be skipped: the two runs must differ in line length, not in coverage.
              "--max-debt-seconds", "60", "--max-source-lag-seconds", "120", "--tail-seconds", "12"]

    def go(name: str, *extra: str) -> tuple[list[dict], dict]:
        out = tmp_path / name
        out.mkdir(parents=True)
        done = subprocess.run(common + list(extra) + ["--trace", str(out / "trace.jsonl"),
                                                      "--out", str(out / "out.wav")],
                              capture_output=True, text=True, encoding="utf-8", timeout=240)
        assert done.returncode == 0, done.stderr[-2000:]
        events = trace.load(out / "trace.jsonl")
        return events, metrics.compute(events, fx)

    live_events, live_metrics = go("live")
    slot_events, slot_metrics = go("slot", "--presentation-delay-ms", "4000")
    off_events, _ = go("no-fill", "--presentation-delay-ms", "4000", "--no-turn-fill")

    # Live playback asks for no fill and gets none.
    live_done = [e for e in live_events if e["ev"] == "tts.done"]
    assert live_done and all(e["pace"] >= 1.0 for e in live_done)
    assert not any(e.get("fill") for e in live_events if e["ev"] == "tts.request")

    done = [e for e in slot_events if e["ev"] == "tts.done"]
    assert done, "no unit completed synthesis"
    filled = [e for e in done if e["pace"] < 1.0]
    assert filled, "a slotted run never filled a short line out to its actor"
    assert all(e["audio_ms"] > e["native_ms"] for e in filled), "a fill must emit more audio, not less"
    assert all(e["pace"] >= 0.85 for e in filled), "fill stays inside the pacer's bound"
    asked = {e["unit"] for e in slot_events if e["ev"] == "tts.request" and e.get("fill")}
    assert {e["unit"] for e in filled} <= asked, "a filled take was never asked for"
    # The A/B escape hatch: same slots, no line slowed, so a listener can hear the difference.
    assert not any(e.get("fill") for e in off_events if e["ev"] == "tts.request")
    assert all(e["pace"] >= 1.0 for e in off_events if e["ev"] == "tts.done")

    # Every filled line ends nearer its actor, and the worst gap in the run narrows. Compared
    # line by line: the median would be unchanged when the run's middle line is not a filled one.
    live_gaps = {r["unit"]: r["turn_gap_s"] for r in live_metrics["per_unit"] if "turn_gap_s" in r}
    slot_gaps = {r["unit"]: r["turn_gap_s"] for r in slot_metrics["per_unit"] if "turn_gap_s" in r}
    assert {e["unit"] for e in done} == {e["unit"] for e in live_done}, "the runs differ in coverage"
    assert set(live_gaps) == set(slot_gaps) and live_gaps
    for filled_unit in filled:
        assert slot_gaps[filled_unit["unit"]] < live_gaps[filled_unit["unit"]]
    assert slot_metrics["turn_dead_air_s"]["max"] < live_metrics["turn_dead_air_s"]["max"]
