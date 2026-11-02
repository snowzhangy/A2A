"""Contact sheets for checking who speaks: one frame per line at its midpoint, captioned, per actor.

Used to label actors by looking at the picture (the lab has no ground-truth speaker labels). A frame shows
who is on screen, which is usually but not always the speaker, so sheets are evidence, not truth.

usage: python frames.py RUN_DIR --actors A01,A02 [--per 8]   -> RUN_DIR/sheets/<actor>.jpg
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess

from PIL import Image, ImageDraw, ImageFont

from common import INPUT

FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"


def frame(episode: str, t: float, width: int = 320) -> Image.Image:
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(INPUT / f"{episode}.webm"),
                          "-frames:v", "1", "-vf", f"scale={width}:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                         check=True, capture_output=True).stdout
    from io import BytesIO
    return Image.open(BytesIO(raw)).convert("RGB")


def sheet(episode: str, rows: list[dict], title: str, cols: int = 4) -> Image.Image:
    tiles = []
    font = ImageFont.truetype(FONT, 14)
    for r in rows:
        im = frame(episode, (r["start"] + r["end"]) / 2)
        canvas = Image.new("RGB", (im.width, im.height + 40), "black")
        canvas.paste(im, (0, 0))
        d = ImageDraw.Draw(canvas)
        d.text((4, im.height + 2), f"{r['start']:.1f}s {r.get('score', '')}", fill="yellow", font=font)
        d.text((4, im.height + 20), r["zh"][:20], fill="white", font=font)
        tiles.append(canvas)
    w, h = tiles[0].size
    n_rows = (len(tiles) + cols - 1) // cols
    out = Image.new("RGB", (w * cols, h * n_rows + 30), "gray")
    ImageDraw.Draw(out).text((6, 4), title, fill="white", font=ImageFont.truetype(FONT, 20))
    for i, t in enumerate(tiles):
        out.paste(t, ((i % cols) * w, 30 + (i // cols) * h))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--actors", required=True)
    ap.add_argument("--per", type=int, default=8)
    ap.add_argument("--lines", default="lines.json", help="lines file in RUN_DIR (e.g. replay-<series>.json)")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    run = pathlib.Path(a.run)
    episode = json.load(open(run / "config.json"))["episode"]
    lines = json.load(open(run / a.lines))
    for aid in a.actors.split(","):
        rows = [l for l in lines if l["actor"] == aid and l["end"] - l["start"] >= 1.2]
        if not rows:
            continue
        step = max(1, len(rows) // a.per)
        pick = rows[::step][: a.per]
        total = sum(l["end"] - l["start"] for l in lines if l["actor"] == aid)
        path = run / "sheets" / f"{a.tag}{aid}.jpg"
        path.parent.mkdir(exist_ok=True)
        sheet(episode, pick, f"{episode} {aid}: {len([l for l in lines if l['actor'] == aid])} lines, {total:.0f} s").save(path, quality=80)
        print(path)


if __name__ == "__main__":
    main()
