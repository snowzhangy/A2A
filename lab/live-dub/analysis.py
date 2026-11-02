"""Cross-checks for the D sweep that the simulator alone cannot give.

1. Pacing of forced-duration takes: English syllables per second and the share of internal silence.
   Ending on time is guaranteed by construction (OmniVoice is told the slot, then fitted), so the risk
   is unnatural pace: crammed lines or slowed lines padded with pauses. Outliers are listed for listening.
2. Coverage against the published subtitles: the share of subtitle time inside the pass's span that a
   played take overlaps. Unlike line coverage, this counts speech the pipeline never found (VAD misses,
   empty recognitions).
3. 8 vs 16 OmniVoice steps with translation cost held equal: re-simulates the 8-step pass with each
   line's MT cost taken from the 16-step pass (nearest line by start time), removing the MT confound.

usage: python analysis.py
"""
from __future__ import annotations

import copy
import json

import numpy as np

from common import RUNS, load, subtitles
from simulate import simulate
from translate import syllables

PASSES = {"ep08 480-780 (16 steps)": ("dub-ep08-480-780", "ep08", 480, 780),
          "ep09 580-880 (16 steps)": ("dub-ep09-580-880", "ep09", 580, 880),
          "ep09 580-880 (8 steps)": ("dub-ep09-580-880-s8", "ep09", 580, 880)}


def internal_silence(audio: np.ndarray, rate: int = 24_000) -> float:
    frame = rate // 50
    n = len(audio) // frame
    if n < 3:
        return 0.0
    rms = np.sqrt((audio[: n * frame].reshape(n, frame) ** 2).mean(1) + 1e-12)
    db = 20 * np.log10(rms / rms.max())
    voiced = np.where(db > -35)[0]
    inner = db[voiced[0]:voiced[-1] + 1] if len(voiced) else db
    return float((inner <= -35).mean())


def pacing(run: str) -> dict:
    rows = []
    for ln in json.load(open(RUNS / run / "lines.json")):
        if "take" not in ln or ln["take_s"] < 0.3:
            continue
        audio, _ = load(RUNS / run / ln["take"])
        rows.append({"start": ln["start"], "sps": syllables(ln["en"]) / ln["take_s"],
                     "silence": internal_silence(audio), "en": ln["en"], "slot": ln["end"] - ln["start"]})
    sps = np.array([r["sps"] for r in rows])
    sil = np.array([r["silence"] for r in rows])
    flagged = [r for r in rows if r["sps"] > 6.5 or r["sps"] < 2.0 or r["silence"] > 0.35]
    return {"takes": len(rows), "syll_per_s_p10_p50_p90": [round(float(np.percentile(sps, q)), 2) for q in (10, 50, 90)],
            "silence_p50_p90": [round(float(np.percentile(sil, q)), 3) for q in (50, 90)],
            "flagged_for_listening": len(flagged),
            "examples": [f"{r['start']:.1f}s {r['sps']:.1f} syl/s, {r['silence']:.0%} pause: {r['en']}" for r in flagged[:4]]}


def subtitle_coverage(run: str, episode: str, lo: float, hi: float, delay: float, separation: str) -> float:
    sched = json.load(open(RUNS / run / f"schedule-D{delay:g}-{separation}.json"))
    lines = {round(x["start"], 3): x for x in json.load(open(RUNS / run / "lines.json"))}
    played = [(x["start"] + x["late"], x["start"] + x["late"] + lines[round(x["start"], 3)]["take_s"]) for x in sched]
    subs = [s for s in subtitles(episode) if lo <= s["start"] and s["end"] <= hi]
    total = covered = 0.0
    for s in subs:
        total += s["end"] - s["start"]
        covered += sum(max(0.0, min(s["end"], b) - max(s["start"], a)) for a, b in played)
    return round(min(covered, total) / max(total, 1e-9), 3)


def main() -> None:
    out = {}
    for name, (run, ep, lo, hi) in PASSES.items():
        sep = "hybrid" if run.startswith("dub-ep08") else "isolation"
        out[name] = {"pacing": pacing(run),
                     f"subtitle_time_covered_D15_{sep}": subtitle_coverage(run, ep, lo, hi, 15, sep)}
        print(name, json.dumps(out[name], ensure_ascii=False))
    # 8 vs 16 steps with MT cost held equal.
    l16 = json.load(open(RUNS / "dub-ep09-580-880/lines.json"))
    l8 = copy.deepcopy(json.load(open(RUNS / "dub-ep09-580-880-s8/lines.json")))
    seg8 = json.load(open(RUNS / "dub-ep09-580-880-s8/segments.json"))
    starts16 = np.array([x["start"] for x in l16])
    for ln in l8:
        ln["compute"]["mt"] = l16[int(np.abs(starts16 - ln["start"]).argmin())]["compute"]["mt"]
    for d in (10, 15):
        r = simulate(l8, seg8, d, "isolation")
        r.pop("_played")
        print(f"8 steps, MT cost from the 16-step pass, D={d} isolation: coverage {r['coverage']}, "
              f"played {r['played']}/{r['lines']}")
        out[f"s8_mt_equal_D{d}"] = r["coverage"]
    (RUNS / "analysis.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
