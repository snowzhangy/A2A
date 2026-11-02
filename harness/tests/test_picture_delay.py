"""Picture delay D (`a2a-pipeline --presentation-delay-ms`, ADR-0012) on deterministic replay.

Live playback starts each phrase as soon as it is ready, so the English trails its source by a
different amount every line and cannot match a picture. With the picture shown D late, every
phrase must start at its own source start + D. Skips when the Swift binary is not built.
"""
import subprocess

from a2a_harness import trace
from a2a_harness.runner import RunConfig, run


def _onset_offsets(path) -> list[float]:
    spans, starts = {}, {}
    for event in trace.load(path):
        if event["ev"] == "commit.source":
            spans[event["unit"]] = event["span"][0]
        elif event["ev"] == "play.start":
            starts[event["unit"]] = event["out_sample"]
    return [(starts[u] - spans[u]) / 48_000 for u in starts]


def test_every_phrase_starts_in_its_own_slot(synthetic_fixture, pipeline_binary, tmp_path):
    live = run(RunConfig(fixture=synthetic_fixture, pipeline=pipeline_binary, out_dir=tmp_path / "live",
                         mode="offline", extra_args=["--fluent", "--translation-only"]))
    slotted = run(RunConfig(fixture=synthetic_fixture, pipeline=pipeline_binary, out_dir=tmp_path / "slot",
                            mode="offline",
                            extra_args=["--fluent", "--translation-only", "--presentation-delay-ms", "4000"]))
    assert slotted["validation"]["ok"], slotted["validation"]["errors"]
    live_offsets = _onset_offsets(tmp_path / "live" / "trace.jsonl")
    offsets = _onset_offsets(tmp_path / "slot" / "trace.jsonl")
    # The fixture's lag varies by more than a second without a delay: that is the mismatch.
    assert max(live_offsets) - min(live_offsets) > 0.5
    # Same phrases, each within one 20 ms driver step of source start + D.
    assert len(offsets) == len(live_offsets) > 0
    assert all(abs(o - 4.0) <= 0.03 for o in offsets), offsets
    late = [e["late_ms"] for e in trace.load(tmp_path / "slot" / "trace.jsonl")
            if e["ev"] == "play.scheduled" and "late_ms" in e]
    assert len(late) == len(offsets) and all(0 <= ms <= 30 for ms in late)
    assert not [e for e in trace.load(tmp_path / "slot" / "trace.jsonl")
                if e["ev"] == "unit.cancel" and e["reason"] in ("catch_up", "source_expired", "queue_limit")]


def test_picture_delay_needs_fluent_playout(pipeline_binary):
    result = subprocess.run([str(pipeline_binary), "--stdin", "--presentation-delay-ms", "4000"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "--presentation-delay-ms requires --fluent" in result.stderr


def test_picture_delay_is_bounded(pipeline_binary):
    result = subprocess.run([str(pipeline_binary), "--stdin", "--fluent", "--presentation-delay-ms", "45000"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "0–30000" in result.stderr


def _capture_seconds(events, sample: int) -> float:
    """Capture time of a source sample, from the driver's own capture anchors."""
    t0 = events[0]["t_ns"]
    for e in events:
        if e["ev"] == "buf.capture" and e["span"][1] >= sample:
            return (e["t_ns"] - t0) / 1e9 - (e["span"][1] - sample) / 48_000
    raise AssertionError(f"no capture anchor covers sample {sample}")


def test_a_pause_keeps_every_phrase_on_its_slot(synthetic_fixture, pipeline_binary, tmp_path):
    # A paused video leaves the global tap without callbacks. The picture keeps showing the last
    # D seconds, so phrases due in that time must still start, and the phrase spoken just before
    # the pause must close although no trailing silence was captured.
    args = ["--fluent", "--translation-only", "--presentation-delay-ms", "4000"]
    run(RunConfig(fixture=synthetic_fixture, pipeline=pipeline_binary, out_dir=tmp_path / "plain",
                  mode="offline", extra_args=args))
    spans = [e["span"] for e in trace.load(tmp_path / "plain" / "trace.jsonl") if e["ev"] == "commit.source"]
    # Pause 0.2 s after a phrase ends, while its slot is still to come.
    start, end = next((s, e) for s, e in spans[1:] if e / 48_000 + 0.2 < s / 48_000 + 4.0)
    pause_at = end / 48_000 + 0.2
    result = run(RunConfig(fixture=synthetic_fixture, pipeline=pipeline_binary, out_dir=tmp_path / "pause",
                           mode="offline", extra_args=args + ["--simulate-capture-gap", f"{pause_at:.3f}:10"]))
    assert result["validation"]["ok"], result["validation"]["errors"]
    events = trace.load(tmp_path / "pause" / "trace.jsonl")
    units = {e["unit"]: e["span"] for e in events if e["ev"] == "commit.source"}
    starts = {e["unit"]: e["out_sample"] for e in events if e["ev"] == "play.start"}
    checked = 0
    in_pause = 0
    for unit, (s, e) in units.items():
        if unit not in starts or s / 48_000 < pause_at < e / 48_000:
            continue   # a phrase the pause cut in two is legitimately late
        slot = _capture_seconds(events, s) + 4.0
        assert abs(starts[unit] / 48_000 - slot) <= 0.05, (unit, starts[unit] / 48_000, slot)
        checked += 1
        in_pause += pause_at <= slot < pause_at + 10
    assert checked >= len(units) - 1
    assert in_pause >= 1, "no phrase was due during the pause"
    assert not [e for e in events if e["ev"] == "unit.cancel"]
