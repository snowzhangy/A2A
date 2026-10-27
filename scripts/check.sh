#!/usr/bin/env bash
# Canonical local/CI check (T0.1). Deterministic: no capture permission, devices, network
# model downloads, or listeners required. Hardware/TCC/model tests run on the interactive
# Mac via a2a-tapctl, a2a-preflight, and harness/run.py (see AGENTS.md).
#
#   scripts/check.sh            build + Swift tests + manifest gate + harness tests
#   scripts/check.sh --quick    skip the harness ↔ pipeline integration tests
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
source scripts/env.sh

QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

step() { printf '\n== %s\n' "$*"; }

step "toolchain"
echo "xcode-select: $(xcode-select -p 2>/dev/null || echo none)"
echo "DEVELOPER_DIR: ${DEVELOPER_DIR:-<unset>}"
swift --version 2>&1
echo "SDK: $(xcrun --show-sdk-version 2>/dev/null) ($(xcrun --show-sdk-build-version 2>/dev/null))"
uv --version

step "swift build"
swift build --package-path apple

step "executables record the current SDK"
# Swift Build stamps the deployment target as the SDK version unless Package.swift's
# sdkVersionStamp() overrides it, and Translation then reports installed languages as
# missing (reports/week2/verifications.md).
BIN="$(swift build --package-path apple --show-bin-path)"
want="$(xcrun --sdk macosx --show-sdk-version)"
for exe in a2a-tapctl a2a-pipeline a2a-preflight a2a-transcribe a2a-stage-probe FixturePlayer A2A; do
  # Drain otool completely: an early awk exit can SIGPIPE it under pipefail.
  got="$(otool -l "$BIN/$exe" | awk '/LC_BUILD_VERSION/ {f=1} f && $1 == "sdk" && !printed {print $2; printed=1}')"
  echo "$exe: sdk $got"
  [[ "$got" == "$want" ]] || { echo "$exe records SDK $got, expected $want (build through scripts/env.sh)" >&2; exit 1; }
done

step "swift test"
log="$(mktemp)"
trap 'rm -f "$log"' EXIT
test_status=0
swift test --package-path apple > "$log" 2>&1 || test_status=$?
rg '✘|error:|Test run with|skipped' "$log" || true
if [[ "$test_status" -ne 0 ]] || rg -q '✘|error:' "$log" || ! rg -q 'passed' "$log"; then
  tail -n 100 "$log" >&2
  echo "swift test failed" >&2
  exit 1
fi
# An empty suite is not a pass (T0.1 DoD).
reported=$(grep -Eo "Test run with [0-9]+ tests" "$log" | awk '{s+=$4} END {print s+0}')
skipped=$(awk '/^➜ Test .* skipped\./ {n++} END {print n+0}' "$log")
tests=$((reported - skipped))
echo "swift tests executed: $tests"
[[ "$tests" -ge 50 ]] || { echo "too few Swift tests ran ($tests)" >&2; exit 1; }

step "model manifest gate"
uv run --project harness python scripts/fetch_models.py --check

step "harness tests"
export A2A_PIPELINE="$(swift build --package-path apple --show-bin-path)/a2a-pipeline"
if [[ $QUICK == 1 ]]; then
  uv run --project harness pytest harness/tests -q --ignore "$ROOT/harness/tests/test_runner.py"
else
  uv run --project harness pytest harness/tests -q
fi

step "ok"
