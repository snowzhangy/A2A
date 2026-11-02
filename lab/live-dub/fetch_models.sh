#!/bin/zsh
# Research-only model fetch for the live-dub lab. Records the resolved revision and SHA-256 of
# every file actually downloaded in .local/live-dub/models/lock.json. Not the product manifest.
set -eu
ROOT=/Users/yangzhang/Documents/github/My/A2A/.local/live-dub
export HF_HOME=$ROOT/hf
PY=$ROOT/venv/bin/python
for repo in "$@"; do
  case $repo in
    https://*)
      out=$ROOT/models/onnx/${repo:t}
      [[ -s $out ]] || curl -fsSL --retry 3 -o $out.part "$repo" && mv $out.part $out 2>/dev/null || true
      ;;
    *)
      $PY -c "import sys; from huggingface_hub import snapshot_download; print(snapshot_download(sys.argv[1]))" "$repo"
      ;;
  esac
done
$PY /Users/yangzhang/Documents/github/My/A2A/lab/live-dub/lock_models.py
