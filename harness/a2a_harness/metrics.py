"""Trace-derived metrics (IMPLEMENTATION_PLAN.md section 4.6).

Everything is computed from an immutable raw run: the trace, the fixture's independent
reference alignment, and optionally the harness feed log. Live spans are diagnostics only;
gate latency uses the reference alignment.
"""

from __future__ import annotations

import bisect
import difflib
import unicodedata
from dataclasses import dataclass, field

import numpy as np

from . import CANONICAL_RATE
from .fixture import Fixture

PUNCT = set("，。！？；：、,.!?;:\"'“”‘’…（）()《》-—")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in text if not ch.isspace() and ch not in PUNCT)


# MARK: Edit distance


def levenshtein(a, b) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(reference: str, hypothesis: str) -> float:
    ref, hyp = normalize(reference), normalize(hypothesis)
    return levenshtein(ref, hyp) / max(1, len(ref))


def wer(reference: str, hypothesis: str) -> float:
    def words(s: str) -> list[str]:
        s = unicodedata.normalize("NFKC", s).lower()
        return "".join(ch if ch.isalnum() or ch.isspace() or ch == "'" else " " for ch in s).split()

    ref, hyp = words(reference), words(hypothesis)
    return levenshtein(ref, hyp) / max(1, len(ref))


# MARK: Time maps


@dataclass
class TimeMap:
    """Piecewise-linear sample → t_ns map from (sample, t_ns) anchors."""

    samples: list[int]
    times: list[int]
    rate: int = CANONICAL_RATE

    @classmethod
    def from_anchors(cls, anchors: list[tuple[int, int]], rate: int = CANONICAL_RATE) -> "TimeMap":
        anchors = sorted(set(anchors))
        return cls([a for a, _ in anchors], [t for _, t in anchors], rate)

    def __bool__(self) -> bool:
        return bool(self.samples)

    def t_ns(self, sample: float) -> float:
        if not self.samples:
            raise ValueError("empty time map")
        i = bisect.bisect_left(self.samples, sample)
        if i < len(self.samples) and self.samples[i] == sample:
            return float(self.times[i])
        if i == 0:
            return self.times[0] - (self.samples[0] - sample) * 1e9 / self.rate
        if i == len(self.samples):
            return self.times[-1] + (sample - self.samples[-1]) * 1e9 / self.rate
        s0, s1 = self.samples[i - 1], self.samples[i]
        t0, t1 = self.times[i - 1], self.times[i]
        return t0 + (t1 - t0) * (sample - s0) / (s1 - s0)


def source_map(events: list[dict], feed_log: list[dict] | None = None) -> TimeMap:
    """When each source sample became live. The harness feed log is independent of the
    pipeline; capture anchors are the fallback (the end of each delivered buffer)."""
    if feed_log:
        # A chunk's first frame became live when the harness started writing it.
        return TimeMap.from_anchors([(int(e.get("start_frames", e["frames"])), int(e["t_ns"])) for e in feed_log])
    # A live trace has device capture records (with `device_index` and the device host time)
    # beside the pipeline's delivery records; never mix the two clocks.
    captures = [e for e in events if e["ev"] == "buf.capture"]
    if any("device_index" in e for e in captures):
        captures = [e for e in captures if "device_index" in e]
    return TimeMap.from_anchors([(e["span"][1], e["t_ns"]) for e in captures if e.get("host_time_valid", True)])


def output_map(events: list[dict]) -> TimeMap:
    # Records without a valid device host time carry a substituted time; never use them as anchors.
    records = [e for e in events if e["ev"] == "buf.output" and e.get("host_time_valid", True)]
    rates = {e["rate"] for e in records if e.get("rate", 0) > 0}
    rate = int(next(iter(rates))) if len(rates) == 1 else CANONICAL_RATE
    return TimeMap.from_anchors([(e["out_sample"], e["t_ns"]) for e in records], rate=rate)


# MARK: Unit alignment


@dataclass
class Alignment:
    unit_chars: dict[str, list[int]] = field(default_factory=dict)  # unit → reference content-char indices
    unmatched_units: list[str] = field(default_factory=list)


def align_units(units: list[tuple[str, str]], fixture: Fixture) -> Alignment:
    """Sequential edit-distance alignment of committed unit text to reference characters.
    Repeated phrases are resolved left to right from a moving cursor; never re-used."""
    content = [i for i, c in enumerate(fixture.ref_chars) if normalize(c.char)]
    ref = "".join(normalize(fixture.ref_chars[i].char) for i in content)
    result = Alignment()
    cursor = 0
    used: set[int] = set()
    for unit, text in units:
        t = normalize(text)
        if not t:
            result.unmatched_units.append(unit)
            continue
        window_end = min(len(ref), cursor + 3 * len(t) + 40)
        matcher = difflib.SequenceMatcher(None, ref[cursor:window_end], t, autojunk=False)
        matched = []
        for block in matcher.get_matching_blocks():
            for k in range(block.size):
                idx = cursor + block.a + k
                if idx not in used:
                    matched.append(idx)
        if len(matched) < max(1, len(t) // 2):
            result.unmatched_units.append(unit)
            continue
        used.update(matched)
        result.unit_chars[unit] = [content[i] for i in matched]
        cursor = max(matched) + 1
    return result


# MARK: Metrics


def _pct(values: list[float], q: float) -> float | None:
    return float(np.percentile(values, q)) if values else None


def _slope_per_minute(ts: list[float], ys: list[float]) -> float | None:
    if len(ts) < 3:
        return None
    t = (np.asarray(ts) - ts[0]) / 60e9
    if np.ptp(t) == 0:
        return None
    return float(np.polyfit(t, np.asarray(ys, dtype=float), 1)[0])


def compute(events: list[dict], fixture: Fixture, *, device_latency_ms: float = 0.0,
            feed_log: list[dict] | None = None) -> dict:
    src = source_map(events, feed_log)
    out = output_map(events)
    chars = fixture.ref_chars

    commits: dict[str, dict] = {}
    play_start: dict[str, int] = {}
    play_end: dict[str, int] = {}
    disposition: dict[str, str] = {}
    disposition_counts: dict[str, int] = {}
    fallbacks = []
    catch_ups = []
    queue = []
    for e in events:
        ev = e["ev"]
        if ev == "commit.source":
            commits[e["unit"]] = e
        elif ev == "play.start":
            play_start.setdefault(e["unit"], e["out_sample"])
        elif ev == "play.end":
            play_end[e["unit"]] = e["out_sample"]
        elif ev == "source.disposition":
            disposition_counts[e["disposition"]] = disposition_counts.get(e["disposition"], 0) + 1
            if "unit" in e:
                disposition[e["unit"]] = e["disposition"]
        elif ev == "fallback.enter":
            fallbacks.append(e)
        elif ev == "catch_up":
            catch_ups.append(e)
        elif ev == "queue.depth":
            queue.append(e)

    has_text = all("text" in c for c in commits.values())
    alignment = align_units([(u, c.get("text", "")) for u, c in commits.items()], fixture) if has_text else Alignment()

    rate = CANONICAL_RATE
    lag_onset, lag_live, lag_end, unit_lengths, weights = [], [], [], [], []
    per_unit = []
    for unit, c in commits.items():
        if unit not in play_start or not out:
            continue
        t_play = out.t_ns(play_start[unit]) + device_latency_ms * 1e6
        row = {"unit": unit, "play_start_ns": t_play}
        row["lag_live_s"] = (t_play - src.t_ns(c["span"][0])) / 1e9
        lag_live.append(row["lag_live_s"])
        idx = alignment.unit_chars.get(unit)
        if idx:
            first, last = chars[min(idx)], chars[max(idx)]
            row["lag_onset_s"] = (t_play - src.t_ns(first.start * rate)) / 1e9
            row["lag_end_s"] = (t_play - src.t_ns(last.end * rate)) / 1e9
            row["chars"] = len(idx)
            lag_onset.append(row["lag_onset_s"])
            lag_end.append(row["lag_end_s"])
            unit_lengths.append(len(idx))
            weights.append(len(idx))
        per_unit.append(row)

    # Coverage over the predefined warm interval: unique reference characters whose units
    # finished playing as translations.
    w0, w1 = fixture.warm_interval_s
    warm = [i for i, c in enumerate(chars) if normalize(c.char) and w0 <= c.start < w1]
    covered: set[int] = set()
    for unit, idx in alignment.unit_chars.items():
        if disposition.get(unit) == "translated" and unit in play_end:
            covered.update(i for i in idx if w0 <= chars[i].start < w1)
    coverage = len(covered) / len(warm) if warm else None

    warm_ns = (src.t_ns(w0 * rate), src.t_ns(w1 * rate)) if src else (0, float("inf"))
    q = [e for e in queue if warm_ns[0] <= e["t_ns"] <= warm_ns[1]]
    debt_slope = _slope_per_minute([e["t_ns"] for e in q], [e["debt_ms"] for e in q])
    age_slope = _slope_per_minute([e["t_ns"] for e in q], [e["oldest_source_age_ms"] for e in q])

    weighted_median = None
    if lag_onset:
        order = np.argsort(lag_onset)
        cum = np.cumsum(np.asarray(weights)[order])
        weighted_median = float(np.asarray(lag_onset)[order][np.searchsorted(cum, cum[-1] / 2)])

    return {
        "units_committed": len(commits),
        "units_played": len(play_start),
        "units_aligned": len(alignment.unit_chars),
        "unmatched_units": alignment.unmatched_units,
        "lag_onset_s": {"median": _pct(lag_onset, 50), "p95": _pct(lag_onset, 95), "max": max(lag_onset, default=None),
                        "char_weighted_median": weighted_median, "n": len(lag_onset)},
        "lag_live_s": {"median": _pct(lag_live, 50), "p95": _pct(lag_live, 95), "n": len(lag_live)},
        "lag_end_s": {"median": _pct(lag_end, 50), "p95": _pct(lag_end, 95), "n": len(lag_end)},
        "unit_chars": {"median": _pct(unit_lengths, 50), "p95": _pct(unit_lengths, 95)},
        "translation_coverage": coverage,
        "warm_interval_s": [w0, w1],
        "source_disposition": disposition_counts,
        "fallbacks": len(fallbacks),
        "fallback_reasons": sorted({f.get("reason", "?") for f in fallbacks}),
        # Fluent playout skips unheard units instead of falling back.
        "catch_ups": len(catch_ups),
        "catch_up_units": sum(e.get("units", 0) for e in catch_ups),
        "debt_ms": {"max": max((e["debt_ms"] for e in q), default=None), "slope_per_min": debt_slope},
        "oldest_source_age_ms": {"max": max((e["oldest_source_age_ms"] for e in q), default=None),
                                 "slope_per_min": age_slope},
        "alignment_uncertainty_ms": fixture.alignment_uncertainty_ms,
        "device_latency_ms_assumed": device_latency_ms,
        "per_unit": per_unit,
    }


@dataclass(frozen=True)
class GateThresholds:
    """Week-2 streaming end-to-end row (section 9). Frozen before a run."""

    lag_onset_median_s: float = 2.5
    lag_onset_p95_s: float = 4.0
    coverage: float = 0.95
    flat_slope_ms_per_min: float = 100.0


def streaming_gate(m: dict, t: GateThresholds = GateThresholds()) -> dict:
    lag = m["lag_onset_s"]
    checks = {
        "lag_onset_median": lag["median"] is not None and lag["median"] <= t.lag_onset_median_s,
        "lag_onset_p95": lag["p95"] is not None and lag["p95"] <= t.lag_onset_p95_s,
        "coverage": m["translation_coverage"] is not None and m["translation_coverage"] >= t.coverage,
        "debt_flat": m["debt_ms"]["slope_per_min"] is None or m["debt_ms"]["slope_per_min"] <= t.flat_slope_ms_per_min,
        "source_age_flat": m["oldest_source_age_ms"]["slope_per_min"] is None
        or m["oldest_source_age_ms"]["slope_per_min"] <= t.flat_slope_ms_per_min,
    }
    return {"thresholds": t.__dict__, "checks": checks, "pass": all(checks.values())}


def future_access_violations(events: list[dict], feed_log: list[dict]) -> list[str]:
    """Every source span a pipeline event references must have been delivered by the
    harness before the event's time. Both sides use the host monotonic clock."""
    if not feed_log:
        return ["no feed log"]
    times = [int(e["t_ns"]) for e in feed_log]
    frames = [int(e["frames"]) for e in feed_log]
    violations = []
    for e in events:
        end = None
        if e["ev"] in {"buf.capture", "asr.partial", "asr.final", "commit.source"} and "span" in e:
            end = e["span"][1]
        elif e["ev"] in {"vad.start", "vad.end"}:
            end = e["src_sample"]
        if end is None:
            continue
        i = bisect.bisect_right(times, e["t_ns"]) - 1
        available = frames[i] if i >= 0 else 0
        if end > available:
            violations.append(f"{e['ev']} at t={e['t_ns']} references sample {end} > delivered {available}")
    return violations
