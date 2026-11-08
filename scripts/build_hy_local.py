#!/usr/bin/env python3
"""Build the pinned Hy-SimulMT worker on Windows (CUDA) or macOS (Metal).

This is explicit setup, never session startup. Model downloads remain fetch_models.py's job.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/lihy715/llama.cpp.Hyv3Speech.git"
COMMIT = "e41ca4212f786815faa37606ee2e6fcf8611f9e4"
WORK = ROOT / ".local" / "hy-simulmt"
SOURCE = WORK / "llama.cpp"
OUTPUT = WORK / "bin" / ("hy-worker.exe" if sys.platform == "win32" else "hy-worker")


def run(args, cwd=None):
    print("+", " ".join(str(arg) for arg in args), flush=True)
    subprocess.run([str(arg) for arg in args], cwd=cwd, check=True)


def source_at_revision(fetch=True):
    if not (SOURCE / ".git").is_dir():
        if not fetch:
            raise SystemExit(f"pinned Hy source is not installed at {SOURCE}")
        WORK.mkdir(parents=True, exist_ok=True)
        run(["git", "init", "-q", SOURCE])
        run(["git", "-C", SOURCE, "remote", "add", "origin", REPO])
    head = subprocess.run(["git", "-C", SOURCE, "rev-parse", "HEAD"],
                          capture_output=True, text=True, encoding="utf-8", check=False).stdout.strip()
    if head != COMMIT:
        if not fetch:
            raise SystemExit(f"Hy source is {head or 'unknown'}, expected {COMMIT}")
        run(["git", "-C", SOURCE, "fetch", "-q", "--depth", "1", "origin", COMMIT])
        run(["git", "-C", SOURCE, "checkout", "-q", "--detach", "FETCH_HEAD"])
    actual = subprocess.run(["git", "-C", SOURCE, "rev-parse", "HEAD"],
                            capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()
    if actual != COMMIT:
        raise SystemExit("Hy source revision verification failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cpu", action="store_true", help="disable CUDA/Metal acceleration")
    parser.add_argument("--no-fetch", action="store_true", help="use an existing pinned source checkout only")
    parser.add_argument("--check", action="store_true", help="check source revision and worker presence without building")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()
    if shutil.which("git") is None or shutil.which("cmake") is None:
        raise SystemExit("Install git and CMake before building the Hy worker")
    source_at_revision(fetch=not args.no_fetch)
    if args.check:
        if not OUTPUT.is_file():
            raise SystemExit(f"Hy worker is not built: {OUTPUT}")
        print(OUTPUT)
        return

    build = SOURCE / "build"
    configure = ["cmake", "-S", SOURCE, "-B", build, "-DCMAKE_BUILD_TYPE=Release",
                 "-DLLAMA_CURL=OFF", "-DLLAMA_BUILD_TESTS=OFF", "-DLLAMA_BUILD_EXAMPLES=OFF"]
    accelerated = not args.cpu
    if sys.platform == "darwin":
        configure.append(f"-DGGML_METAL={'ON' if accelerated else 'OFF'}")
    elif accelerated and shutil.which("nvcc"):
        configure.append("-DGGML_CUDA=ON")
    run(configure)
    run(["cmake", "--build", build, "--config", "Release", "-j", args.jobs, "--target", "llama-mtmd-cli"])

    worker_build = WORK / "worker-build"
    run(["cmake", "-S", ROOT / "research" / "hy_worker", "-B", worker_build,
         "-DCMAKE_BUILD_TYPE=Release", f"-DLLAMA_ROOT={SOURCE}"])
    run(["cmake", "--build", worker_build, "--config", "Release", "-j", args.jobs, "--target", "hy-worker"])
    if sys.platform == "win32":
        # The worker sits beside its runtime DLLs; setup never requires PATH changes.
        for directory in (build / "bin", build / "bin" / "Release"):
            if directory.is_dir():
                for dll in directory.glob("*.dll"):
                    shutil.copy2(dll, OUTPUT.parent / dll.name)
    if not OUTPUT.is_file():
        raise SystemExit(f"worker build did not produce {OUTPUT}")
    print(f"built {OUTPUT} against {COMMIT}")


if __name__ == "__main__":
    main()
