"""Test existing app memory across two file-paced plays of the same scene."""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    for path in (args.input, args.out):
        path.resolve().relative_to((ROOT / ".local").resolve())
    if args.out.exists():
        raise FileExistsError("choose a new output directory")
    args.out.mkdir(mode=0o700, parents=True)
    os.chmod(args.out, 0o700)
    binary = ROOT / ".local/apps/A2A.app/Contents/Helpers/a2a-pipeline"
    for label in ("enrollment", "replay"):
        out = args.out / label
        out.mkdir(mode=0o700)
        command = [str(binary), "--input", str(args.input.resolve()), "--clock", "host", "--pace", "realtime",
                   "--asr", "apple", "--mt", "apple", "--tts", "clone", "--clone-model", "qwen",
                   "--fluent", "--speakers", "auto", "--translation-only", "--research-text",
                   "--max-debt-seconds", "6", "--max-source-lag-seconds", "8", "--tail-seconds", "30",
                   "--voice-memory", str(args.out / "existing-app-memory"), "--trace", str(out / "trace.jsonl"),
                   "--out", str(out / "output.wav"), "--translated-out", str(out / "translated.wav")]
        print(f"Running {label}", flush=True)
        with (out / "run.log").open("w") as log:
            result = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f"{label} failed; see {out / 'run.log'}")
        subprocess.run(["python3", str(ROOT / "research/run_metrics.py"), str(out)], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
