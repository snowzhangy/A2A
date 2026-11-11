"""A2A cross-platform runtime (ADR-0007): the portable policy core, media DSP, model
adapters, the native mixer binding, and the pipeline driver for Windows and macOS."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def release_version() -> str:
    """The source bundle's release identity, independent of the Python package version."""
    try:
        return (REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "development"
