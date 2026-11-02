"""Compare file-paced live queue settings. Does not capture or play a device."""
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    local = (ROOT / ".local").resolve()
    args.input.resolve().relative_to(local)
    args.out.resolve().relative_to(local)
    if args.out.exists():
        raise FileExistsError("choose a new output directory")
    args.out.mkdir(mode=0o700, parents=True)
    os.chmod(args.out, 0o700)
    binary = ROOT / ".local/apps/A2A.app/Contents/Helpers/a2a-pipeline"
    for label, debt, lag in (("baseline", "3", "4"), ("more_context", "6", "8")):
        out = args.out / label
        out.mkdir(mode=0o700)
        command = [str(binary), "--input", str(args.input.resolve()), "--clock", "host",
                   "--pace", "realtime", "--asr", "apple", "--mt", "apple", "--tts", "clone",
                   "--clone-model", "qwen", "--fluent", "--speakers", "auto", "--translation-only",
                   "--research-text", "--max-debt-seconds", debt, "--max-source-lag-seconds", lag,
                   "--tail-seconds", "30", "--trace", str(out / "trace.jsonl"),
                   "--out", str(out / "output.wav"), "--translated-out", str(out / "translated.wav")]
        print(f"Running {label}: debt={debt}s, source lag={lag}s", flush=True)
        with (out / "run.log").open("w") as log:
            completed = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        if completed.returncode:
            raise RuntimeError(f"{label} failed; see {out / 'run.log'}")
        subprocess.run(["python3", str(ROOT / "research/run_metrics.py"), str(out)],
                       cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
