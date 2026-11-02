"""Write .local/live-dub/models/lock.json: observed revision and SHA-256 per downloaded file.

Licenses are not judged here; `license` stays "unverified" until someone records evidence.
"""
import hashlib
import json
import pathlib

ROOT = pathlib.Path("/Users/yangzhang/Documents/github/My/A2A/.local/live-dub")


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


entries = []
for snap in sorted((ROOT / "hf/hub").glob("models--*/snapshots/*")):
    repo = snap.parent.parent.name.removeprefix("models--").replace("--", "/")
    files = {str(p.relative_to(snap)): sha256(p.resolve()) for p in sorted(snap.rglob("*")) if p.is_file()}
    entries.append({"repo": repo, "revision": snap.name, "files": files, "license": "unverified", "research_only": True})
for p in sorted((ROOT / "models/onnx").glob("*.onnx")):
    entries.append({"url_basename": p.name, "files": {p.name: sha256(p)}, "license": "unverified", "research_only": True})
(ROOT / "models/lock.json").write_text(json.dumps(entries, indent=1))
print(f"locked {len(entries)} models")
