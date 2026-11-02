"""File-paced lower-step OmniVoice pipeline experiment; no tap or output device."""
import argparse
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--steps", type=int, choices=(8, 16, 32), default=8)
    p.add_argument("--binary", type=Path, default=ROOT / "apple/.build/debug/a2a-pipeline")
    a = p.parse_args()
    for path in (a.out, a.input):
        path.resolve().relative_to((ROOT / ".local").resolve())
    if a.out.exists():
        raise FileExistsError("choose a new output directory")
    a.out.mkdir(mode=0o700, parents=True)
    os.chmod(a.out, 0o700)
    command = [str(a.binary), "--input", str(a.input), "--clock", "host", "--pace", "realtime",
               "--asr", "apple", "--mt", "apple", "--tts", "clone", "--clone-model", "omnivoice",
               "--clone-worker", str(ROOT / "research/live_dub_lab/omni_worker.py"),
               "--fluent", "--speakers", "auto", "--translation-only", "--research-text",
               "--max-debt-seconds", "6", "--max-source-lag-seconds", "8", "--tail-seconds", "30",
               "--trace", str(a.out / "trace.jsonl"), "--out", str(a.out / "output.wav"),
               "--translated-out", str(a.out / "translated.wav")]
    env = dict(os.environ, A2A_LAB_OMNI_STEPS=str(a.steps))
    with (a.out / "run.log").open("w") as log:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(f"pipeline failed; see {a.out / 'run.log'}")
    subprocess.run(["python3", str(ROOT / "research/run_metrics.py"), str(a.out)], cwd=ROOT, check=True)


if __name__ == "__main__":
    main()
