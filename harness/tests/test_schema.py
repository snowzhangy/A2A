"""Trace schema contract: the Swift golden traces and the plan's examples must validate."""

import json

import jsonschema
import pytest

from a2a_harness import REPO_ROOT, SCHEMA_PATH, trace

GOLDEN = sorted((REPO_ROOT / "apple" / "Tests" / "A2ACoreTests" / "Golden").glob("*.jsonl"))

PLAN_EXAMPLES = [
    '{"v":0,"t_ns":3100000000,"session":"a1f3","epoch":1,"gen":1,"ev":"asr.partial","spk":"S1","span":[96000,141120],"text":"我们下周开会","stable":2}',
    '{"v":0,"t_ns":3400000000,"session":"a1f3","epoch":1,"gen":1,"ev":"commit.target","unit":"u17","spk":"S1","prof":2,"span":[96000,141120],"text":"We will meet next week."}',
    '{"v":0,"t_ns":3800000000,"session":"a1f3","epoch":1,"gen":1,"ev":"play.start","unit":"u17","out_sample":182400}',
]


def test_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def test_schema_covers_every_required_event_type():
    required = {
        "session.start", "buf.capture", "buf.output", "state.change", "vad.start", "vad.end", "spk.assign",
        "spk.change", "profile.ready", "asr.partial", "asr.final", "commit.source", "mt.request", "mt.result",
        "commit.target", "tts.request", "tts.first_chunk", "tts.chunk", "play.scheduled", "play.start",
        "play.end", "unit.cancel", "source.disposition", "queue.depth", "clock.drift", "resource.sample",
        "fallback.enter", "fallback.exit", "trace.drop", "error",
    }
    enum = set(json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))["properties"]["ev"]["enum"])
    assert required <= enum


def test_plan_examples_validate():
    report = trace.validate(json.loads(line) for line in PLAN_EXAMPLES)
    assert report.ok, report.errors


@pytest.mark.parametrize("path", GOLDEN, ids=[p.stem for p in GOLDEN])
def test_swift_golden_traces_validate(path):
    assert GOLDEN, "no golden traces found"
    report = trace.validate_file(path)
    assert report.ok, report.errors[:5]
    assert report.events > 0


def test_invalid_events_are_reported():
    bad = [
        {"v": 0, "t_ns": 1, "session": "s", "epoch": 1, "gen": 1, "ev": "play.start", "unit": "u1"},  # no out_sample
        {"v": 0, "t_ns": 1, "session": "s", "epoch": 1, "gen": 1, "ev": "commit.source", "unit": "x",
         "spk": "S1", "span": [10, 5], "reason": "r", "timing_estimated": True, "wait_ms": 0},
        {"v": 1, "t_ns": 1, "session": "s", "epoch": 1, "gen": 1, "ev": "error", "where": "x"},
        {"v": 0, "t_ns": 1, "session": "s", "epoch": 1, "gen": 1, "ev": "made.up"},
    ]
    report = trace.validate(bad)
    text = "\n".join(report.errors)
    assert "out_sample" in text
    assert "span end 5 < start 10" in text
    assert "'u[0-9]+'" in text or "does not match" in text
    assert len(report.errors) >= 4


def test_trace_drops_invalidate_measurements():
    events = [{"v": 0, "t_ns": 1, "session": "s", "epoch": 1, "gen": 1, "ev": "trace.drop", "count": 3}]
    report = trace.validate(events)
    assert report.ok and report.trace_drops == 3 and not report.measurements_valid


def test_per_kind_validation_matches_full_schema():
    full = trace._validator()
    events = [e for p in GOLDEN for e in trace.load(p)]
    events += [
        {"v": 0, "t_ns": 1, "session": "s", "epoch": 1, "gen": 1, "ev": "play.start", "unit": "u1"},
        {"v": 0, "t_ns": 1, "session": "s", "epoch": 1, "gen": 1, "ev": "queue.depth", "debt_ms": 1},
        {"v": 0, "t_ns": 1, "session": "s", "epoch": 1, "gen": 1, "ev": "source.disposition", "disposition": "lost"},
    ]
    for e in events:
        assert bool(list(full.iter_errors(e))) == bool(list(trace._kind_validator(e["ev"]).iter_errors(e))), e
