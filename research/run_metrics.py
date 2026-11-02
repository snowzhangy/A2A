"""Exploratory summary of a file-mode a2a-pipeline run (trace.jsonl in a run directory).

Units, played units, source coverage, onset lag (source onset to first English sample on the
offline output timeline), TTS first-chunk latency, commit reasons, dispositions, fallbacks,
synthesized English seconds per Mandarin source character, and clone pacing (`tts.done`:
speed-up applied, share of units at the 1.3x cap, and native length over the source span).
Diagnostic only: not the harness H2 metrics, not gate evidence. Needs --research-text traces
for the per-char ratio.

usage: python research/run_metrics.py RUN_DIR [RUN_DIR ...]
"""
from __future__ import annotations

import collections
import json
import re
import statistics
import sys

RATE = 48_000
PUNCT = re.compile(r"[\s，。？！、；：,.?!;:]")


def pct(values: list[float], q: float) -> float | None:
    return round(sorted(values)[min(len(values) - 1, int(q * len(values)))], 3) if values else None


def summarize(run: str) -> dict:
    with open(f"{run}/trace.jsonl", encoding="utf-8") as source:
        events = [json.loads(line) for line in source]
    by = collections.defaultdict(dict)
    for e in events:
        if "unit" in e and e["ev"] in ("commit.source", "tts.request", "tts.first_chunk", "play.start", "play.end"):
            by[e["ev"]][e["unit"]] = e
    commits, play = by["commit.source"], by["play.start"]
    completed = by["play.end"]
    english = collections.Counter()
    for e in events:
        if e["ev"] in ("tts.first_chunk", "tts.chunk"):
            english[e["unit"]] += e["frames"] / e["rate"]
    lags = [(play[u]["out_sample"] - c["span"][0]) / RATE for u, c in commits.items() if u in play]
    first = [(by["tts.first_chunk"][u]["t_ns"] - r["t_ns"]) / 1e9 for u, r in by["tts.request"].items()
             if u in by["tts.first_chunk"]]
    seconds = lambda c: (c["span"][1] - c["span"][0]) / RATE
    source_total = sum(seconds(c) for c in commits.values()) or 1e-9
    per_char, ratio = [], []
    for u in by["play.end"]:
        c = commits.get(u)
        if c and english[u]:
            chars = len(PUNCT.sub("", c.get("text", "")))
            if chars:
                per_char.append(english[u] / chars)
            ratio.append(english[u] / max(seconds(c), 1e-9))
    done = [e for e in events if e["ev"] == "tts.done"]
    paces = [e["pace"] for e in done]
    native_over_target = [e["native_ms"] / e["target_ms"] for e in done if e.get("target_ms")]
    return {
        "run": run,
        "units": len(commits),
        "played": len(play),
        "completed": len(completed),
        "coverage_by_source_seconds": round(sum(seconds(commits[u]) for u in play if u in commits) / source_total, 3),
        "completed_coverage_by_source_seconds": round(sum(seconds(commits[u]) for u in completed if u in commits) / source_total, 3),
        "lag_onset_s": {"median": round(statistics.median(lags), 2) if lags else None, "p90": pct(lags, 0.9),
                        "max": round(max(lags), 2) if lags else None},
        "tts_first_chunk_s": {"median": round(statistics.median(first), 3) if first else None, "max": pct(first, 1.0)},
        "english_s_per_source_char": {"median": round(statistics.median(per_char), 3) if per_char else None,
                                      "p90": pct(per_char, 0.9)},
        "english_over_source_seconds": round(statistics.median(ratio), 2) if ratio else None,
        "commit_reasons": dict(collections.Counter(c["reason"] for c in commits.values())),
        "dispositions": dict(collections.Counter(f"{e['disposition']}/{e['reason']}" for e in events
                                                 if e["ev"] == "source.disposition" and "unit" in e)),
        "fallbacks": dict(collections.Counter(e["reason"] for e in events if e["ev"] == "fallback.enter")),
        "pacing": {"units": len(done), "pace_median": round(statistics.median(paces), 3) if paces else None,
                   "at_cap": sum(p >= 1.299 for p in paces),
                   # A take slower than the model's own pace was filled out to the actor's end.
                   "filled": sum(p <= 0.999 for p in paces),
                   "fill_median": round(statistics.median([p for p in paces if p <= 0.999]), 3)
                   if any(p <= 0.999 for p in paces) else None,
                   "native_over_target_median": round(statistics.median(native_over_target), 2) if native_over_target else None,
                   "native_over_target_p90": pct(native_over_target, 0.9)},
    }


if __name__ == "__main__":
    for run in sys.argv[1:]:
        print(json.dumps(summarize(run), ensure_ascii=False))
