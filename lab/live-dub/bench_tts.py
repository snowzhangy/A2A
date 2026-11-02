"""TTS bench on the goal scene: 12 directed lines, our translations, per-line references, slot-length fill.

For each line: reference = the actor's other clips in the scene followed by this line's separated vocals
(dub-anything's per-line rule), text = the 30B translation, target = the actor's slot. Reports compute
time, take length against the slot after fitting, and an English back-transcription word error rate
(Qwen3-ASR) as an intelligibility flag. Writes each take and a scene mix for listening.

usage: python bench_tts.py --engine omnivoice|qwen [--steps 32]
"""
from __future__ import annotations

import argparse
import json
import re

import numpy as np

from common import RUNS, load, resample, save, write_json
from translate import goal_script
from tts import RATE, OmniVoiceTTS, QwenTTS, Voice, fit, trim

SCENE = 583.0


def words(s: str) -> list[str]:
    return re.findall(r"[a-z']+", s.lower())


def wer(ref: str, hyp: str) -> float:
    from rapidfuzz.distance import Levenshtein
    r, h = words(ref), words(hyp)
    return Levenshtein.distance(r, h) / max(1, len(r))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", default="omnivoice", choices=["omnivoice", "qwen"])
    ap.add_argument("--steps", type=int, default=32)
    ap.add_argument("--mt", default="mt-goal-qwen3-30b-a3b-nolook.json")
    a = ap.parse_args()
    lines = goal_script()
    english = [x["en"] for x in json.load(open(RUNS / a.mt))]
    voc, rate = load(RUNS / "sep-ep08-full/vocals.wav", SCENE - 1, SCENE + 40)
    voc = resample(voc, rate, RATE)

    def clip(s, e):
        return voc[int((s + 1) * RATE):int((e + 1) * RATE)]

    engine = OmniVoiceTTS(a.steps) if a.engine == "omnivoice" else QwenTTS()
    from mlx_audio.stt import load as load_stt
    asr = load_stt("mlx-community/Qwen3-ASR-0.6B-8bit")
    engine(Voice(clip(0.72, 4.45), lines[0]["zh"]), "Warm up.", 1.0)
    out_dir = RUNS / (f"tts-goal-{a.engine}" + (f"-s{a.steps}" if a.engine == "omnivoice" else ""))
    rows, mix = [], np.zeros(int(40 * RATE), np.float32)
    for i, (l, en) in enumerate(zip(lines, english)):
        slot = l["end"] - l["start"]
        others = [Voice(clip(m["start"], m["end"]), m["zh"]) for j, m in enumerate(lines)
                  if m["voice"] == l["voice"] and j != i][:3]
        ref = Voice.concat(others + [Voice(clip(l["start"], l["end"]), l["zh"])])
        audio, compute = engine(ref, en, slot)
        take, speed = fit(audio, slot)
        heard = (asr.generate(resample(take, RATE, 16000), language="English").text or "").strip()
        rows.append({"line": i, "voice": l["voice"], "slot_s": round(slot, 2), "raw_s": round(len(trim(audio)) / RATE, 2),
                     "take_s": round(len(take) / RATE, 2), "speed": round(speed, 2), "compute_s": round(compute, 2),
                     "end_error_s": round(len(take) / RATE - slot, 2), "wer": round(wer(en, heard), 2),
                     "text": en, "heard": heard})
        save(out_dir / f"line{i:02d}.wav", take, RATE)
        s = int(l["start"] * RATE)
        mix[s:s + len(take)] += take[:len(mix) - s]
        print(rows[-1])
    save(out_dir / "voices.wav", mix, RATE)
    comp = sum(r["compute_s"] for r in rows)
    summary = {"engine": a.engine, "steps": a.steps, "lines": len(rows),
               "compute_over_slot": round(comp / sum(r["slot_s"] for r in rows), 2),
               "within_0.25s": sum(abs(r["end_error_s"]) <= 0.25 for r in rows),
               "median_abs_end_error_s": float(np.median([abs(r["end_error_s"]) for r in rows])),
               "wer_mean": round(float(np.mean([r["wer"] for r in rows])), 3)}
    write_json(out_dir / "rows.json", rows)
    write_json(out_dir / "summary.json", summary)
    print(summary)


if __name__ == "__main__":
    main()
