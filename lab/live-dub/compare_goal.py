"""Goal dub vs lab zero-delay dub on the goal scene (ep08 583-620), per line, on the final mixes.

Each line's English is cut from the mixed track where it plays (goal: the actor's slot, since the goal
fills it; lab: start + late for the take's played length). Same ASR and CAM++ for both.
"""
import json
import re
import subprocess
import sys

import numpy as np

sys.path.insert(0, "/Users/yangzhang/Documents/github/My/A2A/lab/live-dub")
from common import RUNS, load, resample  # noqa: E402
from speakers import Embedder  # noqa: E402
from translate import syllables  # noqa: E402

T0 = 583.0
GOAL_MP4 = "/Users/yangzhang/Documents/github/Research/dub-anything/videos/condor (dubbed).mp4"
GOAL_SCRIPT = "/Users/yangzhang/Documents/github/Research/dub-anything/work/condor-01/script.json"
LAB = RUNS / "catchup2-ep08-480-780"


def mix16(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000",
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32)


def words(s):
    return re.findall(r"[a-z']+", s.lower())


def main():
    from mlx_audio.stt import load as load_stt
    from rapidfuzz.distance import Levenshtein
    asr = load_stt("mlx-community/Qwen3-ASR-0.6B-8bit")
    emb = Embedder("campplus")
    v, r = load(RUNS / "sep-ep08-full/vocals.wav", T0, T0 + 37)
    vox = resample(v, r, 16_000)

    def row(mix, a, b, src_a, src_b, en):
        seg = mix[int(a * 16000):int(min(b, 37) * 16000)]
        heard = (asr.generate(seg, language="English").text or "").strip()
        src = vox[int(src_a * 16000):int(src_b * 16000)]
        ref = words(en)
        return {"wer": Levenshtein.distance(ref, words(heard)) / max(1, len(ref)),
                "likeness": float(emb(seg) @ emb(src)), "sps": syllables(en) / max(b - a, 0.3),
                "heard": heard, "en": en, "lag": None}

    goal = mix16(GOAL_MP4)
    lab = mix16(LAB / "goal-scene-nodelay-catchup.mp4")
    out = {"goal": [], "lab": []}
    for l in json.load(open(GOAL_SCRIPT))["lines"]:
        out["goal"].append({**row(goal, l["start"], l["end"], l["start"], l["end"], l["text"]), "lag": 0.0,
                            "zh": l["zh"]})
    lines = {round(x["start"], 3): x for x in json.load(open(LAB / "lines.json"))}
    for s in json.load(open(LAB / "schedule-catchup-isolation.json")):
        ln = lines[round(s["start"], 3)]
        a = ln["start"] - T0 + s["late"]
        if not (ln["start"] >= T0 - 1 and a < 37):
            continue
        b = a + ln["take_s"] / s["speed"]
        if b > 37:  # runs past the rendered 37 s window; it would play in a live stream, but cannot be scored here
            print("excluded (cut by the render window):", ln["en"])
            continue
        out["lab"].append({**row(lab, a, b, ln["start"] - T0, ln["end"] - T0, ln["en"]), "lag": s["late"],
                           "zh": ln["zh"]})
    for k, rows in out.items():
        w = np.array([x["wer"] for x in rows])
        s = np.array([x["likeness"] for x in rows])
        print(k, {"lines": len(rows), "wer_mean": round(float(w.mean()), 3),
                  "likeness_median": round(float(np.median(s)), 3),
                  "sps_median": round(float(np.median([x["sps"] for x in rows])), 2)})
        for x in rows:
            print(f"   lag {x['lag']:.1f}  wer {x['wer']:.2f}  sim {x['likeness']:.2f}  {x['en']}  || heard: {x['heard']}")
    json.dump(out, open(RUNS / "compare/goal-vs-lab-nodelay-metrics.json", "w"), indent=1, ensure_ascii=False)


if __name__ == "__main__":
    main()
