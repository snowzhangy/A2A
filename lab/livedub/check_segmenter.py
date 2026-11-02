"""Check the segmenter's boundaries against the actor windows measured for the directed dub.

CPU only. The reference windows are the directed script's own (start, end) pairs, in clip
seconds; the clip is 583 s into the episode.

    python check_segmenter.py [--min-silence 0.25]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time

import numpy as np

from paths import ASR_RATE, load
from segmenter import Segmenter

EPISODE = pathlib.Path(__file__).resolve().parents[2] / ".local/live-dub/input/ep08-16k.wav"
DIRECTED = pathlib.Path.home() / ("Documents/github/Research/dub-anything/work/condor-01/"
                                 "script.json")
CLIP_START = 583.0
CLIP_END = 620.0


def windows() -> list[tuple[float, float, str]]:
    script = json.loads(DIRECTED.read_text())
    return [(float(line["start"]), float(line["end"]), line.get("voice", "?"))
            for line in script["lines"]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-silence", type=float, default=0.25)
    ap.add_argument("--preroll", type=float, default=0.20)
    ap.add_argument("--sweep", action="store_true", help="endpoint thresholds side by side")
    args = ap.parse_args()

    audio = load(EPISODE, CLIP_START, CLIP_END, ASR_RATE)
    seg = Segmenter(min_silence=args.min_silence, preroll=args.preroll)
    out, frame = [], int(0.02 * ASR_RATE)
    t0 = time.perf_counter()
    for i in range(0, len(audio), frame):
        out += seg.push(audio[i:i + frame])
    out += seg.finish()
    wall = time.perf_counter() - t0

    refs = windows()
    print(f"{len(audio)/ASR_RATE:.1f} s of audio, {len(out)} utterances, "
          f"VAD RTF {wall/(len(audio)/ASR_RATE):.4f}\n")
    print(f"{'utt':>3} {'start':>6} {'end':>6} {'dur':>5} {'wait':>5}  forced  nearest directed window")
    for u in out:
        near = min(refs, key=lambda r: abs(r[0] - u.start) + abs(r[1] - u.end)) if refs else None
        d = "" if near is None else f"{near[0]:.2f}-{near[1]:.2f} {near[2]}" + (
            f"  off {u.start-near[0]:+.2f}/{u.end-near[1]:+.2f}")
        print(f"{u.index:3d} {u.start:6.2f} {u.end:6.2f} {u.duration:5.2f} {u.wait:5.2f}"
              f"  {str(u.forced):5s}  {d}")
    print("\n" + coverage(out, refs))
    if args.sweep:
        print("\nsweep (the live trade-off: finer units speak sooner and fit better, "
              "but cut inside words)")
        print(f"{'min_silence':>11} {'units':>5} {'durs median/max':>16} {'whole lines':>11} "
              f"{'merges':>6} {'cuts inside a line':>19}")
        for ms in (0.15, 0.20, 0.25, 0.30, 0.40):
            s = Segmenter(min_silence=ms, preroll=args.preroll)
            got: list = []
            for i in range(0, len(audio), frame):
                got += s.push(audio[i:i + frame])
            got += s.finish()
            durs = sorted(u.duration for u in got)
            print(f"{ms:11.2f} {len(got):5d} {durs[len(durs)//2]:7.2f}/{durs[-1]:6.2f} "
                  f"{covered_by(got, refs):11d} {merges(got, refs):6d} {cuts(got, refs):19d}")
    return 0


def covered_by(out, refs) -> int:
    return sum(1 for r in refs if any(u.start - 0.05 <= r[0] and r[1] <= u.end + 0.1 for u in out))


def merges(out, refs) -> int:
    return sum(1 for u in out
               if sum(1 for r in refs if u.start - 0.05 <= r[0] and r[1] <= u.end + 0.1) > 1)


def cuts(out, refs) -> int:
    """Directed lines split across two utterances: the cost of a short endpoint."""
    n = 0
    for r in refs:
        parts = [u for u in out if u.start < r[1] - 0.1 and u.end > r[0] + 0.1]
        whole = [u for u in parts if u.start - 0.05 <= r[0] and r[1] <= u.end + 0.1]
        if parts and not whole:
            n += 1
    return n


def coverage(out, refs) -> str:
    return (f"directed lines covered whole: {covered_by(out, refs)}/{len(refs)}; "
            f"utterances holding >1 line: {merges(out, refs)}; "
            f"lines split across utterances: {cuts(out, refs)}")


if __name__ == "__main__":
    sys.exit(main())
