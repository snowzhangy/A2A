from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from a2a_harness import REPO_ROOT
from a2a_harness.synth import SynthConfig, generate


@pytest.fixture(scope="session")
def synthetic_fixture(tmp_path_factory) -> Path:
    return generate(tmp_path_factory.mktemp("fx"), SynthConfig(sentences=4, fixture_id="ci-zh"))


@pytest.fixture(scope="session")
def pipeline_binary() -> Path:
    """Path to a built a2a-pipeline; integration tests skip when it is unavailable."""
    env = os.environ.get("A2A_PIPELINE")
    if env and Path(env).exists():
        return Path(env)
    if shutil.which("swift"):
        try:
            bin_dir = subprocess.run(["swift", "build", "--package-path", str(REPO_ROOT / "apple"), "--show-bin-path"],
                                     capture_output=True, text=True, encoding="utf-8", timeout=60).stdout.strip()
            candidate = Path(bin_dir) / "a2a-pipeline"
            if candidate.exists():
                return candidate
        except (OSError, subprocess.SubprocessError):
            pass
    # The portable runtime's pipeline (ADR-0007), e.g. on Windows where Swift is not built.
    scripts = "Scripts" if os.name == "nt" else "bin"
    portable = REPO_ROOT / "runtime" / ".venv" / scripts / ("a2a-pipeline-portable" + (".exe" if os.name == "nt" else ""))
    if portable.exists():
        return portable
    pytest.skip("a2a-pipeline not built (run scripts/check.sh or set A2A_PIPELINE)")
