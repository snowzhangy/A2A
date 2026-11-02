"""Session voice bank: learn each voice once from its first 3 s, then recognize it for the rest of the session.

Replays the app's own speech segments (`a2a-stage-probe speakers` output: VAD spans of at most 3 s) in
time order and compares two ways of deciding which cloned voice a segment gets:

- `tracker`: the app's DSP tracker label as it is (mel cepstrum, at most 4 speakers).
- `bank`: a CAM++ embedding per segment, matched against the voices learned so far in this session.
  A segment that matches no voice joins a pending voice; a pending voice becomes usable once it holds
  `--learn-seconds` of speech (the app's 3 s reference). The bank itself is not saved; the per-segment
  embeddings are cached beside OUT_JSON (voiceprints, 0600) so threshold sweeps skip re-embedding.

Reference labels are the offline CAM++ clusters of the same episode (`enroll.py`), a proxy for the cast,
not picture-checked truth. Scores:

- own voice (`learned`): share of reference-actor speech voiced with a voice learned from that actor's
  own speech (the first 3 s of a tracker label, or the segment that completed a bank voice). This is
  what a listener hears. `majority` instead credits the voice's majority actor, which is kinder;
- relearns: voices created beyond one per actor (each costs another 3 s wait and changes the voice);
- waits: speech that arrived while its voice was still pending.

usage: python session_bank.py TRACKER_JSON ENROLLMENT_JSON AUDIO_WAV OUT_JSON [--accept 0.6 ...]
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib

import numpy as np

from common import load, resample, write_json
from speakers import RATE, Embedder


def reference(enrollment: list[dict], names: dict[str, str] | None = None):
    """Clusters that share a name are one person (e.g. ep08's Yang Guo in two registers)."""
    names = names or {}
    spans = sorted((m["start"], m["end"], names.get(c["id"]) or c["id"]) for c in enrollment for m in c["members"])

    def actor(s: float, e: float) -> str | None:
        best, ov = None, 0.0
        for a, b, k in spans:
            if a >= e:
                break
            o = min(e, b) - max(s, a)
            if o > ov:
                best, ov = k, o
        return best if ov >= 0.5 * (e - s) else None
    return actor


class Bank:
    def __init__(self, accept: float, margin: float, join: float, learn_seconds: float, min_seconds: float,
                 short_accept: float, max_voices: int, new_below: float, turn_gap: float, turn_keep: float):
        self.accept, self.margin, self.join = accept, margin, join
        self.learn_seconds, self.min_seconds, self.short_accept = learn_seconds, min_seconds, short_accept
        self.max_voices, self.new_below = max_voices, new_below
        self.turn_gap, self.turn_keep = turn_gap, turn_keep
        self.voices: list[dict] = []   # {"id", "sum", "ex", "seconds", "ready_at"}
        self.pending: list[dict] = []  # {"sum", "ex", "seconds", "last"}
        self.prev: tuple[str | None, float] = (None, -1e9)  # last voice and where its segment ended

    @staticmethod
    def _sim(v: dict, emb: np.ndarray) -> float:
        """Mean of the top-3 exemplar similarities, as ActorDB.scores does."""
        sims = np.stack(v["ex"]) @ emb
        return float(np.sort(sims)[-3:].mean())

    def assign(self, emb: np.ndarray, start: float, end: float) -> tuple[str | None, str]:
        vid, decision = self._assign(emb, start, end)
        if vid:
            self.prev = (vid, end)
        return vid, decision

    def _assign(self, emb: np.ndarray, start: float, end: float) -> tuple[str | None, str]:
        """Returns (voice id or None, decision: known|turn|pending|new|unassigned)."""
        seconds = end - start
        ranked = sorted(((self._sim(v, emb), v) for v in self.voices), key=lambda x: -x[0])
        best = ranked[0][0] if ranked else -1.0
        second = ranked[1][0] if len(ranked) > 1 else -1.0
        need = self.accept if seconds >= self.min_seconds else self.short_accept
        if ranked and best >= need and best - second >= self.margin:
            v = ranked[0][1]
            if seconds >= self.min_seconds and best >= self.accept + 0.05 and len(v["ex"]) < 60:
                v["ex"].append(emb)  # only confident, long matches extend the voice
            return v["id"], "known"
        # A forced cut inside one speaking turn keeps the turn's voice unless the new piece is clearly someone else.
        prev = next((v for v in self.voices if v["id"] == self.prev[0]), None)
        if prev is not None and start - self.prev[1] <= self.turn_gap and self._sim(prev, emb) >= self.turn_keep:
            return prev["id"], "turn"
        # Unsure (close to a known voice but not accepted): never start a new voice from it.
        if seconds < self.min_seconds or len(self.voices) >= self.max_voices or best >= self.new_below:
            return None, "unassigned"
        self.pending = [p for p in self.pending if start - p["last"] <= 30.0]
        centroid = lambda p: float(p["sum"] @ emb / (np.linalg.norm(p["sum"]) + 1e-9))
        cand = max(self.pending, key=centroid, default=None)
        if cand is None or centroid(cand) < self.join:
            cand = {"sum": np.zeros_like(emb), "ex": [], "seconds": 0.0, "last": end}
            self.pending.append(cand)
        cand["sum"] = cand["sum"] + emb
        cand["ex"].append(emb)
        cand["seconds"] += seconds
        cand["last"] = end
        if cand["seconds"] >= self.learn_seconds:
            self.pending = [p for p in self.pending if p is not cand]
            v = {"id": f"V{len(self.voices) + 1}", "sum": cand["sum"], "ex": cand["ex"], "seconds": cand["seconds"],
                 "ready_at": end}
            self.voices.append(v)
            return v["id"], "new"
        return None, "pending"


def learned_from(rows: list[dict], key: str, seconds: float = 3.0) -> dict:
    """Whose speech each voice was learned from (None: not a reference actor, e.g. a theme song)."""
    owner, heard = {}, collections.defaultdict(collections.Counter)
    for r in rows:
        v = r[key]
        if not v or v in owner:
            continue
        if key == "bank":
            if r["decision"] == "new":
                owner[v] = r["actor"]
            continue
        heard[v][r["actor"]] += r["end"] - r["start"]
        if sum(heard[v].values()) >= seconds:
            owner[v] = heard[v].most_common(1)[0][0]
    return owner


def score(rows: list[dict], key: str) -> dict:
    lab = [r for r in rows if r["actor"]]
    total = sum(r["end"] - r["start"] for r in lab)
    by_v = collections.defaultdict(collections.Counter)
    for r in lab:
        if r[key]:
            by_v[r[key]][r["actor"]] += r["end"] - r["start"]
    owner = {v: c.most_common(1)[0][0] for v, c in by_v.items()}
    learned = learned_from(rows, key)
    own = sum(r["end"] - r["start"] for r in lab if r[key] and owner[r[key]] == r["actor"])
    own_learned = sum(r["end"] - r["start"] for r in lab if r[key] and learned.get(r[key]) == r["actor"])
    voiced = sum(r["end"] - r["start"] for r in lab if r[key])
    per_actor = collections.Counter(owner.values())
    actors = {r["actor"] for r in lab}
    return {
        "voices": len(by_v),
        "reference_actors": len(actors),
        "actors_with_own_voice": len(per_actor),
        "relearns": sum(n - 1 for n in per_actor.values()),
        "own_voice": round(own_learned / total, 3),
        "wrong_voice": round((voiced - own_learned) / total, 3),
        "own_voice_majority": round(own / total, 3),
        "no_voice": round((total - voiced) / total, 3),
        "voice_purity": {v: round(c.most_common(1)[0][1] / sum(c.values()), 2) for v, c in sorted(by_v.items())},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tracker")
    ap.add_argument("enrollment")
    ap.add_argument("audio")
    ap.add_argument("out")
    ap.add_argument("--accept", type=float, default=0.6)
    ap.add_argument("--margin", type=float, default=0.05)
    ap.add_argument("--join", type=float, default=0.6)
    ap.add_argument("--learn-seconds", type=float, default=3.0)
    ap.add_argument("--min-seconds", type=float, default=1.0, help="shorter segments only join a known voice")
    ap.add_argument("--short-accept", type=float, default=0.65)
    ap.add_argument("--max-voices", type=int, default=64)
    ap.add_argument("--embedder", default="campplus", help="campplus or eres2netv2 (the reference clusters are CAM++)")
    ap.add_argument("--new-below", type=float, default=0.45, help="a segment this close to a known voice never starts one")
    ap.add_argument("--turn-gap", type=float, default=0.3, help="seconds between pieces of one speaking turn")
    ap.add_argument("--turn-keep", type=float, default=0.4, help="similarity that keeps a turn's voice across a cut")
    a = ap.parse_args()
    out = pathlib.Path(a.out)
    if out.exists():
        raise FileExistsError("choose a new results file")
    segs = json.load(open(a.tracker))["segments"]
    db = pathlib.Path(a.enrollment).with_name("db.json")
    names = {x["id"]: x.get("name") for x in json.load(open(db))["actors"]} if db.exists() else {}
    actor = reference(json.load(open(a.enrollment)), names)
    cache = out.parent / f"emb-{pathlib.Path(a.tracker).parent.name}-{pathlib.Path(a.audio).stem}-{a.embedder}.npy"
    if cache.exists():
        vecs = np.load(cache)
    else:
        audio, rate = load(pathlib.Path(a.audio))
        audio = resample(audio, rate, RATE)
        embed = Embedder(a.embedder)
        vecs = np.stack([embed(audio[int(s["start_s"] * RATE):int(s["end_s"] * RATE)]) for s in segs])
        np.save(cache, vecs)
        cache.chmod(0o600)  # voiceprints
    bank = Bank(a.accept, a.margin, a.join, a.learn_seconds, a.min_seconds, a.short_accept, a.max_voices,
                a.new_below, a.turn_gap, a.turn_keep)
    rows, waits = [], collections.Counter()
    for s, vec in zip(segs, vecs):
        st, en = s["start_s"], s["end_s"]
        vid, decision = bank.assign(vec, st, en)
        ref = actor(st, en)
        rows.append({"start": st, "end": en, "tracker": s["speaker"], "bank": vid, "decision": decision, "actor": ref})
        if ref and decision == "pending":
            waits[ref] += en - st
    result = {
        "config": vars(a),
        "segments": len(rows),
        "reference_seconds": round(sum(r["end"] - r["start"] for r in rows if r["actor"]), 1),
        "tracker": score(rows, "tracker"),
        "bank": score(rows, "bank"),
        "bank_decisions": dict(collections.Counter(r["decision"] for r in rows)),
        "bank_wait_seconds_on_reference_actors": round(sum(waits.values()), 1),
        "bank_voices_ready_at_s": {v["id"]: round(v["ready_at"], 1) for v in bank.voices},
    }
    write_json(out, {**result, "rows": rows})
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
