#!/usr/bin/env python3
"""Interactive local-media test through a real ffplay process, never a file replay.

Raw media, captions, and trace text must stay under .local/ or a restricted research
store. This is a development test using a system voice, not identity gate evidence.
Acquire the selected source explicitly before running this script.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import time


def prepare_reference(source: Path, output: Path, seconds: float, lead: float, tail: float) -> None:
    # all=1 is essential for stereo sources: delaying only channel zero before
    # downmixing overlays current speech with delayed speech from the other channel.
    subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(source.resolve()), "-vn",
        "-af", f"atrim=duration={seconds},asetpts=PTS-STARTPTS,adelay={lead * 1000}:all=1,apad=pad_dur={tail}",
        "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(output),
    ], check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--seconds", type=float, default=120)
    ap.add_argument("--source-offset", type=float, default=0, help="Original media time of excerpt sample zero")
    ap.add_argument("--hy-server", default="http://10.0.1.29:8082", help="Hy-SimulMT server URL")
    ap.add_argument("--captions-only", action="store_true")
    ap.add_argument("--player", choices=("fixture", "ffplay"), default="fixture")
    args = ap.parse_args()
    if not args.input.is_file() or not 0 < args.seconds <= 600:
        ap.error("input must exist and duration must be between 0 and 600 seconds")
    root = Path(__file__).resolve().parents[1]
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    pipeline = root / "apple/.build/debug/a2a-pipeline"
    if not pipeline.is_file():
        ap.error("build the pipeline through scripts/check.sh first")
    for executable in (("ffmpeg", "ffplay") if args.player == "ffplay" else ("ffmpeg",)):
        if not shutil.which(executable):
            ap.error(f"{executable} is required")
    lead, tail = 6, 24
    # Lead silence gives the tap and installed models time to arm. Tail silence keeps
    # the source process alive while in-flight translation and playout finish.
    reference = out / "reference.wav"
    prepare_reference(args.input, reference, args.seconds, lead, tail)
    command = [str(pipeline), "--asr", "audio", "--mt", "hy", "--hy-server", args.hy_server, "--tts", "system",
               "--seconds", str(lead + args.seconds), "--tail-seconds", "18", "--research-text",
               "--trace", str(out / "trace.jsonl"), "--source-out", str(out / "source.wav"),
               "--rendered-out", str(out / "rendered.wav")]
    if args.captions_only:
        command += ["--captions-only", "--events-stdout"]
    else:
        command += ["--translation-only", "--translated-out", str(out / "translated.wav")]
    started = time.time()
    player = None
    with (out / "player.log").open("w") as player_log, (out / "pipeline.log").open("w") as pipeline_log, \
            (out / "pipeline.stdout.jsonl").open("w") as stdout:
        try:
            player_command = [
                "ffplay", "-autoexit", "-loglevel", "error", "-window_title",
                f"A2A real source — original start {args.source_offset:.0f}s", "-showmode", "waves",
                str(reference),
            ] if args.player == "ffplay" else [
                str(root / ".local/apps/FixturePlayer.app/Contents/MacOS/FixturePlayer"),
                "--file", str(reference),
            ]
            player = subprocess.Popen(player_command, stdout=player_log, stderr=player_log)
            (out / "player.pid").write_text(str(player.pid), encoding="utf-8")
            command += ["--tap-pid", str(player.pid)]
            metadata = {"input": str(args.input.resolve()), "source_offset_s": args.source_offset,
                        "excerpt_seconds": args.seconds, "lead_seconds": lead, "tail_seconds": tail,
                        "captions_only": args.captions_only, "translation_only": not args.captions_only,
                        "pipeline_command": command, "player_command": player_command, "player_pid": player.pid}
            (out / "run.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            result = subprocess.run(command, stdout=stdout, stderr=pipeline_log, timeout=args.seconds + 80)
        finally:
            if player is not None and player.poll() is None:
                player.terminate()
                try:
                    player.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    player.kill()
                    player.wait(timeout=5)
    rows = (out / "pipeline.stdout.jsonl").read_text(encoding="utf-8").splitlines()
    if rows:
        summary = json.loads(rows[-1])
        (out / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps({"run": str(out), "pipeline_exit_code": result.returncode,
                      "elapsed_seconds": round(time.time() - started, 2)}))
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
