"""Evaluate separate-turn speaker matching on the user's local scene; no downloads.

Inputs are restricted, locally created annotations. Outputs stay under .local.
This oracle-segment experiment does not measure streaming diarization or cross-episode ID.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "research"))
import voice_clone  # noqa: E402  Sets offline mode before model libraries load.
from actor_memory import ActorMemory  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    local = (ROOT / ".local").resolve()
    for path in (args.plan, args.audio, args.out):
        path.resolve().relative_to(local)
    if args.out.exists():
        raise FileExistsError("choose a new output directory")
    args.out.mkdir(mode=0o700, parents=True)
    os.chmod(args.out, 0o700)

    import mlx.core as mx
    import numpy as np
    from mlx_audio.tts.utils import load_model
    from mlx_audio.utils import load_audio

    model_path, manifest = voice_clone.verified_model()
    model_id = manifest["id"] + "@" + manifest["converted"]["revision"]
    model = load_model(str(model_path), strict=True)
    audio = np.asarray(load_audio(str(args.audio), sample_rate=24000), dtype=np.float32).reshape(-1)
    plan = json.loads(args.plan.read_text())
    rows = sorted(plan["rows"], key=lambda r: r["source_start"])
    memory = ActorMemory(args.out / "actor-memory", model_id)
    first = {}
    evaluations = []
    times = []
    for row in rows:
        start = round(float(row["source_start"]) * 24000)
        end = round(float(row["source_end"]) * 24000)
        if start < 0 or end <= start or end > len(audio):
            raise ValueError("invalid annotated span")
        speaker = row["speaker"]
        # Include leading 1-second snippets to test the immediate recognition ambition.
        spans = {"full": audio[start:end], "first_1s": audio[start:min(end, start + 24000)]}
        vectors = {}
        for name, samples in spans.items():
            t0 = time.perf_counter()
            embedding = model.extract_speaker_embedding(mx.array(samples))
            mx.eval(embedding)
            vectors[name] = np.asarray(embedding).reshape(-1)
            times.append(time.perf_counter() - t0)
        if speaker not in first:
            # Test every new actor against the pre-existing database before enrolling.
            ranked = memory.rank(vectors["full"])
            if ranked:
                evaluations.append({"kind": "unknown_before_enrollment", "line": row["id"],
                                    "duration": "full", "best_score": ranked[0][1],
                                    "margin": ranked[0][1] - ranked[1][1] if len(ranked) > 1 else None})
            memory.enroll(speaker, row["id"], vectors["full"])
            first[speaker] = row["id"]
            continue
        for name, v in vectors.items():
            ranked = memory.rank(v)
            evaluations.append({"kind": "known", "line": row["id"], "duration": name,
                                "correct_top1": ranked[0][0] == speaker,
                                "true_score": next(score for actor, score in ranked if actor == speaker),
                                "best_score": ranked[0][1],
                                "margin": ranked[0][1] - ranked[1][1] if len(ranked) > 1 else None})
    memory.close()
    result = {"model": model_id, "scope": "single scene, manually labeled separate turns; no cross-episode evidence",
              "enrolled_actors": len(first), "enrollment_policy": "first labeled turn per actor, no updates",
              "embedding_seconds": times, "evaluations": evaluations}
    result_path = args.out / "results.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    os.chmod(result_path, 0o600)
    for duration in ("full", "first_1s"):
        known = [r for r in evaluations if r["kind"] == "known" and r["duration"] == duration]
        print(f"{duration}: top-1 correct {sum(r['correct_top1'] for r in known)}/{len(known)}")
    print(f"Unknown-before-enrollment tests: {sum(r['kind'] == 'unknown_before_enrollment' for r in evaluations)}")
    print(f"Private results: {args.out}")


if __name__ == "__main__":
    main()
