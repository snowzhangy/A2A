"""Replay the actor database over episodes already recognized by `pipeline.py`, without re-running ASR.

Recomputes each line's CAM++ embedding from the separated vocals and runs the same online policy as the
live pass (identify, learn, keep references), so thresholds can be tuned in seconds. With several runs,
the database carries over from one episode to the next, as it would across viewing sessions.

usage: python replay_db.py --series NAME RUN_DIR [RUN_DIR ...] [--accept 0.6 --margin 0.08 --learn-min 0.65]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil

import numpy as np

from actordb import ActorDB, SessionTracks
from common import ACTORS, RUNS, load, resample, write_json
from pipeline import NEW_ACTOR_CONFIDENCE
from speakers import Embedder


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--series", required=True)
    ap.add_argument("--accept", type=float, default=0.6)
    ap.add_argument("--margin", type=float, default=0.08)
    ap.add_argument("--new-below", type=float, default=0.45)
    ap.add_argument("--learn-min", type=float, default=0.65)
    ap.add_argument("--embedder", default="campplus")
    ap.add_argument("--from-db", default=None, help="start from this series' database instead of an empty one")
    ap.add_argument("--freeze", action="store_true", help="recognize only: no new actors, no learning")
    ap.add_argument("--tracks", action="store_true", help="pool each voice's lines within a scene (SessionTracks)")
    a = ap.parse_args()
    target = ACTORS / a.series
    shutil.rmtree(target, ignore_errors=True)
    if a.from_db:
        shutil.copytree(ACTORS / a.from_db, target)
    else:
        target.mkdir(parents=True)
        shutil.copy(ACTORS / "condor" / "series.json", target / "series.json")
    target.chmod(0o700)
    db = ActorDB(a.series, accept=a.accept, margin=a.margin, new_below=a.new_below, learn_min=a.learn_min)
    emb = Embedder(a.embedder)
    for run in map(pathlib.Path, a.runs):
        tracks = SessionTracks(db) if a.tracks else None
        episode = json.load(open(run / "config.json"))["episode"]
        v, r = load(RUNS / f"sep-{episode}-full/vocals.wav")
        v16, v24 = resample(v, r, 16_000), resample(v, r, 24_000)
        out = []
        for ln in json.load(open(run / "lines.json")):
            vec = emb(v16[int(ln["start"] * 16_000):int(ln["end"] * 16_000)])
            seconds = ln["end"] - ln["start"]
            top = [(x["id"], round(sc, 3)) for sc, x in db.scores(vec)[:2]]
            if tracks is not None:
                actor, score, decision, _ = tracks.assign(vec, ln["start"], ln["end"])
            else:
                actor, score, decision = db.identify(vec, 0.0 if a.freeze else seconds)
            conf = NEW_ACTOR_CONFIDENCE if decision == "new" else score
            if actor is not None and not a.freeze:
                db.learn(actor, vec, seconds, episode, conf)
                db.offer_reference(actor, v24[int(ln["start"] * 24_000):int(ln["end"] * 24_000)], ln["zh"], conf,
                                   episode, ln["start"])
            out.append({"start": ln["start"], "end": ln["end"], "zh": ln["zh"], "actor": actor["id"] if actor else None,
                        "score": round(score, 3), "decision": decision, "top": top,
                        "db_refs": len(actor["refs"]) if actor else 0})
        write_json(run / f"replay-{a.series}.json", out)
        d = {k: sum(o["decision"] == k for o in out) for k in ("known", "new", "unassigned")}
        print(episode, len(out), "lines", d, "actors", len(db.actors))
    db.save()
    for x in sorted(db.actors, key=lambda x: -x["seconds"])[:12]:
        print(x["id"], round(x["seconds"]), "s", len(x["_ex"]), "exemplars", len(x["refs"]), "refs", x["episodes"])


if __name__ == "__main__":
    main()
