"""Integration: harness ↔ a2a-pipeline (skips when the Swift binary is not built)."""

from a2a_harness import trace
from a2a_harness.runner import RunConfig, run

import json
import subprocess

import pytest

from a2a_harness.fixture import load


def _deterministic(events):
    return [e for e in events if e["ev"] != "resource.sample"]


def test_offline_replay_is_valid_and_deterministic(synthetic_fixture, pipeline_binary, tmp_path):
    r1 = run(RunConfig(fixture=synthetic_fixture, pipeline=pipeline_binary, out_dir=tmp_path / "r1", mode="offline"))
    r2 = run(RunConfig(fixture=synthetic_fixture, pipeline=pipeline_binary, out_dir=tmp_path / "r2", mode="offline"))
    assert r1["validation"]["ok"], r1["validation"]["errors"]
    m = r1["metrics"]
    assert m["units_played"] > 0 and m["units_aligned"] == m["units_committed"]
    assert m["translation_coverage"] > 0
    assert r1["plumbing_only"] and not r1["gate_valid"]
    e1 = _deterministic(trace.load(tmp_path / "r1" / "trace.jsonl"))
    e2 = _deterministic(trace.load(tmp_path / "r2" / "trace.jsonl"))
    assert e1 == e2, "virtual-clock replay must be byte-for-byte reproducible"
    assert (tmp_path / "r1" / "report.md").read_text().startswith("# Replay report")


def test_fluent_replay_finishes_each_audible_phrase(synthetic_fixture, pipeline_binary, tmp_path):
    result = run(RunConfig(fixture=synthetic_fixture, pipeline=pipeline_binary,
                           out_dir=tmp_path / "fluent", mode="offline",
                           extra_args=["--fluent", "--translation-only"]))
    assert result["validation"]["ok"], result["validation"]["errors"]
    assert result["metrics"]["units_played"] > 0
    active = None
    for event in trace.load(tmp_path / "fluent" / "trace.jsonl"):
        if event["ev"] == "play.start":
            assert active is None, "a later phrase started before the preceding phrase ended"
            active = event["unit"]
        elif event["ev"] == "play.end":
            assert event["unit"] == active
            active = None
        elif event["ev"] == "fallback.enter":
            raise AssertionError(f"fixture speech unexpectedly fell back: {event['reason']}")
    assert active is None, "the final audible phrase was truncated"


def test_english_to_chinese_replay_produces_captions_and_audio(synthetic_fixture, pipeline_binary, tmp_path):
    fx = load(synthetic_fixture)
    source, target = "We will meet next week.", "我们下周开会。"
    script = tmp_path / "english-asr.json"
    dictionary = tmp_path / "chinese-mt.json"
    trace_path = tmp_path / "reverse.jsonl"
    script.write_text(json.dumps({"hypotheses": [
        {"at_sample": 96_000, "utt": 1, "text": source, "final": True,
         "tokens": [{"text": source, "start": 24_000, "end": 72_000}]}]}))
    dictionary.write_text(json.dumps({source: target}))
    result = subprocess.run([
        str(pipeline_binary), "--input", str(fx.audio), "--asr", "scripted", "--asr-script", str(script),
        "--mt", "dictionary", "--mt-dictionary", str(dictionary), "--direction", "en-zh",
        "--translation-only", "--fluent", "--events-stdout", "--trace", str(trace_path),
        "--out", str(tmp_path / "reverse.wav"),
    ], capture_output=True, text=True, timeout=30, check=True)
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    summary = messages[-1]
    assert summary["direction"] == "en-zh" and summary["pair"] == "en-US>zh-CN"
    assert any(m.get("caption") == "target.final" and m["text"] == target for m in messages)
    events = trace.load(trace_path)
    assert trace.validate(events).ok
    assert next(e for e in events if e["ev"] == "session.start")["pair"] == "en-US>zh-CN"
    assert any(e["ev"] == "play.end" for e in events)
    assert not any(e["ev"] == "mt.result" and not e["ok"] for e in events)
    assert all("text" not in e for e in events), "captions must remain in memory"


@pytest.mark.parametrize("value", ["auto", "en-en", "zh-fr"])
def test_unknown_direction_fails_before_audio_startup(pipeline_binary, value):
    result = subprocess.run([str(pipeline_binary), "--stdin", "--direction", value],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and "--direction is zh-en or en-zh" in result.stderr


def test_paced_replay_never_reads_future_audio(synthetic_fixture, pipeline_binary, tmp_path):
    r = run(RunConfig(fixture=synthetic_fixture, pipeline=pipeline_binary, out_dir=tmp_path / "p",
                      mode="paced", speed=4.0))
    assert r["validation"]["ok"], r["validation"]["errors"]
    assert r["future_access_violations"] == []
    assert r["validation"]["kinds"].get("buf.capture", 0) > 0
    assert r["metrics"]["units_played"] > 0
    assert not r["gate_valid"]   # speed != 1 and plumbing adapters
    # Only one synthesis stream may feed the shared PCM bus at a time.
    closed, current = set(), None
    for event in trace.load(tmp_path / "p" / "trace.jsonl"):
        if event["ev"] not in {"tts.first_chunk", "tts.chunk"}:
            continue
        unit = event["unit"]
        if unit != current:
            if current is not None:
                closed.add(current)
            assert unit not in closed, "synthesis streams interleaved on the PCM bus"
            current = unit


def test_caption_delivery_does_not_persist_text(synthetic_fixture, pipeline_binary, tmp_path):
    fx = load(synthetic_fixture)
    trace_path = tmp_path / "private.jsonl"
    result = subprocess.run([
        str(pipeline_binary), "--input", str(fx.audio), "--asr", "scripted",
        "--asr-script", str(fx.asr_script), "--mt", "dictionary", "--mt-dictionary", str(fx.mt_dictionary),
        "--captions-only", "--events-stdout", "--trace", str(trace_path),
    ], capture_output=True, text=True, timeout=30, check=True)
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert any(m.get("caption") == "target.final" and m["text"] for m in messages)
    assert messages[-1]["captions_only"]
    events = trace.load(trace_path)
    assert all("text" not in event for event in events)
    assert not any(event["ev"] in {"tts.request", "play.start"} for event in events)


def test_nonfinite_duration_is_rejected_before_startup(pipeline_binary):
    result = subprocess.run([str(pipeline_binary), "--stdin", "--tail-seconds", "nan"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert "invalid duration" in result.stderr


def test_translation_only_is_silent_while_voice_is_unavailable(synthetic_fixture, pipeline_binary, tmp_path):
    import numpy as np
    from a2a_harness.audio import read_wav

    r = run(RunConfig(fixture=synthetic_fixture, pipeline=pipeline_binary, out_dir=tmp_path / "silent",
                      extra_args=["--translation-only"], profile_ready_ms=3_600_000))
    original, _ = read_wav(load(synthetic_fixture).audio)
    rendered, _ = read_wav(tmp_path / "silent" / "out.wav")
    assert np.max(np.abs(original)) > 0.01
    assert np.max(np.abs(rendered)) == 0
    assert r["metrics"]["units_played"] == 0
    for event in trace.load(tmp_path / "silent" / "trace.jsonl"):
        if event["ev"] == "gain.ramp":
            assert event["original"] == 0
