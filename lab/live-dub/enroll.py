"""Offline enrollment: build (or extend) a series' actor database from episodes already watched.

Live recognition needs a clean database more than a clever online rule: online clustering from scratch
either merges similar voices or fragments one voice into many. Between episodes there is time to look at
every line at once, so enrollment clusters all lines of the watched episodes globally (average linkage
on CAM++ cosine similarity), keeps clusters with enough speech as actors, and stores for each actor its
member embeddings as exemplars plus its most typical 3-8 s lines as voice references.

Names are optional and come from the user (`--names A01=Yang Guo,...`); IDs are stable across re-runs
only within one database version.

usage: python enroll.py --series condor RUN_DIR [RUN_DIR ...] [--threshold 0.55 --min-seconds 20]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil

import numpy as np

from actordb import MAX_EXEMPLARS, MAX_REFS, ActorDB
from common import ACTORS, RUNS, load, resample, write_json
from speakers import Embedder, cluster


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--series", required=True)
    ap.add_argument("--threshold", type=float, default=0.55)
    ap.add_argument("--min-seconds", type=float, default=20.0)
    ap.add_argument("--min-line", type=float, default=1.0)
    ap.add_argument("--names", default="")
    a = ap.parse_args()
    emb = Embedder("campplus")
    rows, audio24 = [], {}
    for run in map(pathlib.Path, a.runs):
        episode = json.load(open(run / "config.json"))["episode"]
        v, r = load(RUNS / f"sep-{episode}-full/vocals.wav")
        v16 = resample(v, r, 16_000)
        audio24[episode] = resample(v, r, 24_000)
        for ln in json.load(open(run / "lines.json")):
            if ln["end"] - ln["start"] >= a.min_line:
                rows.append({"episode": episode, "start": ln["start"], "end": ln["end"], "zh": ln["zh"],
                             "emb": emb(v16[int(ln["start"] * 16_000):int(ln["end"] * 16_000)])})
    E = np.array([x["emb"] for x in rows])
    labels = cluster(E, a.threshold)
    groups: dict[int, list[int]] = {}
    for i, l in enumerate(labels):
        groups.setdefault(int(l), []).append(i)
    secs = {k: sum(rows[i]["end"] - rows[i]["start"] for i in v) for k, v in groups.items()}
    keep = sorted((k for k in groups if secs[k] >= a.min_seconds), key=lambda k: -secs[k])

    target = ACTORS / a.series
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    target.chmod(0o700)
    shutil.copy(ACTORS / "condor" / "series.json", target / "series.json")
    db = ActorDB(a.series)
    names = dict(x.split("=", 1) for x in a.names.split(",") if "=" in x)
    report = []
    for k in keep:
        members = groups[k]
        c = E[members].mean(0)
        c /= np.linalg.norm(c)
        typical = sorted(members, key=lambda i: -float(E[i] @ c))
        actor = db.add_actor(E[typical[0]])
        actor["_ex"] = E[typical[:MAX_EXEMPLARS]].copy()
        actor["seconds"] = round(secs[k], 1)
        actor["episodes"] = sorted({rows[i]["episode"] for i in members})
        actor["name"] = names.get(actor["id"])
        for i in [i for i in typical if 3.0 <= rows[i]["end"] - rows[i]["start"] <= 8.0][:MAX_REFS]:
            x = rows[i]
            pcm = audio24[x["episode"]][int(x["start"] * 24_000):int(x["end"] * 24_000)]
            db.offer_reference(actor, pcm, x["zh"], 1.0, x["episode"], x["start"])
        report.append({"id": actor["id"], "seconds": actor["seconds"], "lines": len(members),
                       "cohesion": round(float(np.mean(E[members] @ c)), 3), "refs": len(actor["refs"]),
                       "members": [{"episode": rows[i]["episode"], "start": rows[i]["start"], "end": rows[i]["end"],
                                    "zh": rows[i]["zh"], "score": round(float(E[i] @ c), 3)} for i in typical]})
    db.save()
    write_json(target / "enrollment.json", report)
    covered = sum(secs[k] for k in keep) / max(1e-9, sum(secs.values()))
    print(f"{len(rows)} lines, {len(groups)} clusters, {len(keep)} actors kept "
          f"({covered:.0%} of line seconds)")
    for x in report[:15]:
        print(x["id"], x["seconds"], "s", x["lines"], "lines, cohesion", x["cohesion"], "refs", x["refs"])


if __name__ == "__main__":
    main()
