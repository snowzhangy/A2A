#!/usr/bin/env bash
# Optimized macOS development app for GitHub Releases; no weights or research data.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
source scripts/env.sh
[[ "$(uname -m)" == "arm64" ]] || { echo "Build the macOS release on Apple silicon." >&2; exit 1; }
TAG="${1:-}"
[[ "$TAG" =~ ^v[0-9][A-Za-z0-9._-]*$ ]] || { echo "usage: scripts/package_release.sh TAG" >&2; exit 2; }
[[ "$TAG" == "v$(tr -d '\r\n' < VERSION)" ]] || { echo "The tag must match VERSION." >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "Commit the source changes before packaging." >&2; exit 1; }
COMMIT="$(git rev-parse HEAD)"
[[ "$(git rev-parse "$TAG^{commit}")" == "$COMMIT" ]] || { echo "The release tag must point at HEAD." >&2; exit 1; }

OUT="$ROOT/.local/releases/$TAG"
mkdir -p "$OUT"
STAGE="$(mktemp -d "$OUT/.package.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
NAME="A2A-$TAG-macOS-arm64"
PAYLOAD="$STAGE/$NAME"
mkdir -p "$PAYLOAD"
scripts/build_app.sh --output "$PAYLOAD/A2A.app"
cp docs/releases/macos-development-build.md "$PAYLOAD/INSTALL.md"
uv run --project harness python - "$PAYLOAD/BUILD-INFO.json" "$TAG" "$COMMIT" "$A2A_SDK_VERSION" <<'PY'
import datetime
import json
from pathlib import Path
import sys

metadata = {
    "tag": sys.argv[2], "commit": sys.argv[3], "sdk": sys.argv[4],
    "platform": "macOS", "minimum_os": "26.0", "architecture": "arm64",
    "configuration": "release", "signing": "ad-hoc", "notarized": False,
    "requires_matching_checkout": True, "includes_models": False,
    "built_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
}
Path(sys.argv[1]).write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
PY
ARCHIVE="$OUT/$NAME.zip"
ditto -c -k --sequesterRsrc --keepParent "$PAYLOAD" "$ARCHIVE"
cd "$OUT"
shasum -a 256 "$NAME.zip" > SHA256SUMS
echo "$ARCHIVE"
