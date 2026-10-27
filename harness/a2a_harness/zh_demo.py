"""Mandarin demo clip and video for the live-translation plumbing test.

Speaks a short meeting script with a macOS zh_CN system voice (`say`), spaces the
sentences so English playback can keep up, and writes:

    clip.wav        48 kHz mono float32 source audio
    video.mp4       the same audio over an ffmpeg test pattern (for a real player app)
    expected.json   per-sentence source text, onset/offset, and English keywords

The voice is synthetic, so the clip may be used freely in tests. It proves plumbing and
timing only: system TTS is not a meeting corpus.

    uv run --project harness python -m a2a_harness.zh_demo --out .local/fixtures/zh-demo
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from . import CANONICAL_RATE
from .audio import read_wav, write_wav

# (Mandarin sentence, English keyword groups). Each group is a set of alternatives; a
# translation passes a sentence when every group has at least one match.
SCRIPT: list[tuple[str, list[list[str]]]] = [
    ("大家好，今天我们讨论下个季度的计划。", [["discuss", "talk"], ["quarter"], ["plan"]]),
    ("我们下周开会。", [["meeting", "meet"], ["next week"]]),
    ("这个方案我们不同意。", [["disagree", "not agree", "don't agree", "do not agree"], ["plan", "proposal", "scheme"]]),
    ("预算需要增加百分之二十。", [["budget"], ["20", "twenty"], ["percent", "%"]]),
    ("请在星期五之前把报告发给我。", [["report"], ["friday"]]),
    ("谢谢大家，会议到此结束。", [["thank"], ["meeting"]]),
]


def speak(text: str, voice: str, rate_wpm: int | None, workdir: Path) -> np.ndarray:
    out = workdir / "s.wav"
    cmd = ["say", "-v", voice, "--file-format=WAVE", f"--data-format=LEF32@{CANONICAL_RATE}", "-o", str(out)]
    if rate_wpm:
        cmd += ["-r", str(rate_wpm)]
    subprocess.run(cmd + [text], check=True)
    samples, rate = read_wav(out)
    if rate != CANONICAL_RATE:
        raise RuntimeError(f"say produced {rate} Hz, expected {CANONICAL_RATE}")
    return samples if samples.ndim == 1 else samples.mean(axis=1)


def build(out: Path, voice: str, gap_s: float, lead_s: float, rate_wpm: int | None, video: bool) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    pieces = [np.zeros(int(lead_s * CANONICAL_RATE), dtype=np.float32)]
    cursor = len(pieces[0])
    sentences = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, (text, keywords) in enumerate(SCRIPT):
            audio = speak(text, voice, rate_wpm, Path(tmp)).astype(np.float32)
            sentences.append({"index": i, "text": text, "keywords": keywords,
                              "onset_s": cursor / CANONICAL_RATE,
                              "offset_s": (cursor + len(audio)) / CANONICAL_RATE})
            gap = np.zeros(int(gap_s * CANONICAL_RATE), dtype=np.float32)
            pieces += [audio, gap]
            cursor += len(audio) + len(gap)
    clip = np.concatenate(pieces)
    write_wav(out / "clip.wav", clip, CANONICAL_RATE)
    meta = {"voice": voice, "rate": CANONICAL_RATE, "duration_s": len(clip) / CANONICAL_RATE,
            "gap_s": gap_s, "synthetic": True, "sentences": sentences}
    if video:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found; pass --no-video")
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                        "-f", "lavfi", "-i", f"testsrc2=size=960x540:rate=30:duration={meta['duration_s']:.3f}",
                        "-i", str(out / "clip.wav"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-b:a", "192k", "-shortest", str(out / "video.mp4")], check=True)
        meta["video"] = "video.mp4"
    (out / "expected.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return meta


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--voice", default="Tingting")
    ap.add_argument("--gap", type=float, default=3.0, help="silence after each sentence, seconds")
    ap.add_argument("--lead", type=float, default=1.5, help="silence before the first sentence, seconds")
    ap.add_argument("--rate-wpm", type=int, default=None)
    ap.add_argument("--no-video", action="store_true")
    args = ap.parse_args()
    meta = build(args.out, args.voice, args.gap, args.lead, args.rate_wpm, not args.no_video)
    print(json.dumps({"out": str(args.out), "duration_s": round(meta["duration_s"], 2),
                      "sentences": len(meta["sentences"]), "video": meta.get("video")}))


if __name__ == "__main__":
    main()
