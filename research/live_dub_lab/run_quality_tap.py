"""Interactive Mac: tap only our synchronized FixturePlayer, translate with local models.

Requires existing capture permission. Saves restricted source, rendered output and traces.
This plays translated audio on the default device; it does not perform physical loopback.
"""
import argparse
import json
import select
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--context", action="store_true", help="research local contextual MT instead of Apple MT")
    p.add_argument("--series", type=Path)
    p.add_argument("--responsive-background", action="store_true", help="stream with separated music/effects and a smaller delay budget")
    p.add_argument("--debt", type=float, default=6)
    p.add_argument("--lag", type=float, default=8)
    a = p.parse_args()
    for path in (a.input, a.out):
        path.resolve().relative_to((ROOT / ".local").resolve())
    if a.out.exists():
        raise FileExistsError("use a fresh evidence directory")
    binary = ROOT / "apple/.build/debug"
    permission = subprocess.run([str(binary / "a2a-tapctl"), "permission"], capture_output=True, text=True, check=True)
    if json.loads(permission.stdout).get("audio_capture") != "granted":
        raise RuntimeError("existing System Audio Recording permission is required")
    a.out.mkdir(mode=0o700, parents=True)
    player = helper = None
    try:
        player = subprocess.Popen([str(binary / "FixturePlayer"), "--file", str(a.input),
            "--seconds", "100", "--wait-for-stdin"], cwd=ROOT,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if not select.select([player.stdout], [], [], 15)[0]:
            raise RuntimeError("test player did not open output")
        info = json.loads(player.stdout.readline())
        (a.out / "player.json").write_text(json.dumps(info, indent=2) + "\n")
        command = [str(binary / "a2a-pipeline"), "--tap-pid", str(player.pid),
            "--asr", "apple", "--mt", "context" if a.context else "apple", "--tts", "clone", "--clone-model", "qwen",
            "--quality-first", "--fluent", "--speakers", "auto", "--translation-only", "--research-text",
            "--seconds", "50", "--tail-seconds", "30", "--trace", str(a.out / "trace.jsonl"),
            "--source-out", str(a.out / "source.wav"), "--rendered-out", str(a.out / "rendered.wav"),
            "--translated-out", str(a.out / "translated.wav"),
            "--glossary", str(ROOT / ".local/runs/glossary-test/glossary.json"),
            "--pronounce", str(ROOT / ".local/runs/glossary-test/pronounce.json")]
        if a.context:
            command += ["--context-python", str(ROOT / "research/live_dub_lab/.venv/bin/python"),
                        "--context-worker", str(ROOT / "research/live_dub_lab/context_worker.py")]
            if a.series:
                a.series.resolve().relative_to((ROOT / ".local").resolve())
                command += ["--context-series", str(a.series)]
        if a.responsive_background:
            command.remove("--quality-first")
            command += ["--adaptive-prebuffer", "--max-debt-seconds", str(a.debt), "--max-source-lag-seconds", str(a.lag),
                        "--background", "--background-out", str(a.out / "background.wav")]
        with (a.out / "summary.log").open("w") as stdout, (a.out / "run.log").open("w") as stderr:
            helper = subprocess.Popen(command, cwd=ROOT, stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + 120
            while "live: translating" not in (a.out / "run.log").read_text():
                if helper.poll() is not None:
                    raise RuntimeError("pipeline failed before live readiness; see run.log")
                if time.monotonic() > deadline:
                    raise TimeoutError("pipeline warmup exceeded 120 seconds")
                time.sleep(0.1)
            player.stdin.write("\n")
            player.stdin.flush()
            print("Live tap ready; source playback started after model warmup", flush=True)
            result = helper.wait(timeout=150)
        if result:
            raise RuntimeError(f"pipeline exited {result}; see run.log")
        print(f"Actual process-tap evidence: {a.out}", flush=True)
    finally:
        for child in (helper, player):
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


if __name__ == "__main__":
    main()
