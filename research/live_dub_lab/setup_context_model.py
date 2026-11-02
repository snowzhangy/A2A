"""Explicit local setup: register and copy an already cached contextual MT checkpoint.

No downloads. Provenance pins and licenses were checked against the primary model cards
by Codex on 2026-10-27; file hashes below are computed from the inspected local bytes.
"""
import argparse
import hashlib
import os
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
ID = "qwen3-4b-instruct-2507-mlx-4bit"
UP = "cdbee75f17c01a7cc42f958dc650907174af0554"
REV = "50d427756c6b1b2fe0c0a10f67fbda1fc8e82c1b"
REPO = "https://huggingface.co/mlx-community/Qwen3-4B-Instruct-2507-4bit"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=("4b", "30b"), default="4b")
    args = parser.parse_args()
    mid, up, rev, repo, original_repo = ID, UP, REV, REPO, "Qwen/Qwen3-4B-Instruct-2507"
    if args.model == "30b":
        mid = "qwen3-30b-a3b-instruct-2507-mlx-4bit"
        up = "0d7cf23991f47feeb3a57ecb4c9cee8ea4a17bfe"
        rev = "e9675aa3ca5f900ccef55267914466d55ab325fa"
        repo = "https://huggingface.co/mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit"
        original_repo = "Qwen/Qwen3-30B-A3B-Instruct-2507"
    cache_name = repo.split("huggingface.co/")[1].replace("/", "--")
    source = ROOT / f".local/live-dub/hf/hub/models--{cache_name}/snapshots/{rev}"
    manifest = ROOT / "models/manifest.yaml"
    doc = yaml.safe_load(manifest.read_text())
    if any(e["id"] == mid for e in doc["entries"]):
        raise ValueError("model is already registered")
    files = []
    for path in sorted(source.iterdir()):
        if path.name.startswith(".") or not path.is_file():
            continue
        with path.open("rb") as data:
            digest = hashlib.file_digest(data, "sha256").hexdigest()
        files.append(dict(path=path.name, url=f"{repo}/resolve/{rev}/{path.name}",
                          size=path.stat().st_size, sha256=digest))
    if not any(f["path"].endswith(".safetensors") for f in files):
        raise ValueError("local checkpoint is incomplete")
    entry = dict(id=mid, stage="mt", status="verified", enabled=True, verified_by="Codex",
        verified_on="2026-10-27", research_only=True,
        upstream=dict(repo=f"https://huggingface.co/{original_repo}", revision=up,
            license="Apache-2.0", license_evidence=f"https://huggingface.co/{original_repo}/blob/{up}/README.md"),
        converted=dict(repo=repo, revision=rev, license="Apache-2.0",
            license_evidence=f"{repo}/blob/{rev}/README.md",
            note="Conversion card names upstream but does not identify its revision; numerical parity unqualified."),
        runtime="mlx-lm 0.32.2; research lab only, explicit local directory; no runtime downloads",
        backend="GPU (MLX)", open_question="Causal translation accuracy on actual ASR and contention with live TTS",
        files=files)
    target = ROOT / doc["cache_dir"] / mid / up
    if target.exists():
        raise FileExistsError(target)
    target.mkdir(parents=True)
    for f in files:
        # A hard link survives removal of the cache's name without duplicating large
        # immutable weights. Fall back to copying if the directories span volumes.
        try:
            os.link((source / f["path"]).resolve(), target / f["path"])
        except OSError:
            shutil.copy2(source / f["path"], target / f["path"])
    fragment = yaml.safe_dump([entry], sort_keys=False, allow_unicode=True)
    insertion = "\n".join("  " + line for line in fragment.splitlines()) + "\n\n"
    original = manifest.read_text()
    marker = "# System frameworks:"
    if original.count(marker) != 1:
        raise ValueError("unexpected manifest structure")
    manifest.write_text(original.replace(marker, insertion + marker))
    print(f"Registered {mid}: {len(files)} locally hashed artifacts; no downloads")


if __name__ == "__main__":
    main()
