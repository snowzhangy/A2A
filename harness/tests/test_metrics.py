"""Known-answer tests for trace-derived metrics (H2 subset that needs no models)."""

import textwrap

import pytest

from a2a_harness import fixture, metrics


def test_edit_distances():
    assert metrics.levenshtein("kitten", "sitting") == 3
    assert metrics.cer("我们下周开会。", "我们下周开会") == 0.0           # punctuation ignored
    assert metrics.cer("我们下周开会", "我们下开会") == pytest.approx(1 / 6)
    assert metrics.wer("We will meet next week.", "we will meet next week") == 0.0
    assert metrics.wer("we do not agree", "we do agree") == pytest.approx(0.25)


def test_time_map_interpolates_and_extrapolates():
    m = metrics.TimeMap.from_anchors([(0, 1_000_000_000), (48_000, 2_000_000_000)])
    assert m.t_ns(24_000) == pytest.approx(1.5e9)
    assert m.t_ns(96_000) == pytest.approx(3.0e9)
    assert m.t_ns(-48_000) == pytest.approx(0.0)


@pytest.fixture
def tiny_fixture(tmp_path):
    (tmp_path / "a.wav").write_bytes(b"")
    (tmp_path / "f.yaml").write_text(textwrap.dedent("""\
        id: tiny
        kind: synthetic
        license: test
        audio: a.wav
        sample_rate: 48000
        duration_s: 4.0
        warm_interval_s: [0.0, 4.0]
        reference:
          units:
            - text: 好的。
              chars:
                - {c: 好, start: 1.0, end: 1.2}
                - {c: 的, start: 1.2, end: 1.4}
                - {c: 。, start: 1.4, end: 1.4}
            - text: 没有。
              chars:
                - {c: 没, start: 2.0, end: 2.2}
                - {c: 有, start: 2.2, end: 2.4}
                - {c: 。, start: 2.4, end: 2.4}
        """), encoding="utf-8")
    return fixture.load(tmp_path / "f.yaml")


def ev(t_ms, kind, **kw):
    return {"v": 0, "t_ns": int(t_ms * 1e6), "session": "s", "epoch": 1, "gen": 1, "ev": kind, **kw}


def test_lag_coverage_and_disposition(tiny_fixture):
    # Source sample n is live at n/48 ms (capture anchors); output sample n plays at n/48 ms.
    events = [ev(t, "buf.capture", span=[(t - 20) * 48, t * 48], frames=960) for t in range(20, 4020, 20)]
    events += [ev(t, "buf.output", out_sample=t * 48, frames=960) for t in range(0, 4000, 20)]
    events += [
        ev(1500, "commit.source", unit="u1", spk="S1", span=[48_000, 67_200], reason="finalized",
           timing_estimated=False, wait_ms=0, text="好的。"),
        ev(1900, "play.start", unit="u1", out_sample=int(3.0 * 48_000)),   # plays at 3.0 s
        ev(2500, "play.end", unit="u1", out_sample=int(3.5 * 48_000)),
        ev(2500, "source.disposition", unit="u1", span=[48_000, 67_200], disposition="translated", reason="played"),
        ev(2600, "commit.source", unit="u2", spk="S1", span=[96_000, 115_200], reason="finalized",
           timing_estimated=False, wait_ms=0, text="没有。"),
        ev(2700, "unit.cancel", unit="u2", reason="fallback"),
        ev(2700, "source.disposition", unit="u2", span=[96_000, 115_200], disposition="abandoned", reason="fallback"),
        ev(2700, "fallback.enter", reason="debt"),
        ev(1000, "queue.depth", debt_ms=100, oldest_source_age_ms=0),
        ev(3000, "queue.depth", debt_ms=100, oldest_source_age_ms=0),
        ev(3900, "queue.depth", debt_ms=100, oldest_source_age_ms=0),
    ]
    events.sort(key=lambda e: e["t_ns"])
    m = metrics.compute(events, tiny_fixture)
    assert m["units_aligned"] == 2
    # Unit 1: first source char onset 1.0 s, translated audio starts 3.0 s.
    assert m["lag_onset_s"]["median"] == pytest.approx(2.0, abs=1e-6)
    assert m["lag_end_s"]["median"] == pytest.approx(1.6, abs=1e-6)
    # Two of four content characters finished playing as translation.
    assert m["translation_coverage"] == pytest.approx(0.5)
    assert m["source_disposition"] == {"translated": 1, "abandoned": 1}
    assert m["fallbacks"] == 1
    assert m["debt_ms"]["slope_per_min"] == pytest.approx(0.0, abs=1e-6)
    gate = metrics.streaming_gate(m)
    assert gate["checks"]["lag_onset_median"] and not gate["checks"]["coverage"]


def test_turn_timing_measures_dead_air_and_overrun_against_the_actor(tiny_fixture):
    """Whether each line takes its actor's time (`turn_dead_air_s` / `turn_overrun_s`).

    dub-anything re-times every line to end where the actor stops; this is the instrument for
    whether a live run does the same. Both sides are durations, so the measure holds whatever
    delay the run plays with: source span seconds against seconds actually on the bus."""
    events = [ev(t, "buf.capture", span=[(t - 20) * 48, t * 48], frames=960) for t in range(20, 8020, 20)]
    events += [ev(t, "buf.output", out_sample=t * 48, frames=960) for t in range(0, 8000, 20)]
    events += [
        # u1: 0.4 s of actor speech, 0.2 s of English: the tail of their turn is left silent.
        ev(1500, "commit.source", unit="u1", spk="S1", span=[48_000, 67_200], reason="finalized",
           timing_estimated=False, wait_ms=0, text="好的。"),
        ev(1500, "play.start", unit="u1", out_sample=int(1.0 * 48_000)),
        ev(1520, "play.end", unit="u1", out_sample=int(1.2 * 48_000)),
        # u2: 0.4 s of actor speech, 0.5 s of English: it runs past their last sound.
        ev(2600, "commit.source", unit="u2", spk="S1", span=[96_000, 115_200], reason="finalized",
           timing_estimated=False, wait_ms=0, text="没有。"),
        ev(2600, "play.start", unit="u2", out_sample=int(2.4 * 48_000)),
        ev(2900, "play.end", unit="u2", out_sample=int(2.9 * 48_000)),
        # u3: an artificial cut mid-speech has no actor end to land on, so it is not counted.
        ev(5000, "commit.source", unit="u3", spk="S1", span=[200_000, 344_000], reason="max_length",
           timing_estimated=True, wait_ms=0, text="好的。"),
        ev(5000, "play.start", unit="u3", out_sample=int(5.0 * 48_000)),
        ev(5100, "play.end", unit="u3", out_sample=int(5.1 * 48_000)),
        # Never played: no English to place.
        ev(6000, "commit.source", unit="u4", spk="S1", span=[300_000, 320_000], reason="finalized",
           timing_estimated=False, wait_ms=0, text="好的。"),
    ]
    events.sort(key=lambda e: e["t_ns"])
    m = metrics.compute(events, tiny_fixture)
    assert m["turn_dead_air_s"]["n"] == 1 and m["turn_dead_air_s"]["median"] == pytest.approx(0.2, abs=1e-6)
    assert m["turn_overrun_s"]["n"] == 1 and m["turn_overrun_s"]["median"] == pytest.approx(0.1, abs=1e-6)
    assert m["turn_timing_n"] == 2
    gaps = {r["unit"]: r["turn_gap_s"] for r in m["per_unit"] if "turn_gap_s" in r}
    assert gaps == {"u1": pytest.approx(0.2), "u2": pytest.approx(-0.1)}


def test_future_access_violation_is_detected():
    feed = [{"t_ns": 1_000, "start_frames": 0, "frames": 960}, {"t_ns": 2_000, "start_frames": 960, "frames": 1_920}]
    ok = [ev(0.0015, "buf.capture", span=[0, 960], frames=960)]
    bad = [ev(0.0015, "asr.partial", span=[0, 1_920])]
    assert metrics.future_access_violations(ok, feed) == []
    assert len(metrics.future_access_violations(bad, feed)) == 1


def test_alignment_handles_repeated_phrases(tiny_fixture):
    a = metrics.align_units([("u1", "好的"), ("u2", "没有"), ("u3", "好的")], tiny_fixture)
    assert a.unit_chars["u1"] == [0, 1]
    assert a.unit_chars["u2"] == [3, 4]
    assert "u3" in a.unmatched_units   # never re-uses characters already assigned


def test_anchors_without_valid_host_time_are_ignored():
    events = [ev(1000, "buf.output", out_sample=48_000, frames=960),
              ev(9999, "buf.output", out_sample=96_000, frames=960, host_time_valid=False)]
    m = metrics.output_map(events)
    assert m.samples == [48_000]
