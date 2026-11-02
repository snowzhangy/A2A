#!/usr/bin/env bash
# Hy-SimulMT worker latency benchmark (research only: needs the 19.7 GB model, no capture, no devices).
#
# Answers one question with numbers: how long does a span take, and how long does the host still wait once
# the span is closed. It drives the worker's own test mode over a real Mandarin fixture in 3 s spans streamed
# at 100 ms (the live streaming shape) and prints the median span, the median wait after flush, the worst
# span, and a hash of the translated text — so a change that alters output can never be read as a speed win.
#
#   scripts/bench_hy_local.sh                    # merged single-batch turns vs the per-chunk helper
#   scripts/bench_hy_local.sh --load             # both again with a Qwen3-TTS worker generating at the same
#                                                # time, which is the contention the live app really runs in
#   scripts/bench_hy_local.sh --runs 3 --spans 10 --wav X --model Y --no-build --extra "--kv q8_0"
#
# Standalone equivalent (what this script runs, one config):
#   .local/hy-simulmt/bin/hy-worker --model GGUF --test WAV --cuts 3 --from A --to B --stream-ms 100
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/env.sh"

WORKER="$ROOT/.local/hy-simulmt/bin/hy-worker"
SRC="$ROOT/research/hy_worker/hy_worker.cpp"
MODEL=""
WAV=""
RUNS=2
SPANS=10
LOAD=0
BUILD=1
EXTRA=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --wav) WAV="$2"; shift 2 ;;
    --runs) RUNS="$2"; shift 2 ;;
    --spans) SPANS="$2"; shift 2 ;;
    --load) LOAD=1; shift ;;
    --no-build) BUILD=0; shift ;;
    --extra) EXTRA="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$MODEL" ]]; then
  MODEL="$(ls "$ROOT"/.local/models/hy-simulmt-30b-a3b-q4km-gguf/*/Hy-SimulMT-30B-A3B-Q4_K_M.gguf 2>/dev/null | head -1 || true)"
fi
[[ -n "$MODEL" && -f "$MODEL" ]] || {
  echo "model not found; run: uv run --project harness python scripts/fetch_models.py --models hy-simulmt-30b-a3b-q4km-gguf" >&2
  exit 1
}
if [[ -z "$WAV" ]]; then
  WAV="$(ls "$ROOT"/.local/fixtures/*stage*.wav 2>/dev/null | head -1 || true)"
fi
[[ -n "$WAV" && -f "$WAV" ]] || { echo "no speech fixture; pass --wav PATH" >&2; exit 1; }

if [[ "$BUILD" == 1 ]] && { [[ ! -x "$WORKER" ]] || [[ "$SRC" -nt "$WORKER" ]]; }; then
  echo "== building the worker"
  "$ROOT/scripts/build_hy_local.sh"
fi
[[ -x "$WORKER" ]] || { echo "no worker at $WORKER (run scripts/build_hy_local.sh)" >&2; exit 1; }

# Seconds of fixture to cover, from the requested span count.
DURATION=$((SPANS * 3))

run() { # run <label> <worker args...>
  local label="$1"; shift
  for ((r = 1; r <= RUNS; r++)); do
    printf '%-26s run%d  ' "$label" "$r"
    # shellcheck disable=SC2086
    "$WORKER" --model "$MODEL" --test "$WAV" --cuts 3 --from 10 --to $((10 + DURATION)) \
      --stream-ms 100 "$@" $EXTRA 2>&1 | grep -a "^spans" || echo "FAILED"
  done
}

echo "== fixture $WAV (10 s .. $((10 + DURATION)) s), $SPANS spans of 3 s, streamed at 100 ms"
run "per-chunk batches" --no-merge-prompt
run "one batch per turn"

if [[ "$LOAD" == 1 ]]; then
  # A TTS worker generating English in another process: the GPU sharing the live app sees, where the gain of
  # fewer submissions is largest (each submit is a Metal graph competing with the other process's graphs).
  command -v ffmpeg >/dev/null || { echo "--load needs ffmpeg for the 24 kHz voice reference" >&2; exit 1; }
  mkdir -p "$ROOT/.local/bench"
  REFDIR="$(mktemp -d -t a2a-bench-ref)"
  REFPCM="$REFDIR/ref.wav"
  ffmpeg -v error -y -i "$WAV" -t 3 -ac 1 -ar 24000 -c:a pcm_f32le "$REFPCM"
  uv run --project research python "$ROOT/research/tts_bench.py" \
    --reference "$REFPCM" --repeat "$((RUNS * 4))" >"$ROOT/.local/bench/tts-load.log" 2>&1 &
  LOADPID=$!
  sleep 20  # let the worker load, warm up and start speaking
  run "per-chunk, TTS load" --no-merge-prompt
  run "one batch, TTS load"
  wait "$LOADPID" || true
  rm -rf "$REFDIR"
  echo "== TTS units completed under load: $(grep -c '^unit ' "$ROOT/.local/bench/tts-load.log" || true)"
fi
