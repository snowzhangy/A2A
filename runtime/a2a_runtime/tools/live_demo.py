"""Live demo and check on Windows: the counterpart of scripts/live_demo.sh (ADR-0007).

mpv plays a zh_demo clip; the portable pipeline taps mpv's process tree, ducks it, and plays
the translation; then the played audio is back-transcribed and checked with
`a2a_harness.live_check` (every sentence's keywords, errors, drops, alignment, WER).

    uv run --project runtime python -m a2a_runtime.tools.live_demo --clip-dir .local/fixtures/zh-demo \\
        --out .local/runs/live-demo [--output-device SPDIF] [--player PATH] [--tts clone|system] [--global]

The player starts idle so the tap exists before its first sample; the clip is loaded over
mpv's IPC pipe once the pipeline reports it is live. `--output-device` sends both the player
and the translation to a device nobody listens to, for an unattended run.

`--global` taps the whole output mix (as the Mac app does) instead of mpv alone. It then also
ducks and captures every other application playing on this PC for the length of the run, so
use it only when nothing else should be heard or recorded.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .. import REPO_ROOT

MPV_DEFAULT = r"C:\Tools\mpv-x86_64-v3-20260607-git-71ebd08\mpv.exe"


def mpv_device(output_device: str | None) -> str | None:
    if not output_device:
        return None
    from ..audio import windows as win

    endpoint = win.resolve_endpoint(output_device)
    return "wasapi/" + endpoint.id.split("}.")[-1] if endpoint else None


def ipc(pipe: str, command: list) -> None:
    deadline = time.monotonic() + 10
    while True:
        try:
            with open(pipe, "w", encoding="utf-8") as fh:
                fh.write(json.dumps({"command": command}) + "\n")
            return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--clip-dir", type=Path, default=REPO_ROOT / ".local" / "fixtures" / "zh-demo")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--output-device", default=None)
    ap.add_argument("--player", default=shutil.which("mpv") or MPV_DEFAULT)
    ap.add_argument("--tts", default="clone", choices=("clone", "system", "tone"))
    ap.add_argument("--speakers", default="auto", choices=("single", "auto"))
    ap.add_argument("--tail", type=float, default=15.0)
    ap.add_argument("--global", dest="global_mix", action="store_true",
                    help="tap the whole output mix instead of the player (ducks every other app)")
    ap.add_argument("--extra", nargs=argparse.REMAINDER, default=[], help="more pipeline arguments")
    args = ap.parse_args()
    out = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; use a new run directory")
    out.mkdir(parents=True, exist_ok=True)
    expected = json.loads((args.clip_dir / "expected.json").read_text(encoding="utf-8"))
    clip = (args.clip_dir / "clip.wav").resolve()
    pipe = rf"\\.\pipe\a2a-mpv-{uuid.uuid4().hex[:8]}"
    player = [args.player, "--no-video", "--idle=yes", "--really-quiet", f"--input-ipc-server={pipe}"]
    device = mpv_device(args.output_device)
    if device:
        player.append(f"--audio-device={device}")
    mpv = subprocess.Popen(player, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(1.0)
        source = ["--tap-global"] if args.global_mix else ["--tap-pid", str(mpv.pid)]
        cmd = [sys.executable, "-m", "a2a_runtime.cli", *source,
               "--seconds", f"{expected['duration_s'] + 2:.1f}", "--tail-seconds", str(args.tail),
               "--asr", "whisper", "--mt", "llm", "--tts", args.tts, "--fluent", "--translation-only",
               "--speakers", args.speakers, "--research-text", "--trace", str(out / "trace.jsonl"),
               "--source-out", str(out / "source.wav"), "--translated-out", str(out / "translated.wav"),
               "--rendered-out", str(out / "rendered.wav"), "--session", out.name[:16], *args.extra]
        if args.output_device:
            cmd += ["--output-device", args.output_device]
        pipeline = subprocess.Popen(cmd, cwd=REPO_ROOT / "runtime", stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        live = threading.Event()
        stderr_lines: list[str] = []

        def read_stderr() -> None:
            for raw in pipeline.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                stderr_lines.append(line)
                if "live: translating" in line:
                    live.set()
                if line.startswith("a2a-pipeline:"):
                    print(line, flush=True)

        threading.Thread(target=read_stderr, daemon=True).start()
        stdout = []
        threading.Thread(target=lambda: stdout.extend(pipeline.stdout.read().decode("utf-8").splitlines()),
                         daemon=True).start()
        while not live.wait(0.5):
            if pipeline.poll() is not None:
                raise SystemExit("pipeline exited before it went live:\n" + "\n".join(stderr_lines[-20:]))
        ipc(pipe, ["loadfile", str(clip)])
        pipeline.wait()
        time.sleep(0.5)
    finally:
        mpv.kill()
        mpv.wait()
    (out / "pipeline.stderr.txt").write_text("\n".join(stderr_lines) + "\n", encoding="utf-8")
    if not stdout:
        raise SystemExit("pipeline produced no summary")
    (out / "summary.json").write_text(stdout[-1] + "\n", encoding="utf-8")
    subprocess.run([sys.executable, "-m", "a2a_runtime.tools.transcribe", "--input", str(out / "translated.wav"),
                    "--out", str(out / "transcript.json")], cwd=REPO_ROOT / "runtime", check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    check = subprocess.run(["uv", "run", "--project", str(REPO_ROOT / "harness"), "python", "-m",
                            "a2a_harness.live_check", "--run", str(out), "--expected", str(args.clip_dir / "expected.json"),
                            "--transcript", str(out / "transcript.json")], cwd=REPO_ROOT)
    return check.returncode


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
