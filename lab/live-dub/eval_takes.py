"""Objective checks on a pass's takes: intelligibility and likeness proxies (no listening verdict).

- WER: Qwen3-ASR 0.6B back-transcribes each take in English against the English it was asked to say.
- Likeness: CAM++ cosine similarity between the take and the actor's own line (separated vocals).
  OmniVoice never sees CAM++, so this is not the circular check the repo warns about; it is still a
  cross-lingual proxy, and accent and delivery affect it.

usage: python eval_takes.py RUN_DIR [RUN_DIR ...]
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

import numpy as np

from common import RUNS, load, resample, write_json
from speakers import Embedder


def words(s: str) -> list[str]:
    return re.findall(r"[a-z']+", s.lower())


def main() -> None:
    from mlx_audio.stt import load as load_stt
    from rapidfuzz.distance import Levenshtein
    asr = load_stt("mlx-community/Qwen3-ASR-0.6B-8bit")
    emb = Embedder("campplus")
    for run in map(pathlib.Path, sys.argv[1:]):
        episode = json.load(open(run / "config.json"))["episode"]
        v, r = load(RUNS / f"sep-{episode}-full/vocals.wav")
        v16 = resample(v, r, 16_000)
        rows = []
        for ln in json.load(open(run / "lines.json")):
            if "take" not in ln:
                continue
            take, tr = load(run / ln["take"])
            t16 = resample(take, tr, 16_000)
            heard = (asr.generate(t16, language="English").text or "").strip()
            ref, hyp = words(ln["en"]), words(heard)
            src = v16[int(ln["start"] * 16_000):int(ln["end"] * 16_000)]
            sim = float(emb(t16) @ emb(src)) if len(src) else float("nan")
            rows.append({"start": ln["start"], "wer": Levenshtein.distance(ref, hyp) / max(1, len(ref)),
                         "likeness": round(sim, 3), "slot_s": round(ln["end"] - ln["start"], 2), "heard": heard})
        w = np.array([x["wer"] for x in rows])
        s = np.array([x["likeness"] for x in rows])
        summary = {"run": run.name, "takes": len(rows), "wer_mean": round(float(w.mean()), 3),
                   "wer_over_0.3": int((w > 0.3).sum()), "likeness_median": round(float(np.median(s)), 3)}
        write_json(run / "takes-eval.json", {"summary": summary, "rows": rows})
        print(summary)


if __name__ == "__main__":
    main()
