"""Model manifest check, fetch, and notices (T0.2).

    uv run --project harness python scripts/fetch_models.py --check              # CI license/provenance gate
    uv run --project harness python scripts/fetch_models.py --list
    uv run --project harness python scripts/fetch_models.py --models ID[,ID]     # explicit, resumable, verified
    uv run --project harness python scripts/fetch_models.py --notices            # THIRD_PARTY_NOTICES from manifest

Downloads resume from `<cache>/.partial`, verify size and SHA-256 per file, and promote a
model directory atomically into `<cache>/<id>/<revision>` only after every file passes.
Only enabled entries named on the command line are fetched; restricted entries also need a
recorded license determination and --allow-restricted. Runtime code never downloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
STATUSES = {"declared", "verified", "qualified"}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class ManifestError(Exception):
    pass


def load_manifest(path: Path) -> dict:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != 1:
        raise ManifestError(f"{path}: expected schema: 1")
    return doc


def check(doc: dict) -> list[str]:
    """Return every provenance/license problem. Empty means the manifest passes."""
    problems: list[str] = []
    seen: set[str] = set()
    for e in doc.get("entries", []):
        mid = e.get("id", "<missing id>")
        if mid in seen:
            problems.append(f"{mid}: duplicate id")
        seen.add(mid)
        for key in ("stage", "status", "enabled", "upstream", "files"):
            if key not in e:
                problems.append(f"{mid}: missing {key}")
        if e.get("status") not in STATUSES:
            problems.append(f"{mid}: status must be one of {sorted(STATUSES)}")
        up = e.get("upstream") or {}
        if not up.get("license"):
            problems.append(f"{mid}: upstream license must be named (even if unverified)")
        if e.get("restricted") and e.get("enabled") and not e.get("license_determination"):
            problems.append(f"{mid}: restricted entry enabled without a recorded license determination")
        if "hy-mt" in mid and not e.get("restricted"):
            problems.append(f"{mid}: HY-MT must stay restricted")
        if not e.get("enabled"):
            continue
        # Enabled entries need complete provenance.
        if e.get("status") == "declared":
            problems.append(f"{mid}: enabled but only declared (needs verified/qualified)")
        if not HEX40.match(str(up.get("revision") or "")):
            problems.append(f"{mid}: enabled without a pinned 40-hex upstream revision")
        for key in ("license_evidence",):
            if not up.get(key):
                problems.append(f"{mid}: enabled without upstream {key}")
        conv = e.get("converted") or {}
        if conv.get("repo") and (not HEX40.match(str(conv.get("revision") or "")) or not conv.get("license")):
            problems.append(f"{mid}: converted artifact needs a pinned revision and license")
        for key in ("verified_by", "verified_on"):
            if not e.get(key):
                problems.append(f"{mid}: enabled without {key}")
        if not e.get("files"):
            problems.append(f"{mid}: enabled without files")
        for f in e.get("files", []):
            name = f.get("path", "<file>")
            if not f.get("url"):
                problems.append(f"{mid}/{name}: missing url")
            if not HEX64.match(str(f.get("sha256") or "")):
                problems.append(f"{mid}/{name}: missing or malformed sha256")
            if not isinstance(f.get("size"), int) or f["size"] <= 0:
                problems.append(f"{mid}/{name}: missing size")
            if ".." in Path(name).parts or Path(name).is_absolute():
                problems.append(f"{mid}/{name}: path must stay inside the model directory")
    return problems


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _download(url: str, part: Path, size: int, *, timeout: float = 60) -> None:
    part.parent.mkdir(parents=True, exist_ok=True)
    have = part.stat().st_size if part.exists() else 0
    if have > size:
        part.unlink()
        have = 0
    if have == size:
        return
    req = urllib.request.Request(url, headers={"Range": f"bytes={have}-"} if have else {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        mode = "ab" if have and status == 206 else "wb"
        with open(part, mode) as fh:
            shutil.copyfileobj(resp, fh, length=1 << 20)


def fetch(doc: dict, ids: list[str], cache: Path, *, allow_restricted: bool = False, log=print) -> list[Path]:
    entries = {e["id"]: e for e in doc.get("entries", [])}
    problems = check(doc)
    promoted: list[Path] = []
    for mid in ids:
        e = entries.get(mid)
        if e is None:
            raise ManifestError(f"{mid}: not in manifest")
        if not e.get("enabled"):
            raise ManifestError(f"{mid}: disabled entries are never fetched")
        if e.get("restricted") and not (allow_restricted and e.get("license_determination")):
            raise ManifestError(f"{mid}: restricted; needs a license determination and --allow-restricted")
        mine = [p for p in problems if p.startswith(f"{mid}:") or p.startswith(f"{mid}/")]
        if mine:
            raise ManifestError("; ".join(mine))
        rev = e["upstream"]["revision"]
        final = cache / mid / rev
        if final.exists() and all(_sha256(final / f["path"]) == f["sha256"] for f in e["files"]):
            log(f"{mid}@{rev}: already present and verified")
            promoted.append(final)
            continue
        partial_root = cache / ".partial" / mid / rev
        for f in e["files"]:
            part = partial_root / (f["path"] + ".part")
            log(f"{mid}: {f['path']} ({f['size']} bytes)")
            _download(f["url"], part, f["size"])
            actual_size = part.stat().st_size
            digest = _sha256(part)
            if actual_size != f["size"] or digest != f["sha256"]:
                part.unlink(missing_ok=True)
                raise ManifestError(f"{mid}/{f['path']}: verification failed "
                                    f"(size {actual_size} vs {f['size']}, sha256 {digest[:12]}… vs {f['sha256'][:12]}…)")
        staging = cache / ".staging" / f"{mid}@{rev}"
        if staging.exists():
            shutil.rmtree(staging)
        for f in e["files"]:
            dest = staging / f["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(partial_root / (f["path"] + ".part"), dest)
        (staging / ".a2a-manifest-entry.json").write_text(json.dumps(e, indent=2, ensure_ascii=False), encoding="utf-8")
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            shutil.rmtree(final)
        os.replace(staging, final)   # atomic on the same filesystem
        shutil.rmtree(partial_root, ignore_errors=True)
        for empty in (partial_root.parent, staging.parent):
            try:
                empty.rmdir()
            except OSError:
                pass
        log(f"{mid}@{rev}: verified and promoted to {final}")
        promoted.append(final)
    return promoted


def notices(doc: dict) -> str:
    lines = ["# Third-party model notices", "", "Generated from models/manifest.yaml. Enabled entries only.", ""]
    enabled = [e for e in doc.get("entries", []) if e.get("enabled")]
    if not enabled:
        lines.append("_No models are enabled yet._")
    for e in enabled:
        up = e["upstream"]
        lines += [f"## {e['id']}", "", f"- Upstream: {up['repo']} @ {up.get('revision')}", f"- License: {up['license']}",
                  f"- Evidence: {up.get('license_evidence')}"]
        conv = e.get("converted") or {}
        if conv.get("repo"):
            lines.append(f"- Converted: {conv['repo']} @ {conv.get('revision')} ({conv.get('license')})")
        if e.get("pass_on_use_restrictions"):
            lines.append("- Use restrictions in the upstream terms must be passed on to users.")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", type=Path, default=ROOT / "models" / "manifest.yaml")
    ap.add_argument("--cache", type=Path)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--notices", action="store_true")
    ap.add_argument("--models", help="comma-separated ids to fetch")
    ap.add_argument("--allow-restricted", action="store_true")
    args = ap.parse_args(argv)
    doc = load_manifest(args.manifest)
    cache = args.cache or ROOT / doc.get("cache_dir", ".local/models")
    if args.check:
        problems = check(doc)
        for p in problems:
            print(f"manifest: {p}", file=sys.stderr)
        enabled = sum(1 for e in doc.get("entries", []) if e.get("enabled"))
        print(f"manifest: {len(doc.get('entries', []))} entries, {enabled} enabled, {len(problems)} problems")
        return 1 if problems else 0
    if args.list:
        for e in doc.get("entries", []):
            flag = "enabled" if e.get("enabled") else "disabled"
            extra = " RESTRICTED" if e.get("restricted") else ""
            print(f"{e['id']:52s} {e['stage']:18s} {e['status']:9s} {flag}{extra}")
        return 0
    if args.notices:
        print(notices(doc))
        return 0
    if args.models:
        try:
            fetch(doc, [m.strip() for m in args.models.split(",") if m.strip()], cache,
                  allow_restricted=args.allow_restricted)
        except ManifestError as exc:
            print(f"fetch: {exc}", file=sys.stderr)
            return 1
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
