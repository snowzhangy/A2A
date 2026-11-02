"""Compare trace spans against manually annotated scene turns (diagnostic only)."""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

RATE = 48000
ROOT = Path(__file__).resolve().parents[2]


def union_overlap(span, intervals):
    intersections = sorted((max(span[0], a), min(span[1], b)) for a, b in intervals
                           if max(span[0], a) < min(span[1], b))
    total = 0
    end = span[0]
    for a, b in intersections:
        total += max(0, b - max(a, end))
        end = max(end, b)
    return total


def audit(plan, events, offset):
    gold = [(r["speaker"], (round((r["source_start"] - offset) * RATE),
                             round((r["source_end"] - offset) * RATE))) for r in plan["rows"]]
    commits = {e["unit"]: e for e in events if e["ev"] == "commit.source"}
    played = {e["unit"] for e in events if e["ev"] == "play.start"}
    durations = sum(b - a for _, (a, b) in gold)
    committed_spans = [e["span"] for e in commits.values()]
    played_spans = [e["span"] for unit, e in commits.items() if unit in played]
    assign = [e for e in events if e["ev"] == "spk.assign"]
    speakers = sorted({e["spk"] for e in assign})
    actors = sorted({actor for actor, _ in gold})
    if len(speakers) > 8 or len(actors) > 8:
        raise ValueError("diagnostic only supports small scenes")
    # A mapping chosen from this same clip is descriptive, not an ID evaluation.
    def score(mapping):
        return sum(union_overlap(span, [e["span"] for e in assign if mapping.get(e["spk"]) == actor])
                   for actor, span in gold)
    candidates = itertools.permutations(actors + [None] * max(0, len(speakers) - len(actors)), len(speakers))
    correct = max((score(dict(zip(speakers, p))) for p in candidates), default=0)
    assigned = sum(union_overlap(span, [e["span"] for e in assign]) for _, span in gold)
    return {"gold_turns": len(gold), "gold_speech_seconds": round(durations / RATE, 2),
            "gold_span_covered_by_commits": round(sum(union_overlap(s, committed_spans) for _, s in gold) / durations, 3),
            "gold_span_covered_by_played_commits": round(sum(union_overlap(s, played_spans) for _, s in gold) / durations, 3),
            "tracker_labels": len(speakers), "gold_actors": len(actors),
            "gold_span_assigned": round(assigned / durations, 3),
            "gold_span_assigned_to_best_clip_fitted_actor": round(correct / durations, 3),
            "warning": "Span overlap is not semantic completeness; mapping fits this same scene and does not prove identity."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--offset", type=float, default=0)
    args = parser.parse_args()
    for path in (args.plan, args.trace):
        path.resolve().relative_to((ROOT / ".local").resolve())
    plan = json.loads(args.plan.read_text())
    with args.trace.open() as f:
        events = [json.loads(line) for line in f]
    print(json.dumps(audit(plan, events, args.offset), indent=2))


if __name__ == "__main__":
    main()
