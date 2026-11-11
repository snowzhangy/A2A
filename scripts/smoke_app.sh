#!/usr/bin/env bash
# Native UI startup with synthetic captions, no capture or model inference.
# Needs a logged-in desktop; separate from the ordinary check command.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/env.sh"
SMOKE_PYTHON="$(command -v python3)"
APP="${A2A_APP_PATH:-$ROOT/.local/apps/A2A.app}"
[[ -d "$APP" ]] || { echo "Run scripts/build_app.sh first" >&2; exit 1; }
mkdir -p "$ROOT/.local/runs"
OUT="$(mktemp -d "$ROOT/.local/runs/app-smoke.XXXXXX")"
# The preview never captures audio. Close only the instance for this run, including on failure.
SMOKE_PID=""
cleanup() {
  if [[ -n "$SMOKE_PID" ]]; then
    kill "$SMOKE_PID" 2>/dev/null || true
    wait "$SMOKE_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT
SMOKE_ARGS=(--smoke-output "$OUT/app.png")
if [[ "${A2A_SMOKE_RESET:-0}" == 1 ]]; then
  printf '#!%s\n' "$SMOKE_PYTHON" > "$OUT/fake-pipeline"
  cat >> "$OUT/fake-pipeline" <<'PY'
import json, os, signal, sys, threading, time
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
def on_eof():
    sys.stdin.buffer.read()
    os._exit(0)
threading.Thread(target=on_eof, daemon=True).start()
print(json.dumps({"voice_status": "ready", "speaker": "S1", "seconds": 3}), flush=True)
while True:
    time.sleep(0.1)
PY
  chmod +x "$OUT/fake-pipeline"
  SMOKE_ARGS+=(--smoke-reset --smoke-pipeline "$OUT/fake-pipeline")
elif [[ "${A2A_SMOKE_SETUP:-0}" == 1 ]]; then
  SMOKE_ARGS+=(--smoke-setup)
fi
# Launch the exact executable: Launch Services can select an older registered A2A copy.
"$APP/Contents/MacOS/A2A" "${SMOKE_ARGS[@]}" &
SMOKE_PID=$!
for _ in {1..300}; do
  [[ -f "$OUT/app.json" ]] && break
  sleep 0.1
done
[[ -f "$OUT/app.json" ]] || { echo "App did not produce startup evidence" >&2; exit 1; }
WINDOW="$("$SMOKE_PYTHON" - "$OUT/app.json" "$ROOT/VERSION" <<'PY'
import json
from pathlib import Path
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    evidence = json.load(stream)
assert evidence["mode"] == "synthetic-ui" and not evidence["running"]
expected_version = Path(sys.argv[2]).read_text(encoding="utf-8").strip()
assert evidence["app_version"] == expected_version
assert evidence["window_title"] == f"A2A v{expected_version}"
assert evidence["playback_initial"] == evidence["playback_final"] == "translated"
assert evidence["direction_initial"] == "zh-en" and evidence["direction_final"] == "en-zh"
checks = (
    "playback_original_selected", "direction_reset_readiness", "qwen_ready", "voice_gate",
    "direct_pipeline", "speaker_modes", "memory_modes", "missing_reference_blocked",
    "short_reference_blocked", "valid_reference_accepted", "multiple_clears_reference",
    "setup_succeeded", "reset_restarted", "reset_cleared_reference", "reset_suppresses_stale",
    "reset_keeps_delay", "stop_cancels_reset",
)
assert all(evidence[key] for key in checks), {key: evidence[key] for key in checks if not evidence[key]}
print(evidence["window_number"])
PY
)"
[[ "$WINDOW" -gt 0 ]] || { echo "App did not create a window" >&2; exit 1; }
if [[ "${1:-}" == "--screenshot" ]]; then
  screencapture -x -o -l "$WINDOW" "$OUT/app.png"
  [[ -s "$OUT/app.png" ]] || { echo "Window screenshot is empty" >&2; exit 1; }
fi
echo "$OUT"
