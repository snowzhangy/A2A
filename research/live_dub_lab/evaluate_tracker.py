"""Probe known actor vectors on tracker spans, excluding enrollment-turn overlap.

The labeled enrollments come from the same scene, simulating a pre-enrolled cast;
this cannot establish cross-episode performance or open-set safety.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "research"))
import voice_clone  # noqa: E402
from actor_memory import ActorMemory  # noqa: E402

RATE = 48000


def overlap(a, b):
    return max(0, min(a[1], b[1]) - max(a[0], b[0]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--memory", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    for path in (args.plan, args.trace, args.audio, args.memory, args.out):
        path.resolve().relative_to((ROOT / ".local").resolve())
    if args.out.exists():
        raise FileExistsError("choose a new results file")
    import mlx.core as mx
    import numpy as np
    from mlx_audio.tts.utils import load_model
    from mlx_audio.utils import load_audio
    model_path, manifest = voice_clone.verified_model()
    model = load_model(str(model_path), strict=True)
    model_id = manifest["id"] + "@" + manifest["converted"]["revision"]
    memory = ActorMemory(args.memory, model_id)
    pcm = np.asarray(load_audio(str(args.audio), sample_rate=24000), dtype=np.float32).reshape(-1)
    rows = sorted(json.loads(args.plan.read_text())["rows"], key=lambda r: r["source_start"])
    gold = [(r["speaker"], (round(r["source_start"] * RATE), round(r["source_end"] * RATE))) for r in rows]
    first = {}
    for speaker, span in gold:
        first.setdefault(speaker, span)
    with args.trace.open() as f:
        segments = [e for e in map(json.loads, f) if e["ev"] == "spk.assign"]
    pairs = []
    for e in segments:
        start, end = e["span"]
        if end <= start or any(overlap((start, end), span) for span in first.values()):
            continue
        samples = pcm[start // 2:end // 2]
        if len(samples) < 12000:
            continue
        v = model.extract_speaker_embedding(mx.array(samples))
        mx.eval(v)
        ranked = memory.rank(np.asarray(v).reshape(-1))
        if not ranked:
            raise ValueError("actor memory is empty")
        pairs.append((e["spk"], ranked[0][0], (start, end)))
    memory.close()
    speakers = sorted({p[0] for p in pairs})
    actors = sorted(first)
    if len(speakers) > 8 or len(actors) > 8:
        raise ValueError("diagnostic only supports small scenes")
    def score(mapping):
        return sum(overlap(span, ref) for cluster, _, span in pairs for actor, ref in gold
                   if mapping.get(cluster) == actor)
    mappings = itertools.permutations(actors + [None] * max(0, len(speakers) - len(actors)), len(speakers))
    baseline = max((score(dict(zip(speakers, p))) for p in mappings), default=0)
    correct = sum(overlap(span, ref) for _, predicted, span in pairs for actor, ref in gold if predicted == actor)
    covered = sum(overlap(span, ref) for _, _, span in pairs for _, ref in gold)
    result = {"eligible_tracker_spans": len(pairs), "evaluated_labeled_speech_seconds": round(covered / RATE, 2),
              "qwen_known_cast_correct_fraction": round(correct / covered, 3) if covered else None,
              "tracker_clip_fitted_correct_fraction": round(baseline / covered, 3) if covered else None,
              "scope": "pre-enrolled actors from same scene; no enrollment-turn overlap; no rejection threshold; not cross-episode or live"}
    args.out.write_text(json.dumps(result, indent=2) + "\n")
    args.out.chmod(0o600)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
