"""The delivery instruments: can a rendered line be scored against the actor's own line?

`research/dub_delivery.py` ports dub-anything's register measures (pitch, vocal effort, level,
and "arousal" as their robust z-scores within the actor's own lines) and its rank-correlation
follow-through. This is the instrument the next quality levers are judged with — per-line
delivery conditioning especially — so it is tested on synthetic voices with a known answer
rather than trusted because a reference repository uses it.

No models and no audio device: signals are generated here.
"""
from __future__ import annotations

import json
import sys

import numpy as np

from a2a_harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "research"))
import dub_delivery as D  # noqa: E402


def voice(f0: float = 110.0, seconds: float = 1.0, level: float = 0.2, harmonics: int = 6,
          noise: float = 0.0, seed: int = 1) -> np.ndarray:
    """A voiced harmonic stack with vibrato: periodic enough to have a pitch, adjustable
    brightness (spectral tilt) and level, optionally interrupted by silence."""
    t = np.arange(int(seconds * D.SR)) / D.SR
    f = f0 * (1 + 0.012 * np.sin(2 * np.pi * 4.5 * t))
    phase = 2 * np.pi * np.cumsum(f) / D.SR
    x = np.zeros_like(t)
    for k in range(1, harmonics + 1):
        x += np.sin(k * phase) / k
    x *= level / (np.abs(x).max() + 1e-9)
    if noise:
        x = x + noise * np.random.default_rng(seed).standard_normal(len(t))
    return x.astype(np.float32)


def test_pitch_lands_on_a_known_f0():
    # Semitones re 100 Hz: an A2 (110 Hz) actor sits at 12*log2(1.1) = 1.61.
    f = D.features(voice(f0=110.0, seconds=1.2))
    assert f is not None
    assert abs(f["pitch"] - 12 * np.log2(110 / 100)) < 0.6, f


def test_a_richer_voice_measures_as_more_effort():
    dull = D.features(voice(harmonics=2, seconds=1.0))
    bright = D.features(voice(harmonics=16, seconds=1.0))
    assert dull is not None and bright is not None
    assert bright["effort"] > dull["effort"] + 2, (dull["effort"], bright["effort"])


def test_level_follows_amplitude_in_decibels():
    quiet = D.features(voice(level=0.03, seconds=1.0))
    loud = D.features(voice(level=0.3, seconds=1.0))
    assert quiet is not None and loud is not None
    assert 17 < loud["level"] - quiet["level"] < 23, (quiet["level"], loud["level"])


def test_a_short_or_aperiodic_stretch_is_not_measured():
    assert D.features(voice(seconds=0.1)) is None
    # Noise has no pitch. Inventing one would let a garbled take look like it followed the actor.
    noise = D.features(np.random.default_rng(3).standard_normal(D.SR).astype(np.float32) * 0.05)
    assert noise is None or np.isnan(noise["pitch"]), noise


def actor_scene() -> tuple[np.ndarray, np.ndarray, np.ndarray, list[dict]]:
    """Six actor lines whose register climbs (pitch, effort and level together), plus the two
    dub variants scored against them: one that keeps the climb, one that reads everything flat."""
    lines = [voice(f0=100 + 22 * i, level=0.06 + 0.045 * i, harmonics=3 + 2 * i, seconds=1.0, seed=10 + i)
             for i in range(6)]
    # Same semitone and decibel spread as the actor, transposed and re-timed: a take that keeps
    # the shape of the scene without imitating its absolute pitch or level.
    follows = [voice(f0=200 + 44 * i, level=0.10 + 0.075 * i, harmonics=5 + 2 * i, seconds=1.0, seed=40 + i)
               for i in range(6)]
    flat = [voice(f0=160.0, level=0.12, harmonics=6, seconds=1.0, seed=70 + i) for i in range(6)]
    # The wrong shape: quiet and low where the actor rises. A dub that reads the lines in the
    # reverse order of their feeling is what this has to catch.
    inverted = [voice(f0=300 - 40 * i, level=0.40 - 0.06 * i, harmonics=13 - 2 * i, seconds=1.0, seed=90 + i)
                for i in range(6)]
    units = [{"unit": f"u{i}", "src_start": i * 1.1 + 0.02, "src_end": i * 1.1 + 0.98,
              "take_start": i * 1.1 + 0.02, "take_end": i * 1.1 + 0.98} for i in range(6)]
    return join(lines), join(follows), join(flat), join(inverted), units


def join(lines: list[np.ndarray], gap: float = 0.1) -> np.ndarray:
    """Lines one after another with a short silence between them, so each unit window is its
    own line (the take files are one continuous stream, exactly like --translated-out)."""
    pad = np.zeros(int(gap * D.SR), dtype=np.float32)
    out: list[np.ndarray] = []
    for line in lines:
        out += [line, pad]
    return np.concatenate(out)


def variants(source, follows, flat, inverted, units):
    return {v["name"]: v for v in D.score(source, [("follows", follows), ("flat", flat),
                                                   ("inverted", inverted)], units)["variants"]}


def test_a_take_that_keeps_the_actors_climb_scores_high():
    source, follows, flat, inverted, units = actor_scene()
    result = D.score(source, [("follows", follows)], units)
    assert result["status"] == "ok" and result["lines"] == 6
    got = variants(source, follows, flat, inverted, units)["follows"]
    assert got["arousal_corr"] > 0.9, got
    # Rank is exact; the combined spread lands within a third of the actor's.
    assert 0.6 < got["dynamics"] < 1.4, got
    assert got["shape_mae"] < 0.5, got


def test_a_flat_take_scores_as_flat_and_a_reversed_one_worse():
    source, follows, flat, inverted, units = actor_scene()
    got = variants(source, follows, flat, inverted, units)
    # Nothing moves in the flat dub: no ranking to correlate, and no dynamics to lose.
    assert not np.isfinite(got["flat"]["arousal_corr"]), got["flat"]
    assert got["flat"]["dynamics"] < 0.15, got["flat"]
    # The reversed dub is the worst case the instrument must name, not pass.
    assert got["inverted"]["arousal_corr"] < -0.9, got["inverted"]
    assert got["inverted"]["shape_mae"] > got["follows"]["shape_mae"]
    assert got["flat"]["shape_mae"] > got["follows"]["shape_mae"]


def test_offset_and_shape_are_separate_claims():
    """A dub pitched an octave over the actor follows the scene perfectly but sits high on the
    actor's scale; a dub that matches both is the reference case for `bias`."""
    source, follows, flat, inverted, units = actor_scene()
    matched = join([voice(f0=100 + 22 * i, level=0.06 + 0.045 * i, harmonics=3 + 2 * i,
                          seconds=1.0, seed=200 + i) for i in range(6)])
    scored = {v["name"]: v for v in
              D.score(source, [("matched", matched), ("octave up", follows)], units)["variants"]}
    assert abs(scored["matched"]["bias"]) < 0.3 and scored["matched"]["shape_mae"] < 0.5, scored["matched"]
    # An octave-up dub follows the scene perfectly while sitting far above the actor's scale:
    # the two claims are reported apart so one cannot hide the other.
    assert scored["octave up"]["arousal_corr"] > 0.9 and scored["octave up"]["bias"] > 1.0, scored["octave up"]


def test_the_actor_scale_is_shared_so_variants_are_comparable():
    source, follows, flat, inverted, units = actor_scene()
    rows = D.score(source, [("follows", follows)], units)["variants"][0]["rows"]
    # The actor climbs across the scene; the conditioned take must be scored on that same scale.
    actor = [r["actor_arousal"] for r in rows]
    assert actor == sorted(actor) and actor[-1] - actor[0] > 2


def test_fewer_than_four_measurable_lines_is_not_a_number():
    source, follows, _, _, units = actor_scene()
    result = D.score(source, [("follows", follows)], units[:2])
    assert result["status"] == "insufficient_lines"


def test_the_noise_floor_seeps_between_words():
    """`floor` is the gap between the quietest and loudest frames inside the speech: the room
    tone or music a reference carried into the clone (dub-anything's finding 4)."""
    loud, quiet = voice(seconds=0.35), np.zeros(int(0.4 * D.SR), dtype=np.float32)
    clean = np.concatenate([loud, quiet, loud])
    hissy = np.concatenate([loud, 0.02 * np.random.default_rng(5).standard_normal(len(quiet)), loud])
    assert D.floor_db(clean) < -50, D.floor_db(clean)
    assert D.floor_db(hissy) > D.floor_db(clean) + 30, (D.floor_db(clean), D.floor_db(hissy))


def test_units_come_from_the_trace_with_each_side_at_its_own_rate(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text("\n".join([
        json.dumps({"ev": "buffer", "t_ns": 1, "out_sample": 0, "frames": 480, "rate": 48_000}),
        json.dumps({"ev": "commit.source", "unit": "u1", "span": [48_000, 96_000]}),
        json.dumps({"ev": "play.start", "unit": "u1", "out_sample": 2_400}),
        json.dumps({"ev": "play.end", "unit": "u1", "out_sample": 50_400}),
        # A committed unit that never played has no take window and must not be scored.
        json.dumps({"ev": "commit.source", "unit": "u2", "span": [100_000, 120_000]}),
    ]))
    units = D.units_from_trace(trace, source_rate=48_000)
    assert [u["unit"] for u in units] == ["u1"]
    assert units[0]["src_start"] == 1.0 and units[0]["src_end"] == 2.0
    assert units[0]["take_start"] == 0.05 and units[0]["take_end"] == 1.05
