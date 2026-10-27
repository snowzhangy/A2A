"""A2A replay harness: fixtures, paced replay, trace validation, and metrics."""

from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = HARNESS_ROOT.parent
SCHEMA_PATH = HARNESS_ROOT / "schema" / "trace.schema.json"
CANONICAL_RATE = 48_000
