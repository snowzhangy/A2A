"""Derived model artifacts: local conversions of verified manifest entries (ADR-0007).

    uv run --project runtime python -m a2a_runtime.models.derive [qwen3-4b-instruct-2507-ct2] [--check]

A derived artifact is built only from an enabled, verified manifest entry whose files pass
SHA-256 here, so provenance is the pinned source plus the converter and its options. The
output directory carries `.a2a-derived.json` with the source revision, tool version, options,
and every output file's size and SHA-256; loaders check that stamp against the manifest
(`check_stamp`) and refuse an artifact from another source revision. Conversion is local
and offline; nothing is downloaded.

Why: eager PyTorch decoding on Windows is launch-bound (each of ~2,600 kernel launches per
Qwen3-4B token costs ~25 µs under WDDM: 112 ms/token for 12.7 ms of GPU work), while
CTranslate2's fused kernels translate a clause in 0.16-0.26 s on an RTX 4090.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from .. import REPO_ROOT

STAMP = ".a2a-derived.json"
DERIVED_ROOT = REPO_ROOT / ".local" / "derived"


def _manifest() -> dict:
    return yaml.safe_load((REPO_ROOT / "models" / "manifest.yaml").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 22), b""):
            h.update(block)
    return h.hexdigest()


def spec(derived_id: str, doc: dict | None = None) -> dict:
    doc = _manifest() if doc is None else doc
    for d in doc.get("derived", []):
        if d["id"] == derived_id:
            return d
    raise KeyError(f"{derived_id}: not a derived artifact in models/manifest.yaml")


def for_source(source_id: str, tool: str, doc: dict | None = None, quantization: str | None = None) -> dict | None:
    """The derived artifact of `source_id` made with `tool`; with `quantization`, the one
    converted with exactly that `--quantization` option (otherwise the first listed)."""
    doc = _manifest() if doc is None else doc
    for d in doc.get("derived", []):
        if d["from"] != source_id or d["tool"] != tool:
            continue
        options = [str(x) for x in d.get("options", [])]
        stored = options[options.index("--quantization") + 1] if "--quantization" in options[:-1] else None
        if quantization is None or stored == quantization:
            return d
    return None


def source_entry(d: dict, doc: dict) -> dict:
    entry = next((e for e in doc["entries"] if e["id"] == d["from"]), None)
    if entry is None or not entry.get("enabled") or entry.get("status") not in ("verified", "qualified"):
        raise ValueError(f"{d['id']}: source {d['from']} is not an enabled, verified manifest entry")
    return entry


def source_path(entry: dict, doc: dict) -> Path:
    return REPO_ROOT / doc["cache_dir"] / entry["id"] / entry["upstream"]["revision"]


def output_path(d: dict, entry: dict) -> Path:
    return DERIVED_ROOT / d["id"] / entry["upstream"]["revision"]


def check_stamp(d: dict, doc: dict | None = None, full: bool = False) -> Path:
    """The derived directory for this manifest's source revision, verified by size (and by
    SHA-256 with `full`) against its stamp. Raises when missing, stale, or modified."""
    doc = _manifest() if doc is None else doc
    entry = source_entry(d, doc)
    out = output_path(d, entry)
    try:
        stamp = json.loads((out / STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise FileNotFoundError(f"{d['id']} is not converted; run: uv run --project runtime python -m "
                                f"a2a_runtime.models.derive {d['id']}") from None
    if stamp.get("source") != {"id": entry["id"], "revision": entry["upstream"]["revision"]}:
        raise ValueError(f"{d['id']}: converted from a different source revision; convert again")
    if stamp.get("options") != d.get("options"):
        raise ValueError(f"{d['id']}: converted with different options; convert again")
    for f in stamp["files"]:
        path = out / f["path"]
        if not path.is_file() or path.stat().st_size != f["size"] or (full and _sha256(path) != f["sha256"]):
            raise ValueError(f"{d['id']}/{f['path']}: does not match its conversion stamp; convert again")
    return out


def convert(derived_id: str, log=print) -> Path:
    doc = _manifest()
    d = spec(derived_id, doc)
    entry = source_entry(d, doc)
    src = source_path(entry, doc)
    for f in entry["files"]:  # the conversion input must be exactly the pinned artifact
        p = src / f["path"]
        if not p.is_file() or p.stat().st_size != f["size"] or _sha256(p) != f["sha256"]:
            raise ValueError(f"{entry['id']}/{f['path']}: missing or fails SHA-256; fetch it first")
    out = output_path(d, entry)
    staging = out.with_name(out.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    if d["tool"] != "ctranslate2":
        raise ValueError(f"{d['id']}: unknown tool {d['tool']}")
    import ctranslate2

    version = ctranslate2.__version__
    converter = Path(sys.executable).parent / ("ct2-transformers-converter" + (".exe" if os.name == "nt" else ""))
    cmd = [str(converter), "--model", str(src), "--output_dir", str(staging), "--low_cpu_mem_usage",
           *[str(x) for x in d["options"]]]
    log(f"converting {entry['id']}@{entry['upstream']['revision'][:12]} with ctranslate2 {version}")
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
    subprocess.run(cmd, check=True, env=env)
    files = sorted(p for p in staging.rglob("*") if p.is_file())
    stamp = {"id": d["id"], "source": {"id": entry["id"], "revision": entry["upstream"]["revision"]},
             "tool": {"name": "ctranslate2", "version": version}, "options": d["options"],
             "files": [{"path": p.relative_to(staging).as_posix(), "size": p.stat().st_size, "sha256": _sha256(p)}
                       for p in files]}
    (staging / STAMP).write_text(json.dumps(stamp, indent=1), encoding="utf-8")
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(out, ignore_errors=True)
    os.replace(staging, out)
    log(f"{d['id']}: converted to {out}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ids", nargs="*", help="derived artifact ids (default: all)")
    ap.add_argument("--check", action="store_true", help="verify stamps (full SHA-256) instead of converting")
    args = ap.parse_args()
    doc = _manifest()
    ids = args.ids or [d["id"] for d in doc.get("derived", [])]
    for i in ids:
        if args.check:
            print(f"{i}: {check_stamp(spec(i, doc), doc, full=True)} ok")
        else:
            convert(i)


if __name__ == "__main__":
    main()
