"""Wait until this Mac has room for another Metal model.

The live lab loads a recogniser, a translator and a clone at once on a 36 GB machine, and MLX
fails with `Insufficient Memory` rather than queueing — a failed load mid-scene is worse than
waiting. This gate passes when nothing else is holding the GPU and memory has come back.

    python waitgpu.py [--need 10] [--timeout 1800]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

# Anything that means another session is doing inference or a live capture.
BUSY = re.compile(r"a2a-pipeline|tts_worker|live-dub|separate\.py|asr\.py|bench_stages|"
                  r"mlx|uvicorn main:app", re.I)
SKIP = re.compile(r"waitgpu\.py|grep|bash -c", re.I)


def holders() -> list[tuple[int, float, str]]:
    out = subprocess.run(["ps", "ax", "-o", "pid=,rss=,etime=,args="], capture_output=True,
                         text=True).stdout
    rows = []
    for line in out.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4 or not parts[0].isdigit():
            continue
        if BUSY.search(parts[3]) and not SKIP.search(parts[3]):
            rows.append((int(parts[0]), int(parts[1]) / 1024.0, parts[3][:90]))
    return sorted(rows, key=lambda r: -r[1])


def free_percent() -> float:
    out = subprocess.run(["memory_pressure"], capture_output=True, text=True).stdout
    m = re.search(r"free percentage: (\d+)%", out)
    return float(m.group(1)) if m else 0.0


def page_free_gb() -> float:
    stats: dict[str, int] = {}
    for line in subprocess.run(["vm_stat"], capture_output=True, text=True).stdout.splitlines():
        m = re.match(r"(\w[^:]*):\s+(\d+)", line)
        if m:
            stats[m.group(1).replace(" ", "_").lower()] = int(m.group(2))
    page = stats.get("page_size", 16384)
    free = stats.get("pages_free", 0) + stats.get("pages_inactive", 0)
    return free * page / 2**30


def ready(need_gb: float, min_free_percent: float) -> tuple[bool, str]:
    busy = holders()
    heavy = [r for r in busy if r[1] > 200]           # >200 MB resident is holding a model
    gb, pct = page_free_gb(), free_percent()
    ok = not heavy and gb >= need_gb and pct >= min_free_percent
    why = f"free {gb:.1f} GB ({pct:.0f}%)"
    if heavy:
        why += f"; {len(heavy)} model process(es): " + ", ".join(f"{p} MB pid {i}" for i, p, _ in heavy[:3])
    return ok, why


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--need", type=float, default=10.0, help="GB free needed (recogniser+MT+clone)")
    ap.add_argument("--min-free-percent", type=float, default=40.0)
    ap.add_argument("--timeout", type=float, default=1800.0)
    ap.add_argument("--interval", type=float, default=15.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    deadline = time.time() + args.timeout
    while True:
        ok, why = ready(args.need, args.min_free_percent)
        stamp = time.strftime("%H:%M:%S")
        print(f"{stamp}  {'ready' if ok else 'waiting'}  {why}", flush=True)
        if ok or args.once:
            return 0 if ok else 1
        if time.time() >= deadline:
            print(f"{stamp}  gave up after {args.timeout:.0f}s", flush=True)
            return 1
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
