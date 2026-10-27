#!/usr/bin/env bash
# Local development app with bundled tools; no external release or notarization.
# Builds optimized code by default: the helper's per-sample DSP (resampling, mixing feed)
# runs about 11x slower unoptimized. --debug builds a debuggable app instead.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/env.sh"
CONFIG=release
IDENTITY="-"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --release) CONFIG=release ;;
    --debug) CONFIG=debug ;;
    --sign) IDENTITY="$2"; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
swift build --package-path "$ROOT/apple" -c "$CONFIG" >&2
BIN="$(swift build --package-path "$ROOT/apple" -c "$CONFIG" --show-bin-path)"
APP="$ROOT/.local/apps/A2A.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Helpers"
cp "$BIN/A2A" "$APP/Contents/MacOS/A2A"
cp "$ROOT/apple/Apps/A2A/Info.plist" "$APP/Contents/Info.plist"
for tool in a2a-pipeline a2a-preflight; do
  cp "$BIN/$tool" "$APP/Contents/Helpers/$tool"
  codesign --force --sign "$IDENTITY" --options runtime --timestamp=none "$APP/Contents/Helpers/$tool" >&2
done
codesign --force --sign "$IDENTITY" --options runtime --timestamp=none "$APP" >&2
codesign --verify --deep --strict "$APP" >&2
echo "$APP"
