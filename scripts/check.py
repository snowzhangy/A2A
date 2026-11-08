#!/usr/bin/env python3
"""Cross-platform deterministic check: the Windows counterpart of scripts/check.sh, and the
portable runtime's check on macOS (ADR-0007). Standard library only, so any Python 3.9+
can run it; each project runs in its own uv environment.

    python scripts/check.py                 everything deterministic for this platform
    python scripts/check.py --quick         skip the harness <-> pipeline integration
    python scripts/check.py --models        also the opt-in real-model tests (installed models, GPU)

macOS: scripts/check.sh (Swift build and tests, manifest gate, harness against the Swift
pipeline), then the portable runtime. Elsewhere: manifest gate, native shim build, runtime
tests (golden traces shared with Swift), harness against the portable pipeline. No capture,
devices, listeners, or downloads, except --models, which loads installed models.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def step(title: str) -> None:
    print(f"\n== {title}", flush=True)


def run(cmd: list, env: dict | None = None) -> None:
    started = time.monotonic()
    done = subprocess.run(cmd, cwd=ROOT, env={**os.environ, **(env or {})})
    if done.returncode != 0:
        print(f"check failed: {' '.join(map(str, cmd))} exited {done.returncode}", file=sys.stderr)
        sys.exit(1)
    print(f"({time.monotonic() - started:.1f} s)", flush=True)


def portable_pipeline() -> Path:
    scripts, suffix = ("Scripts", ".exe") if os.name == "nt" else ("bin", "")
    return ROOT / "runtime" / ".venv" / scripts / f"a2a-pipeline-portable{suffix}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--quick", action="store_true", help="skip the harness <-> pipeline integration tests")
    ap.add_argument("--models", action="store_true", help="also run the opt-in real-model runtime tests")
    args = ap.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        sys.exit("check: uv is required (https://docs.astral.sh/uv/)")

    step("toolchain")
    print(f"{platform.platform()} {platform.machine()}; python {platform.python_version()}")
    subprocess.run([uv, "--version"], cwd=ROOT)

    mac = sys.platform == "darwin"
    if mac:
        step("Swift and harness (scripts/check.sh)")
        run(["bash", "scripts/check.sh"] + (["--quick"] if args.quick else []))
    else:
        step("model manifest gate")
        run([uv, "run", "--project", "harness", "python", "scripts/fetch_models.py", "--check"])

    step("native shim")
    run([uv, "run", "--project", "runtime", "python", "runtime/native/build.py"])

    step("portable runtime tests")
    # An implicit locale encoding is a bug that the UTF-8 code page on a developer machine
    # hides: every open() and subprocess text stream names its encoding.
    gate = {"PYTHONWARNDEFAULTENCODING": "1"}
    run([uv, "run", "--project", "runtime", "pytest", "runtime/tests", "-q"], env=gate)
    if args.models:
        step("real-model runtime tests (installed models)")
        run([uv, "run", "--project", "runtime", "pytest", "runtime/tests/test_model_runtime.py", "-q"],
            env={**gate, "A2A_RUN_MODEL_TESTS": "1"})

    if not mac:
        step("harness tests (portable pipeline)")
        pipeline = portable_pipeline()
        if not pipeline.exists():
            sys.exit(f"check: {pipeline} is missing; run uv sync --project runtime --locked")
        env = {**gate, "A2A_PIPELINE": str(pipeline)}
        extra = ["--ignore", str(ROOT / "harness" / "tests" / "test_runner.py")] if args.quick else []
        run([uv, "run", "--project", "harness", "pytest", "harness/tests", "-q", *extra], env=env)

    step("ok")


if __name__ == "__main__":
    main()
