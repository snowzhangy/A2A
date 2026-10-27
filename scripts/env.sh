# Source from other scripts: selects a full Xcode toolchain without touching xcode-select.
# The Command Line Tools SwiftPM on the reference Mac cannot link package manifests,
# and MLX/Metal targets need Xcode's metal compiler.
if [[ -z "${DEVELOPER_DIR:-}" ]]; then
  if [[ "$(xcode-select -p 2>/dev/null)" == *CommandLineTools* ]]; then
    for candidate in /Applications/Xcode.app /Applications/Xcode-*.app /Applications/Xcode-beta.app; do
      if [[ -d "$candidate/Contents/Developer" ]]; then
        export DEVELOPER_DIR="$candidate/Contents/Developer"
        break
      fi
    done
  fi
fi
# The SDK version to record in every executable (see sdkVersionStamp() in apple/Package.swift).
if [[ -z "${A2A_SDK_VERSION:-}" ]]; then
  A2A_SDK_VERSION="$(xcrun --sdk macosx --show-sdk-version 2>/dev/null || true)"
  export A2A_SDK_VERSION
fi
