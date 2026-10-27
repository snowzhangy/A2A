"""Experimental model catalog and explicit setup. No model imports or runtime downloads."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS = ("qwen", "omnivoice", "chatterbox")


def manifest() -> dict:
    import yaml
    return yaml.safe_load((ROOT / "models/manifest.yaml").read_text(encoding="utf-8"))


BACKENDS = ("mlx", "torch")


def selection(key: str, doc: dict | None = None, backend: str = "mlx") -> list[dict]:
    """Manifest entries for one experimental model on one worker backend: MLX on Apple
    silicon (the default), PyTorch for CUDA/MPS/CPU hosts (ADR-0007)."""
    if key not in KEYS:
        raise ValueError(f"unknown experimental model: {key}")
    if backend not in BACKENDS:
        raise ValueError(f"unknown worker backend: {backend}")
    doc = manifest() if doc is None else doc
    entries = {e["id"]: e for e in doc["entries"]}
    matches = [e for e in entries.values()
               if e.get("experimental_key") == key and e.get("worker_backend", "mlx") == backend]
    if len(matches) != 1:
        raise ValueError(f"expected one manifest entry for {key}")
    selected = matches[0]
    result = []
    seen = set()

    def visit(entry: dict) -> None:
        if entry["id"] in seen:
            return
        seen.add(entry["id"])
        if not entry.get("enabled") or entry.get("status") not in ("verified", "qualified"):
            raise ValueError(f"model is not enabled and verified: {entry['id']}")
        for dependency in entry.get("dependencies", []):
            visit(entries[dependency])
        result.append(entry)

    visit(selected)
    return result  # dependencies first, selected model last


def local_path(entry: dict, doc: dict) -> Path:
    return ROOT / doc["cache_dir"] / entry["id"] / entry["upstream"]["revision"]


def available_keys(doc: dict, backend: str = "mlx") -> list[str]:
    """Experimental keys that have a manifest entry for `backend`, in catalog order."""
    return [k for k in KEYS if any(e.get("experimental_key") == k and e.get("worker_backend", "mlx") == backend
                                   for e in doc["entries"])]


def installations(doc: dict | None = None, backend: str = "mlx") -> list[dict]:
    doc = manifest() if doc is None else doc
    result = []
    for key in KEYS if backend == "mlx" else available_keys(doc, backend):
        entries = selection(key, doc, backend)
        present = True
        for entry in entries:
            path = local_path(entry, doc)
            # Full SHA-256 verification happens before loading a model in the worker.
            for item in entry["files"]:
                file = path / item["path"]
                if not file.is_file() or file.stat().st_size != item["size"]:
                    present = False
        result.append(dict(key=key, installed=present,
                           bytes=sum(f["size"] for e in entries for f in e["files"])))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true")
    action.add_argument("--install", choices=KEYS)
    parser.add_argument("--backend", choices=BACKENDS, default="mlx",
                        help="worker backend: mlx (Apple silicon, default) or torch (CUDA/MPS/CPU)")
    args = parser.parse_args()
    doc = manifest()
    if args.install:
        # The explicit setup action is the only network-capable path in this module.
        sys.path.insert(0, str(ROOT))
        from scripts.fetch_models import fetch
        entries = selection(args.install, doc, args.backend)
        fetch(doc, [e["id"] for e in entries], ROOT / doc["cache_dir"],
              log=lambda message: print(message, file=sys.stderr, flush=True))
    print(json.dumps(installations(doc, args.backend)))


if __name__ == "__main__":
    main()
