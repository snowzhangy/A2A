#!/usr/bin/env bash
# Explicit first-run setup for the source-built app. Nothing is downloaded at session start.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/env.sh"

MODE=local
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server) MODE=server ;;
    --help) echo "Usage: scripts/setup.sh [--server]"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

for tool in uv git; do
  command -v "$tool" >/dev/null || { echo "Install $tool and run setup again." >&2; exit 1; }
done
if [[ "$MODE" == local ]]; then
  command -v cmake >/dev/null || { echo "Install CMake and run setup again." >&2; exit 1; }
  xcrun --sdk macosx --show-sdk-path >/dev/null || { echo "Install full Xcode and run setup again." >&2; exit 1; }
fi

cd "$ROOT"
echo "Preparing the voice environment..."
uv sync --project research --locked
if [[ "$MODE" == local && ! -x .local/hy-simulmt/bin/hy-worker ]]; then
  echo "Building the Hy-SimulMT worker..."
  scripts/build_hy_local.sh
fi
MODELS="qwen3-tts-12hz-0.6b-base,ecapa-voxlingua107-lid-mlx"
if [[ "$MODE" == local ]]; then
  MODELS="hy-simulmt-30b-a3b-q4km-gguf,$MODELS"
fi
echo "Downloading and verifying the selected models..."
uv run --project harness python scripts/fetch_models.py --models "$MODELS"
echo "Setup complete."
