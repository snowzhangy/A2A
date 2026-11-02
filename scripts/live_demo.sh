#!/usr/bin/env bash
# Live plumbing demo and check (interactive Mac only): ffplay plays a Mandarin video,
# a2a-pipeline taps ffplay (muted while tapped) and speaks the English translation on
# the default output, then the run is checked sentence by sentence and back-transcribed.
#
#   scripts/live_demo.sh [--out DIR] [--fixture DIR] [pipeline options...]
#
# Defaults: --out .local/runs/live-demo, --fixture .local/fixtures/zh-demo (generated with
# a2a_harness.zh_demo when missing). Extra options go to a2a-pipeline, for example
# `--max-debt-seconds 6 --max-source-lag-seconds 8`. Needs System Audio Recording for the
# terminal, the Hy-SimulMT server, the English speech asset for back-transcription, and ffplay. The system voice preserves no speaker
# identity: results are plumbing only.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/env.sh"
OUT="$ROOT/.local/runs/live-demo"
FIXTURE="$ROOT/.local/fixtures/zh-demo"
PASSTHROUGH=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2 ;;
    --fixture) FIXTURE="$2"; shift 2 ;;
    *) PASSTHROUGH+=("$1"); shift ;;
  esac
done
command -v ffplay >/dev/null || { echo "live_demo: ffplay not found (brew install ffmpeg)" >&2; exit 2; }
if [[ ! -f "$FIXTURE/video.mp4" ]]; then
  uv run --project "$ROOT/harness" python -m a2a_harness.zh_demo --out "$FIXTURE" --lead 4 >&2
fi
swift build --package-path "$ROOT/apple" --product a2a-pipeline >&2
swift build --package-path "$ROOT/apple" --product a2a-transcribe >&2
BIN="$(swift build --package-path "$ROOT/apple" --show-bin-path)"
DURATION="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['duration_s'])" "$FIXTURE/expected.json")"
SECONDS_LIVE="$(python3 -c "print(round($DURATION + 6))")"

rm -rf "$OUT"
mkdir -p "$OUT"
ffplay -autoexit -loglevel error -window_title "A2A demo (Mandarin source)" -x 640 -y 360 "$FIXTURE/video.mp4" \
  > "$OUT/ffplay.log" 2>&1 &
PLAYER=$!
trap 'kill $PLAYER 2>/dev/null || true' EXIT
status=0
"$BIN/a2a-pipeline" --tap-pid "$PLAYER" --asr audio --mt hy --tts system --research-text \
  --seconds "$SECONDS_LIVE" --tail-seconds 15 --trace "$OUT/trace.jsonl" \
  --source-out "$OUT/source.wav" --translated-out "$OUT/translated.wav" ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"} \
  > "$OUT/summary.json" 2> >(tee "$OUT/pipeline.log" >&2) || status=$?
if [[ $status -ne 0 ]]; then
  echo "live_demo: a2a-pipeline exited $status" >&2
  exit "$status"
fi
"$BIN/a2a-transcribe" --input "$OUT/translated.wav" > "$OUT/transcript.json"
uv run --project "$ROOT/harness" python -m a2a_harness.live_check --run "$OUT" \
  --expected "$FIXTURE/expected.json" --transcript "$OUT/transcript.json"
