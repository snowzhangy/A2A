"""Convert an A2A trace (JSON Lines, schema v0) to Chrome trace JSON for Perfetto.

    uv run --project harness python harness/trace2perfetto.py run/trace.jsonl -o run/trace.perfetto.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from a2a_harness import perfetto, trace


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("trace", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--include-buffers", action="store_true", help="emit buf.* and tts.chunk instants too")
    args = ap.parse_args()
    events = trace.load(args.trace)
    out = args.output or args.trace.with_suffix(".perfetto.json")
    out.write_text(json.dumps(perfetto.convert(events, include_buffers=args.include_buffers)))
    print(out)


if __name__ == "__main__":
    main()
