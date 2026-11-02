"""Causal dubbing pass over an episode: what a live engine would decide, with each stage's compute measured.

Walks the episode in time order and uses only audio up to the end of the current line, previous lines'
English, and the actor database as it stood (loaded from earlier episodes, updated as it goes). Each
stage's wall time is recorded so `simulate.py` can replay the run on a single-GPU queue for any
presentation delay D, counting late takes as misses. Stages per VAD segment of the separated vocals:

  ASR (Qwen3-ASR-1.7B on the mix, series glossary as hotwords)
  -> split at sentence ends with the forced aligner (Qwen3-ForcedAligner) when the segment holds several
  -> per sentence: CAM++ embedding -> actor DB (known / new / unassigned); merge same-actor sentences
  -> per line: MT (Qwen3-30B-A3B, context + glossary + slot budget)
  -> TTS (OmniVoice, reference = actor's stored clips + this line's own vocals, duration = slot)

usage: python pipeline.py --episode ep09 --series condor [--start 0 --end 2690] [--no-tts] [--fresh-db]
"""
from __future__ import annotations

import argparse
import re
import time

import numpy as np

from actordb import ActorDB
from asr import Recognizer, vad_segments
from common import RUNS, episode_audio, load, resample, save, write_json
from speakers import Embedder
from translate import Translator, load_series
from tts import Voice, fit, trim

SENTENCE_END = re.compile(r"(?<=[。？！?!])")
NEW_ACTOR_CONFIDENCE = 0.7  # a new actor's first line is its own exemplar: learn from it and keep it as a reference


class Clock:
    def __init__(self):
        self.t = {}

    def run(self, key: str, fn, *args, **kwargs):
        t = time.perf_counter()
        out = fn(*args, **kwargs)
        self.t[key] = round(self.t.get(key, 0.0) + time.perf_counter() - t, 3)
        return out


def apply_pronounce(text: str, table: dict) -> str:
    for k in sorted(table, key=len, reverse=True):
        text = re.sub(rf"\b{re.escape(k)}\b", table[k], text)
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", default="ep09")
    ap.add_argument("--series", default="condor")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=None)
    ap.add_argument("--no-tts", action="store_true", help="enrollment pass: ASR, speakers and DB only")
    ap.add_argument("--fresh-db", action="store_true", help="ignore the stored DB (baseline: no prior episodes)")
    ap.add_argument("--readonly-db", action="store_true", help="use the DB but do not save it")
    ap.add_argument("--steps", type=int, default=16)
    ap.add_argument("--min-silence", type=float, default=0.25, help="VAD pause that closes a unit (0.15 cuts at clauses)")
    ap.add_argument("--max-speech", type=float, default=10.0, help="longest unit before VAD forces a cut")
    ap.add_argument("--syl-rate", type=float, default=4.0, help="English syllables per second of the actor's slot")
    ap.add_argument("--strict", action="store_true", help="the syllable budget is a ceiling (catch-up mode)")
    ap.add_argument("--no-kv", action="store_true", help="translator without KV-cache reuse")
    ap.add_argument("--duration", default="slot", choices=["slot", "natural"],
                    help="slot: fill the actor's time (delayed picture); natural: the text's own length, capped "
                         "at the slot (catch-up without picture delay)")
    ap.add_argument("--mt", default="qwen3-30b-a3b")
    ap.add_argument("--accept", type=float, default=0.6)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    import mlx.core as mx
    from common import wait_for_memory
    wait_for_memory(8 if a.no_tts else 26)  # 30B MT + OmniVoice + ASR + aligner need about 24 GB
    # Several MLX models share one process; keep freed buffers from piling up between stages.
    mx.set_cache_limit(1 << 30)
    series = load_series(a.series)
    db = ActorDB(a.series if not a.fresh_db else f"{a.series}-scratch-{a.episode}", accept=a.accept,
                 readonly=a.readonly_db)
    if a.fresh_db:
        db.actors = []
    vocals44, vr = load(RUNS / f"sep-{a.episode}-full/vocals.wav", a.start, a.end)
    voc16, voc24 = resample(vocals44, vr, 16_000), resample(vocals44, vr, 24_000)
    mix16 = episode_audio(a.episode, 16_000, a.start, a.end)
    spans = vad_segments(voc16, threshold=0.5, min_silence=a.min_silence, max_speech=a.max_speech)

    hot = [k for k in series.get("glossary", {}) if len(k) >= 2]
    asr = Recognizer("qwen3-asr-1.7b", hot)
    from mlx_audio.stt import load as load_stt
    aligner = load_stt("mlx-community/Qwen3-ForcedAligner-0.6B-8bit")
    emb = Embedder("campplus")
    mt = tts = None
    if not a.no_tts:
        from tts import OmniVoiceTTS
        mt = Translator(a.mt, series, syllables_per_second=a.syl_rate, kv_reuse=not a.no_kv, strict=a.strict)
        mt("你好", 1.0)
        mt.history.clear()
        tts = OmniVoiceTTS(a.steps)
    asr(mix16[:16_000])

    out = a.out or RUNS / f"live-{a.episode}-{int(a.start)}-{int(a.end or 0)}{'-enroll' if a.no_tts else ''}{'-fresh' if a.fresh_db else ''}"
    segments, lines = [], []
    for si, (s, e) in enumerate(spans):
        if e - s < 0.4:
            continue
        clock = Clock()
        seg16 = mix16[int(s * 16_000):int(e * 16_000)]
        text = clock.run("asr", asr, seg16)
        if not re.sub(r"\W", "", text):
            segments.append({"seg": si, "start": s + a.start, "end": e + a.start, "text": "", "compute": clock.t})
            continue
        # Split at sentence ends; times from the forced aligner on the separated vocals.
        sentences = [x for x in SENTENCE_END.split(text) if re.sub(r"\W", "", x)]
        parts = [(s, e, text)]
        if len(sentences) > 1 and e - s > 2.0:
            res = clock.run("align", aligner.generate, voc16[int(s * 16_000):int(e * 16_000)],
                            text=re.sub(r"[^\w]", "", text), language="Chinese")
            items = list(getattr(res, "items", res))
            if items:
                parts, k = [], 0
                for sent in sentences:
                    n = len(re.sub(r"\W", "", sent))
                    chunk = items[k:k + n]
                    k += n
                    if chunk:
                        parts.append((s + chunk[0].start_time, s + chunk[-1].end_time, sent))
                # Close gaps: each part runs to the next part's start (within the segment).
                parts = [(p0, (parts[i + 1][0] if i + 1 < len(parts) else e), t) for i, (p0, p1, t) in enumerate(parts)]
        # Speaker per part, then merge consecutive parts of the same actor.
        merged = []
        for p0, p1, t in parts:
            v = voc16[int(p0 * 16_000):int(p1 * 16_000)]
            if len(v) < 0.3 * 16_000:
                v = voc16[int(max(0, p0 - 0.3) * 16_000):int((p1 + 0.3) * 16_000)]
            vec = clock.run("embed", emb, v)
            top = [(x["id"], round(sc, 3)) for sc, x in db.scores(vec)[:2]]
            actor, score, decision = db.identify(vec, p1 - p0)
            aid = actor["id"] if actor else None
            if merged and aid and merged[-1]["actor"] == aid and p1 - merged[-1]["start"] <= 7.0:
                merged[-1].update(end=p1, zh=merged[-1]["zh"] + t)
                merged[-1]["vecs"].append(vec)
            else:
                merged.append({"start": p0, "end": p1, "zh": t, "actor": aid, "score": round(score, 3),
                               "decision": decision, "top": top, "vecs": [vec], "_actor": actor})
        segments.append({"seg": si, "start": s + a.start, "end": e + a.start, "text": text, "parts": len(parts),
                         "compute": clock.t})
        for ln in merged:
            lc = Clock()
            slot = ln["end"] - ln["start"]
            actor = ln.pop("_actor")
            vec = np.mean(ln.pop("vecs"), axis=0)
            vec /= np.linalg.norm(vec) + 1e-9
            pcm = voc24[int(ln["start"] * 24_000):int(ln["end"] * 24_000)]
            row = {"seg": si, "start": round(ln["start"] + a.start, 3), "end": round(ln["end"] + a.start, 3),
                   "zh": ln["zh"], "actor": ln["actor"], "score": ln["score"], "decision": ln["decision"], "top": ln["top"],
                   "db_refs": len(actor["refs"]) if actor else 0}
            row["emb"] = [round(float(x), 5) for x in vec]
            confidence = NEW_ACTOR_CONFIDENCE if ln["decision"] == "new" else ln["score"]
            if actor is not None:
                db.learn(actor, vec, slot, a.episode, confidence)
            if mt is not None:
                name = (actor or {}).get("name") or (f"actor {actor['id']}" if actor else None)
                r = lc.run("mt", mt, ln["zh"], slot, name)
                en = r["en"]
                stored = []
                if actor is not None:
                    for (pcm_ref, txt), r in zip(db.references(actor), sorted(actor["refs"], key=lambda r: -r["quality"])):
                        if sum(len(x[1]) for x in stored) + len(pcm_ref) + len(pcm) > 10 * 24_000 or len(stored) == 2:
                            break
                        stored.append((r["file"], pcm_ref, txt))
                parts = stored + [(None, pcm, ln["zh"])]
                spoken = apply_pronounce(en, series.get("pronounce", {}))
                target = slot if a.duration == "slot" else min(slot, tts.natural_parts(parts, spoken))
                audio, _ = lc.run("tts", tts.render_parts, parts, spoken, max(0.6, target))
                ref_s = sum(len(x[1]) for x in parts) / 24_000
                take, speed = fit(audio, slot) if a.duration == "slot" else (trim(audio), 1.0)
                take_path = f"takes/{len(lines):04d}.wav"
                save(f"{out}/{take_path}", take, 24_000)
                lvl = float(np.sqrt((trim(pcm) ** 2).mean() + 1e-12)) if len(pcm) else 0.0
                row.update(en=en, take=take_path, take_s=round(len(take) / 24_000, 3), speed=round(speed, 2),
                           ref_s=round(ref_s, 2), source_rms=round(lvl, 5))
            if actor is not None:
                db.offer_reference(actor, pcm, ln["zh"], confidence, a.episode, ln["start"] + a.start)
            row["compute"] = lc.t
            lines.append(row)
        if si % 25 == 0:
            print(f"{s + a.start:7.1f}s  seg {si}/{len(spans)}  lines {len(lines)}  actors {len(db.actors)}", flush=True)
            write_json(f"{out}/lines.json", lines)
    write_json(f"{out}/lines.json", lines)
    write_json(f"{out}/segments.json", segments)
    write_json(f"{out}/config.json", vars(a) | {"spans": len(spans)})
    if not a.readonly_db:
        db.save()
    known = sum(r["decision"] == "known" for r in lines)
    print({"lines": len(lines), "actors": len(db.actors), "known": known,
           "new": sum(r["decision"] == "new" for r in lines),
           "unassigned": sum(r["decision"] == "unassigned" for r in lines)})


if __name__ == "__main__":
    main()
