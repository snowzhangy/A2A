"""Mux actual translated output with the aligned scene, retaining delayed final speech."""
import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--video", type=Path, required=True, help="aligned reference video (its audio is discarded)")
    p.add_argument("--run", type=Path, required=True)
    p.add_argument("--audio", choices=("output.wav", "rendered.wav"), default="output.wav")
    a = p.parse_args()
    a.run.resolve().relative_to((ROOT / ".local").resolve())
    # --translated-out is the compact speech stem: it omits empty-bus time.
    # --out records actual rendered output including the live timeline's silence.
    audio = a.run / a.audio
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(audio)], capture_output=True, text=True, check=True)
    duration = float(json.loads(result.stdout)["format"]["duration"])
    if not 0 < duration < 180:
        raise ValueError("use a short scene")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-n", "-i", str(a.video),
        "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-vf",
        f"tpad=stop_mode=clone:stop_duration={duration}", "-t", str(duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "21", "-c:a", "aac", "-movflags",
        "+faststart", str(a.run / "preview-timeline.mp4")], check=True)


if __name__ == "__main__":
    main()
