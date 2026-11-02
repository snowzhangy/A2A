#!/usr/bin/env bash
# Build the Hy-SimulMT llama.cpp fork with Metal, then the native Hy worker against it (research only).
#
# Upstream llama.cpp (and Homebrew's build) has no `hy_v3` support yet: it lives in the unmerged
# PR ggml-org/llama.cpp#31198, pinned here to lihy715/llama.cpp.Hyv3Speech @ e41ca42.
# Output: .local/hy-simulmt/bin/hy-worker (research/hy_worker/hy_worker.cpp), which a2a-pipeline starts
# per session the way it starts research/tts_worker.py, linked to the fork's libllama/libmtmd in
# .local/hy-simulmt/llama.cpp/build/bin (llama-mtmd-cli is built too, for smoke tests).
#
# The model: uv run --project harness python scripts/fetch_models.py --models hy-simulmt-30b-a3b-q4km-gguf
# Check:     .local/hy-simulmt/bin/hy-worker --model GGUF --test WAV --cuts 3 --from A --to B
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/env.sh"

REPO="https://github.com/lihy715/llama.cpp.Hyv3Speech.git"
COMMIT="e41ca4212f786815faa37606ee2e6fcf8611f9e4"
DIR="$ROOT/.local/hy-simulmt/llama.cpp"

mkdir -p "$(dirname "$DIR")"
if [ ! -d "$DIR/.git" ]; then
  git init -q "$DIR"
  git -C "$DIR" remote add origin "$REPO"
fi
if [ "$(git -C "$DIR" rev-parse HEAD 2>/dev/null || true)" != "$COMMIT" ]; then
  git -C "$DIR" fetch -q --depth 1 origin "$COMMIT"
  git -C "$DIR" checkout -q --detach FETCH_HEAD
fi
test "$(git -C "$DIR" rev-parse HEAD)" = "$COMMIT"

cmake -S "$DIR" -B "$DIR/build" -DCMAKE_BUILD_TYPE=Release -DGGML_METAL=ON \
  -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF
cmake --build "$DIR/build" --config Release -j "$(sysctl -n hw.perflevel0.physicalcpu)" \
  --target llama-mtmd-cli

mkdir -p "$ROOT/.local/hy-simulmt/bin"
clang++ -std=c++17 -O2 -Wall \
  -I"$DIR/include" -I"$DIR/ggml/include" -I"$DIR/tools/mtmd" -I"$DIR/vendor" \
  "$ROOT/research/hy_worker/hy_worker.cpp" \
  -L"$DIR/build/bin" -lllama -lmtmd -lggml -lggml-base -Wl,-rpath,"$DIR/build/bin" \
  -o "$ROOT/.local/hy-simulmt/bin/hy-worker"

echo "built $ROOT/.local/hy-simulmt/bin/hy-worker against $COMMIT"
