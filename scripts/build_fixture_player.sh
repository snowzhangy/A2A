#!/usr/bin/env bash
# Wrap the FixturePlayer binary in a signed .app with a stable bundle identifier
# (dev.a2a.FixturePlayer). Permission and audio-process identity follow the bundle,
# so harness tap tests always target the same app.
#
#   scripts/build_fixture_player.sh [--release] [--sign IDENTITY]   → .local/apps/FixturePlayer.app
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
source "$ROOT/scripts/env.sh"
CONFIG=debug
IDENTITY="-"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --release) CONFIG=release ;;
    --sign) IDENTITY="$2"; shift ;;
    *) echo "unknown argument $1" >&2; exit 2 ;;
  esac
  shift
done
swift build --package-path "$ROOT/apple" -c "$CONFIG" --product FixturePlayer >&2
BIN="$(swift build --package-path "$ROOT/apple" -c "$CONFIG" --show-bin-path)"
APP="$ROOT/.local/apps/FixturePlayer.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp "$BIN/FixturePlayer" "$APP/Contents/MacOS/FixturePlayer"
cp "$ROOT/apple/Apps/FixturePlayer/Info.plist" "$APP/Contents/Info.plist"
codesign --force --sign "$IDENTITY" --options runtime --timestamp=none "$APP" >&2
codesign --verify --strict "$APP" >&2
echo "$APP"
