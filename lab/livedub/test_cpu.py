"""Deterministic checks for the live lab. No models, no GPU, no network.

    python test_cpu.py
"""
from __future__ import annotations

import sys

import numpy as np

from mixer import Line, Mixer, budget_words, fit, match_gain
from paths import ASR_RATE, MIX_RATE
from segmenter import Segmenter

OK = []


def check(name: str, cond: bool, detail: str = "") -> None:
    OK.append((name, cond, detail))
    print(f"{'ok  ' if cond else 'FAIL'} {name}{'' if cond else '  ' + detail}")


def test_fit() -> None:
    # ready before it is needed: the line lands where the actor stopped, shifted by the delay
    s = fit(end_input=4.4, delay=2.5, ready_at=6.0, duration=2.0, free_until=9.0, last_end=0.0)
    check("fit: on time", abs(s.start - 6.9) < 1e-6 and s.why == "on time", str(s))

    # not ready yet: late, but never cut
    s = fit(end_input=4.4, delay=2.5, ready_at=8.0, duration=3.0, free_until=20.0, last_end=0.0)
    check("fit: late keeps its length", abs(s.start - 8.0) < 1e-6 and s.why == "late", str(s))

    # previous line still playing: wait for it rather than start two voices together
    s = fit(end_input=4.4, delay=2.5, ready_at=6.0, duration=2.0, free_until=20.0, last_end=7.2)
    check("fit: pushed behind the previous line", abs(s.start - 7.25) < 1e-6 and s.why == "pushed", str(s))

    # a collision is resolved by waiting, not by talking over the previous line
    s = fit(end_input=4.4, delay=2.5, ready_at=6.0, duration=6.0, free_until=16.0, last_end=9.0)
    check("fit: a collision never overlaps", s.start >= 9.0 and s.tempo == 1.0, str(s))

    # a line longer than the silence in front of it: speak faster rather than run on
    s = fit(end_input=4.4, delay=2.5, ready_at=6.0, duration=3.8, free_until=10.0, last_end=0.0)
    check("fit: sped to fit the gap", s.why == "sped" and s.tempo > 1.0, str(s))

    # too long for the silence available even at the speed cap: give up on this one
    s = fit(end_input=4.4, delay=2.5, ready_at=6.0, duration=9.0, free_until=8.0, last_end=0.0)
    check("fit: skipped rather than crowding", s.why == "skipped:busy", str(s))

    # a line too long to ever finish inside the budget
    s = fit(end_input=4.4, delay=2.5, ready_at=6.0, duration=30.0, free_until=40.0, last_end=0.0)
    check("fit: refuses an over-long line", s.why == "skipped:too-long", str(s))

    check("budget: 3 s at 2.6 w/s", budget_words(3.0, 2.6) == 7, str(budget_words(3.0, 2.6)))


def test_mixer() -> None:
    rate = MIX_RATE
    m = Mixer(delay=1.0, duck=0.5, ramp=0.01)
    tone = np.ones(rate * 4, dtype=np.float32) * 0.5
    m.add_input(tone)
    out = np.concatenate([m.block(1000) for _ in range(rate * 4 // 1000)])
    check("mixer: silence during the delay", float(np.max(np.abs(out[: rate // 2]))) == 0.0)
    mid = out[int(2.5 * rate): int(3.0 * rate)]
    check("mixer: bed plays delayed", np.allclose(mid, 0.5, atol=1e-6), f"{mid[:3]}")

    m = Mixer(delay=0.5, duck=0.5, ramp=0.01)
    m.add_input(np.ones(rate * 3, dtype=np.float32) * 0.6)
    line = Line(index=0, start=1.0, planned=1.0)
    line.buffer(np.ones(rate, dtype=np.float32) * 0.2)
    line.close()
    m.lines.append(line)
    out = np.concatenate([m.block(500) for _ in range(rate * 3 // 500)])
    ducked = out[int(1.2 * rate)]
    plain = out[int(0.9 * rate)]
    loud = ducked + plain           # ducked bed 0.3 + line 0.2
    check("mixer: bed ducks under the line", abs(ducked - 0.5) < 0.05, f"{ducked:.3f}")
    check("mixer: bed back to full afterwards", abs(out[int(2.4 * rate)] - 0.6) < 0.02,
          f"{out[int(2.4 * rate)]:.3f}")
    check("mixer: the line played from where it was put", abs(line.played_from - 1.0) < 0.02,
          str(line.played_from))
    check("mixer: nothing clipped", m.clipped == 0, str(m.clipped))

    # A line that arrives while it plays: playback starts on the first chunk and never stutters.
    m = Mixer(delay=0.0, duck=1.0, ramp=0.01)
    slow = Line(index=1, start=0.0, planned=1.0)
    m.lines.append(slow)
    blocks = []
    for k in range(6):
        if k == 1:
            slow.buffer(np.ones(rate // 4, dtype=np.float32))     # a quarter second in, late
        blocks.append(m.block(rate // 10))
    out = np.concatenate(blocks)
    check("mixer: silence until the first chunk arrives", float(np.max(np.abs(out[: rate // 10]))) == 0.0)
    check("mixer: plays what arrived late", float(np.max(np.abs(out[rate // 10: rate // 2]))) > 0.5)
    check("mixer: counts the underrun instead of repeating audio", slow.underruns > 0,
          str(slow.underruns))
    check("mixer: an open line is not dropped", m.lines == [slow])


def test_gain() -> None:
    loud = np.tile(np.repeat([0.3, -0.3, 0.25, -0.25], 200), 20).astype(np.float32)
    quiet = loud * 0.25
    g = match_gain(quiet, loud)
    check("gain: a quiet line is lifted toward the actor", 3.0 <= g <= 4.1, f"{g:.2f}")
    check("gain: identical audio stays at 1", abs(match_gain(loud, loud) - 1.0) < 0.05)
    check("gain: silence does not divide by zero", match_gain(np.zeros(100), loud) == 1.0)


def _run(seg: Segmenter, audio: np.ndarray) -> list:
    out = []
    for i in range(0, len(audio), 320):
        out += seg.push(audio[i:i + 320])
    return out + seg.finish()


def test_segmenter() -> None:
    """On real dialogue, against the actor windows the offline dub was directed on.

    Silero is trained on speech, so synthetic noise bursts prove nothing; this scene has
    published windows measured by hand for the same reason."""
    from check_segmenter import CLIP_START, EPISODE, windows
    from paths import load
    audio = load(EPISODE, CLIP_START, CLIP_START + 37.0, ASR_RATE)
    refs = windows()

    out = _run(Segmenter(min_silence=0.25), audio)
    check("segmenter: finds the utterances", len(out) >= 7, str(len(out)))
    natural = [u.wait for u in out if not u.forced]
    check("segmenter: waits about one min_silence after speech",
          bool(natural) and min(natural) >= 0.1 and max(natural) <= 0.35,
          str([round(w, 2) for w in natural]))
    check("segmenter: the ceiling bounds the wait on a long run",
          max(u.wait for u in out) <= 1.5, str(round(max(u.wait for u in out), 2)))
    check("segmenter: units never overlap", all(a.end <= b.start + 1e-6 for a, b in zip(out, out[1:])),
          str([(round(a.end, 2), round(b.start, 2)) for a, b in zip(out, out[1:])
               if a.end > b.start]))

    # What the endpoint costs, on measured actor windows. Lines closer together than min_silence
    # share a unit: that is the design (a live cut must fall in a gap), so merges are counted and
    # bounded rather than treated as errors.
    from check_segmenter import covered_by, cuts, merges
    check("segmenter: most directed lines land whole in one unit",
          covered_by(out, refs) >= 8, f"{covered_by(out, refs)}/{len(refs)}")
    check("segmenter: merges stay rare", merges(out, refs) <= 3, str(merges(out, refs)))
    check("segmenter: few lines are split", cuts(out, refs) <= 4, str(cuts(out, refs)))

    capped = _run(Segmenter(min_silence=0.25, max_speech=2.0), audio)
    check("segmenter: caps a run of speech", all(u.duration <= 2.25 for u in capped),
          str([round(u.duration, 2) for u in capped]))
    check("segmenter: capping keeps the speech", 
          0.9 <= sum(u.duration for u in capped) / sum(u.duration for u in out) <= 1.25,
          f"{sum(u.duration for u in capped):.1f} vs {sum(u.duration for u in out):.1f}")
    check("segmenter: pieces stay in order",
          all(a.end <= b.start + 1e-6 for a, b in zip(capped, capped[1:])))


def main() -> int:
    test_fit()
    test_mixer()
    test_gain()
    test_segmenter()
    bad = [n for n, ok, _ in OK if not ok]
    print(f"\n{len(OK) - len(bad)}/{len(OK)} checks passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
