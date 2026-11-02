"""Resolve explicit, verified model installations without contacting a registry."""

from pathlib import Path

import yaml

from .. import REPO_ROOT
from ..models.base import AdapterError


def installed_model(model_id: str, stage: str) -> Path:
    doc = yaml.safe_load((REPO_ROOT / "models" / "manifest.yaml").read_text(encoding="utf-8"))
    entry = next((e for e in doc["entries"] if e["id"] == model_id), None)
    if not entry or entry.get("stage") != stage or not entry.get("enabled") or entry.get("status") not in ("verified", "qualified"):
        raise AdapterError(f"model {model_id!r} is not an enabled, verified {stage} model")
    path = REPO_ROOT / doc["cache_dir"] / entry["id"] / entry["upstream"]["revision"]
    for item in entry.get("files", []):
        file = path / item["path"]
        if not file.is_file() or file.stat().st_size != item["size"]:
            raise AdapterError(f"model {model_id!r} is not installed; explicitly fetch it with scripts/fetch_models.py --models {model_id}")
    return path
