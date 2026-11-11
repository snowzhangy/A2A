"""Package the experimental Windows desktop runtime and its compiled WASAPI library.

Run on Windows from a clean checkout, after runtime/native/build.py and deterministic checks:
    uv run --project harness python scripts/package_windows_release.py v0.2.0-alpha.2

Only tracked source files and the explicitly selected DLL enter the archive. Models, virtual
environments, recordings, traces, saved voices and machine preferences are never enumerated.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8").strip()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tag")
    args = ap.parse_args()
    if sys.platform != "win32":
        ap.error("build and package the Windows library on Windows")
    if not re.fullmatch(r"v[0-9][A-Za-z0-9._-]*", args.tag):
        ap.error("invalid release tag")
    if git("status", "--porcelain"):
        ap.error("commit the source changes before packaging")
    commit = git("rev-parse", "HEAD")
    dll = ROOT / ".local" / "native" / "windows" / "a2a_rt.dll"
    if not dll.is_file():
        ap.error("run runtime/native/build.py first")
    # Refuse an accidentally copied library for another platform/architecture.
    blob = dll.read_bytes()
    pe_offset = int.from_bytes(blob[60:64], "little")
    if (blob[:2] != b"MZ" or blob[pe_offset:pe_offset + 4] != b"PE\0\0"
            or int.from_bytes(blob[pe_offset + 4:pe_offset + 6], "little") != 0x8664):
        ap.error("the Windows release requires an x64 PE library")
    name = f"A2A-{args.tag}-Windows11-x64"
    out = ROOT / ".local" / "releases" / args.tag
    out.mkdir(parents=True, exist_ok=True)
    archive = out / f"{name}.zip"
    metadata = {
        "tag": args.tag, "commit": commit, "platform": "Windows 11", "architecture": "x64",
        "configuration": "release", "runtime": "experimental portable Python runtime",
        "requires_python": "3.12 or 3.13", "includes_models": False,
        "native_sha256": hashlib.sha256(blob).hexdigest(),
        "built_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for relative in git("ls-files", "-z").split("\0"):
            if relative:
                bundle.write(ROOT / relative, f"{name}/{relative}")
        bundle.write(dll, f"{name}/.local/native/windows/a2a_rt.dll")
        bundle.write(ROOT / "docs" / "releases" / "windows-development-build.md", f"{name}/INSTALL-WINDOWS.md")
        bundle.writestr(f"{name}/BUILD-INFO.json", json.dumps(metadata, indent=2) + "\n")
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (out / "SHA256SUMS-Windows.txt").write_text(f"{checksum}  {archive.name}\n", encoding="utf-8")
    print(archive)


if __name__ == "__main__":
    main()
