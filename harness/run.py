"""Replay a fixture through a2a-pipeline and write an immutable run directory.

    uv run --project harness python harness/run.py --fixture fixtures/synthetic/synthetic-zh.yaml \\
        --pipeline "$(swift build --package-path apple --show-bin-path)/a2a-pipeline" --mode paced
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from a2a_harness import REPO_ROOT
from a2a_harness.runner import RunConfig, run


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixture", type=Path, required=True)
    ap.add_argument("--pipeline", type=Path, required=True)
    ap.add_argument("--mode", choices=["offline", "paced", "tap"], default="paced")
    ap.add_argument("--out-dir", type=Path)
    ap.add_argument("--speed", type=float, default=1.0, help="paced feed speed; anything but 1.0 is not gate-valid")
    ap.add_argument("--asr", default="scripted")
    ap.add_argument("--mt", default="dictionary")
    ap.add_argument("--tts", default="tone")
    ap.add_argument("--profile-ready-ms", type=int, default=0, help="0 = prepared profile; >0 = cold acquisition")
    ap.add_argument("--no-text", action="store_true", help="product-mode trace without text (disables alignment)")
    args = ap.parse_args()
    out = args.out_dir or REPO_ROOT / ".local" / "runs" / time.strftime(f"%Y%m%d-%H%M%S-{args.mode}")
    result = run(RunConfig(fixture=args.fixture, pipeline=args.pipeline, out_dir=out, mode=args.mode,
                           speed=args.speed, asr=args.asr, mt=args.mt, tts=args.tts,
                           profile_ready_ms=args.profile_ready_ms, research_text=not args.no_text))
    print((out / "report.md").read_text(encoding="utf-8"))
    print(json.dumps({"out_dir": str(out), "gate_valid": result["gate_valid"],
                      "streaming_gate_pass": result["streaming_gate"]["pass"]}))


if __name__ == "__main__":
    main()
