"""Render a dubbed span: the separated background bed plus each played take at its scheduled time.

Takes are placed at `start + late` on the programme timeline (the viewer's clock, already delayed by D),
level-matched to the actor's original line (dub-anything's --match-level), with 10 ms fades. The result
is muxed under the episode's own picture for listening.

usage: python render.py RUN_DIR --delay 10 --start 583 --end 620 [--separation roformer] [--out NAME]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess

import numpy as np

from common import INPUT, RUNS, load, resample, save

RATE = 48_000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--delay", type=float, default=10)
    ap.add_argument("--separation", default="roformer")
    ap.add_argument("--start", type=float, required=True)
    ap.add_argument("--end", type=float, required=True)
    ap.add_argument("--bed-gain", type=float, default=1.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--schedule", default=None, help="schedule file in RUN_DIR (default: the --delay one)")
    a = ap.parse_args()
    run = pathlib.Path(a.run)
    cfg = json.load(open(run / "config.json"))
    episode = cfg["episode"]
    lines = {round(x["start"], 3): x for x in json.load(open(run / "lines.json"))}
    sched = json.load(open(run / (a.schedule or f"schedule-D{a.delay:g}-{a.separation}.json")))
    bed, br = load(RUNS / f"sep-{episode}-full/background.wav", a.start, a.end)
    out = resample(bed, br, RATE) * a.bed_gain
    fade = int(0.01 * RATE)
    placed = 0
    for x in sched:
        ln = lines[round(x["start"], 3)]
        t = ln["start"] + x["late"]
        if not a.start <= t < a.end:
            continue
        take, tr = load(run / ln["take"])
        if x.get("speed", 1.0) > 1.01 or "switch_at" in x:  # catch-up: tempo change without pitch change
            from tts import wsola
            v = x.get("speed", 1.0)
            if "switch_at" in x:  # mid-line catch-up: the rest of the take plays at tail_speed
                cut = min(len(take), int(x["switch_at"] * v * tr))
                head = wsola(take[:cut], v, tr) if v > 1.01 else take[:cut]
                take = np.concatenate([head, wsola(take[cut:], x["tail_speed"], tr)]) if cut < len(take) else head
            else:
                take = wsola(take, v, tr)
        take = resample(take, tr, RATE)
        rms = float(np.sqrt((take ** 2).mean() + 1e-12))
        if ln.get("source_rms"):
            take = take * min(4.0, ln["source_rms"] / rms)
        if len(take) > 2 * fade:
            ramp = np.linspace(0, 1, fade, dtype=np.float32)
            take[:fade] *= ramp
            take[-fade:] *= ramp[::-1]
        i = int((t - a.start) * RATE)
        n = min(len(take), len(out) - i)
        out[i:i + n] += take[:n]
        placed += 1
    peak = float(np.abs(out).max())
    if peak > 0.99:
        out *= 0.99 / peak
    name = a.out or f"dub-{episode}-{int(a.start)}-{int(a.end)}-D{a.delay:g}"
    wav = run / f"{name}.wav"
    save(wav, out, RATE)
    mp4 = run / f"{name}.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(a.start), "-t", str(a.end - a.start),
                    "-i", str(INPUT / f"{episode}.webm"), "-i", str(wav), "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "aac", "-b:a", "160k",
                    "-shortest", str(mp4)], check=True)
    print({"takes": placed, "wav": str(wav), "mp4": str(mp4)})


if __name__ == "__main__":
    main()
