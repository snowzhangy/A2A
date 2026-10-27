"""T0.2 DoD: clean cache reproduces the selection; corrupt/incomplete artifacts are rejected;
disabled/restricted candidates are never fetched; enabled rows need provenance."""

import copy
import hashlib
import http.server
import importlib.util
import threading

import pytest

from a2a_harness import REPO_ROOT

spec = importlib.util.spec_from_file_location("fetch_models", REPO_ROOT / "scripts" / "fetch_models.py")
fm = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fm)

PAYLOAD = bytes(range(256)) * 400   # 102,400 bytes
SHA = hashlib.sha256(PAYLOAD).hexdigest()
REV = "0123456789abcdef0123456789abcdef01234567"


class RangeHandler(http.server.BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}
    ranges: list[str] = []

    def do_GET(self):  # noqa: N802
        body = self.files.get(self.path)
        if body is None:
            self.send_error(404)
            return
        rng = self.headers.get("Range")
        if rng:
            self.ranges.append(rng)
            start = int(rng.split("=")[1].split("-")[0])
            chunk = body[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(body) - 1}/{len(body)}")
        else:
            chunk = body
            self.send_response(200)
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    RangeHandler.files = {"/good.bin": PAYLOAD, "/corrupt.bin": PAYLOAD[:-1] + b"\x00"}
    RangeHandler.ranges = []
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def manifest(base: str, name: str = "good.bin", **overrides) -> dict:
    entry = {
        "id": "tiny-model", "stage": "test", "status": "verified", "enabled": True,
        "upstream": {"repo": "https://example.invalid/tiny", "revision": REV, "license": "MIT",
                     "license_evidence": "https://example.invalid/tiny/LICENSE"},
        "converted": {"repo": None},
        "verified_by": "test", "verified_on": "2026-10-24",
        "files": [{"path": "weights/model.bin", "url": f"{base}/{name}", "sha256": SHA, "size": len(PAYLOAD)}],
    }
    entry.update(overrides)
    return {"schema": 1, "entries": [entry]}


def test_repository_manifest_passes_check():
    doc = fm.load_manifest(REPO_ROOT / "models" / "manifest.yaml")
    assert fm.check(doc) == []
    hy = next(e for e in doc["entries"] if e["id"] == "hy-mt1.5")
    assert hy["restricted"] and not hy["enabled"]
    # Enabled research candidates are allowed once the provenance gate above passes.


def test_fetch_verifies_and_promotes_atomically(server, tmp_path):
    out = fm.fetch(manifest(server), ["tiny-model"], tmp_path, log=lambda *_: None)
    final = tmp_path / "tiny-model" / REV
    assert out == [final]
    assert (final / "weights/model.bin").read_bytes() == PAYLOAD
    assert not (tmp_path / ".partial" / "tiny-model").exists()
    # Second run is a verified no-op.
    assert fm.fetch(manifest(server), ["tiny-model"], tmp_path, log=lambda *_: None) == [final]


def test_corrupt_artifact_is_rejected_and_not_promoted(server, tmp_path):
    with pytest.raises(fm.ManifestError, match="verification failed"):
        fm.fetch(manifest(server, "corrupt.bin"), ["tiny-model"], tmp_path, log=lambda *_: None)
    assert not (tmp_path / "tiny-model").exists()
    assert not list((tmp_path / ".partial").rglob("*.part"))


def test_interrupted_download_resumes_with_range(server, tmp_path):
    part = tmp_path / ".partial" / "tiny-model" / REV / "weights/model.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(PAYLOAD[:40_000])
    fm.fetch(manifest(server), ["tiny-model"], tmp_path, log=lambda *_: None)
    assert RangeHandler.ranges == ["bytes=40000-"]
    assert (tmp_path / "tiny-model" / REV / "weights/model.bin").read_bytes() == PAYLOAD


def test_disabled_and_restricted_entries_are_never_fetched(server, tmp_path):
    with pytest.raises(fm.ManifestError, match="disabled"):
        fm.fetch(manifest(server, enabled=False), ["tiny-model"], tmp_path)
    with pytest.raises(fm.ManifestError, match="restricted"):
        fm.fetch(manifest(server, restricted=True, license_determination="ok for internal benchmark"),
                 ["tiny-model"], tmp_path)
    assert not (tmp_path / "tiny-model").exists()


def test_check_rejects_incomplete_enabled_rows(server):
    doc = manifest(server)
    bad = copy.deepcopy(doc)
    bad["entries"][0].update(status="declared", verified_by=None)
    bad["entries"][0]["upstream"]["revision"] = "main"
    bad["entries"][0]["files"][0]["sha256"] = None
    problems = "\n".join(fm.check(bad))
    for needle in ("only declared", "40-hex", "verified_by", "sha256"):
        assert needle in problems
    assert fm.check(doc) == []


def test_notices_list_enabled_models_only(server):
    text = fm.notices(manifest(server))
    assert "tiny-model" in text and "MIT" in text
