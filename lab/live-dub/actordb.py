"""Per-series actor database: recognize an actor from a line's voice, and keep their best references.

Layout (`.local/live-dub/actors/<series>/`, 0700, files 0600):
  db.json         actors: id, name, exemplar embeddings (CAM++, 192-d), reference clip index
  refs/<id>-N.wav separated vocals (24 kHz) of the actor's cleanest lines, with transcripts in db.json

Recognition is open-set nearest-neighbour scoring: a line's embedding is compared with each actor's
exemplars (mean of the top-3 cosine similarities, which tolerates an actor's range from murmur to shout).
It is assigned when the best score clears `accept` and beats the runner-up by `margin`; a long line that
matches nobody starts a new actor; anything else stays unassigned and is voiced from its own audio only.
"""
from __future__ import annotations

import json
import os
import pathlib

import numpy as np

from common import ACTORS, save

MAX_EXEMPLARS = 60
MAX_REFS = 4


class ActorDB:
    def __init__(self, series: str, accept: float = 0.6, margin: float = 0.08, new_below: float = 0.45,
                 new_min_s: float = 1.5, learn_min: float = 0.65, readonly: bool = False):
        self.dir = ACTORS / series
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.accept, self.margin, self.new_below, self.new_min_s = accept, margin, new_below, new_min_s
        self.learn_min = learn_min  # only confident matches extend an actor, so clusters do not drift
        self.readonly = readonly    # a session that must not change the stored database keeps new clips in memory
        path = self.dir / "db.json"
        self.actors: list[dict] = json.loads(path.read_text())["actors"] if path.exists() else []
        for a in self.actors:
            a["_ex"] = np.array(a["exemplars"], dtype=np.float32)

    # -- recognition -------------------------------------------------------------------------
    def scores(self, emb: np.ndarray) -> list[tuple[float, dict]]:
        out = []
        for a in self.actors:
            sims = a["_ex"] @ emb
            out.append((float(np.sort(sims)[-3:].mean()), a))
        return sorted(out, key=lambda x: -x[0])

    def identify(self, emb: np.ndarray, seconds: float) -> tuple[dict | None, float, str]:
        """Returns (actor or None, best score, decision: known|new|unassigned).

        The margin is taken against the best *other person*: clusters that share a name are one actor
        enrolled in several registers (e.g. calm and shouting), so they must not veto each other."""
        ranked = self.scores(emb)
        best = ranked[0][0] if ranked else -1.0
        person = lambda a: a.get("name") or a["id"]
        second = next((sc for sc, a in ranked[1:] if person(a) != person(ranked[0][1])), -1.0)
        if ranked and best >= self.accept and best - second >= self.margin:
            return ranked[0][1], best, "known"
        if seconds >= self.new_min_s and best < self.new_below:
            return self.add_actor(emb), best, "new"
        return None, best, "unassigned"

    def add_actor(self, emb: np.ndarray) -> dict:
        a = {"id": f"A{len(self.actors) + 1:02d}", "name": None, "exemplars": [emb.tolist()], "_ex": emb[None].copy(),
             "refs": [], "seconds": 0.0, "episodes": []}
        self.actors.append(a)
        return a

    # -- learning ----------------------------------------------------------------------------
    def learn(self, actor: dict, emb: np.ndarray, seconds: float, episode: str, score: float = 1.0) -> None:
        actor["seconds"] = round(actor["seconds"] + seconds, 2)
        if episode not in actor["episodes"]:
            actor["episodes"].append(episode)
        if score < self.learn_min:
            return
        ex = np.concatenate([actor["_ex"], emb[None]])
        if len(ex) > MAX_EXEMPLARS:  # drop the exemplar most redundant with the rest
            sim = ex @ ex.T
            np.fill_diagonal(sim, 0)
            ex = np.delete(ex, int(sim.sum(1).argmax()), axis=0)
        actor["_ex"] = ex

    def offer_reference(self, actor: dict, pcm24: np.ndarray, text: str, score: float, episode: str, t: float) -> None:
        """Keep the actor's MAX_REFS best clips: 2.5-8 s, closest to the actor's own voice."""
        seconds = len(pcm24) / 24_000
        if not 2.5 <= seconds <= 8.0 or not text or score < self.learn_min:
            return
        quality = score * min(seconds, 6.0)
        refs = actor["refs"]
        if len(refs) >= MAX_REFS and quality <= min(r["quality"] for r in refs):
            return
        name = f"{actor['id']}-{episode}-{int(t * 10)}.wav"
        entry = {"file": name, "text": text, "seconds": round(seconds, 2), "quality": round(quality, 3),
                 "episode": episode, "t": round(t, 2)}
        if self.readonly:
            entry["_pcm"] = pcm24.copy()
        else:
            path = self.dir / "refs" / name
            save(path, pcm24, 24_000)
            os.chmod(path, 0o600)
        refs.append(entry)
        if len(refs) > MAX_REFS:
            worst = min(refs, key=lambda r: r["quality"])
            refs.remove(worst)
            if "_pcm" not in worst and not self.readonly:
                (self.dir / "refs" / worst["file"]).unlink(missing_ok=True)

    def references(self, actor: dict) -> list[tuple[np.ndarray, str]]:
        """The actor's clips, best first, as (24 kHz PCM, transcript)."""
        from common import load
        out = []
        for r in sorted(actor["refs"], key=lambda r: -r["quality"]):
            out.append((r["_pcm"] if "_pcm" in r else load(self.dir / "refs" / r["file"])[0], r["text"]))
        return out

    def save(self) -> None:
        if self.readonly:
            raise RuntimeError("read-only actor database")
        for a in self.actors:
            a["exemplars"] = a["_ex"].round(5).tolist()
        data = {"actors": [{k: v for k, v in a.items() if k != "_ex"} for a in self.actors]}
        for a in data["actors"]:
            a["refs"] = [{k: v for k, v in r.items() if k != "_pcm"} for r in a["refs"]]
        path = self.dir / "db.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        tmp.replace(path)


class SessionTracks:
    """Pools a voice's lines within a scene so a returning actor is recognized from a few lines.

    A line joins the active track it matches best when its similarity to the track's running mean is
    at least `join`; otherwise it opens a new track. All tracks close after a pause longer than
    `scene_gap` (a scene change). A track's pooled mean is matched against the database with the same
    accept/margin rule as single lines; once a track is recognized, its later lines inherit the actor.
    """

    def __init__(self, db: ActorDB, join: float = 0.6, scene_gap: float = 8.0):
        self.db, self.join, self.scene_gap = db, join, scene_gap
        self.tracks: list[dict] = []
        self.last_end = -1e9

    def assign(self, emb: np.ndarray, start: float, end: float) -> tuple[dict | None, float, str, int]:
        """Returns (actor or None, score, decision, track size after this line)."""
        if start - self.last_end > self.scene_gap:
            self.tracks = []
        self.last_end = max(self.last_end, end)
        best, track = -1.0, None
        for t in self.tracks:
            m = t["sum"] / np.linalg.norm(t["sum"])
            if float(m @ emb) > best:
                best, track = float(m @ emb), t
        if track is None or best < self.join:
            track = {"sum": np.zeros_like(emb), "n": 0, "actor": None, "score": -1.0}
            self.tracks.append(track)
        track["sum"] = track["sum"] + emb
        track["n"] += 1
        single, s_score, s_dec = self.db.identify(emb, 0.0)
        pooled = track["sum"] / np.linalg.norm(track["sum"])
        p_actor, p_score, p_dec = self.db.identify(pooled, 0.0)
        if p_actor is not None and (track["actor"] is None or p_score >= track["score"]):
            track["actor"], track["score"] = p_actor, p_score
        if track["actor"] is not None:
            return track["actor"], track["score"], "known", track["n"]
        return single, s_score, s_dec, track["n"]
