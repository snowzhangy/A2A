#!/usr/bin/env bash
# Native UI startup with synthetic captions, no capture or model inference.
# Needs a logged-in desktop; separate from the ordinary check command.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/env.sh"
APP="${A2A_APP_PATH:-$ROOT/.local/apps/A2A.app}"
[[ -d "$APP" ]] || { echo "Run scripts/build_app.sh first" >&2; exit 1; }
mkdir -p "$ROOT/.local/runs"
OUT="$(mktemp -d "$ROOT/.local/runs/app-smoke.XXXXXX")"
# The preview never captures audio. Close only the instance for this run, including on failure.
cleanup() {
  local pids
  pids="$(pgrep -f -- "--smoke-output $OUT/app.png" || true)"
  if [[ -n "$pids" ]]; then kill $pids 2>/dev/null || true; fi
}
trap cleanup EXIT
if [[ "${A2A_SMOKE_SETUP:-0}" == 1 ]]; then
  open -n "$APP" --args --smoke-output "$OUT/app.png" --smoke-setup
else
  open -n "$APP" --args --smoke-output "$OUT/app.png"
fi
for _ in {1..150}; do
  [[ -f "$OUT/app.json" ]] && break
  sleep 0.1
done
[[ -f "$OUT/app.json" ]] || { echo "App did not produce startup evidence" >&2; exit 1; }
WINDOW="$(python3 -c 'import json,sys; e=json.load(open(sys.argv[1])); assert e["mode"] == "synthetic-ui" and not e["running"]; assert e["playback_initial"] == "translated" and e["playback_original_selected"] and e["playback_final"] == "translated"; assert e["direction_initial"] == "zh-en" and e["direction_final"] == "en-zh" and e["direction_reset_readiness"]; assert e["qwen_ready"] and e["voice_gate"] and e["direct_pipeline"] and e["speaker_modes"] and e["missing_reference_blocked"] and e["short_reference_blocked"] and e["valid_reference_accepted"] and e["multiple_clears_reference"] and e["setup_succeeded"]; print(e["window_number"])' "$OUT/app.json")"
[[ "$WINDOW" -gt 0 ]] || { echo "App did not create a window" >&2; exit 1; }
if [[ "${1:-}" == "--screenshot" ]]; then
  screencapture -x -o -l "$WINDOW" "$OUT/app.png"
  [[ -s "$OUT/app.png" ]] || { echo "Window screenshot is empty" >&2; exit 1; }
fi
echo "$OUT"
