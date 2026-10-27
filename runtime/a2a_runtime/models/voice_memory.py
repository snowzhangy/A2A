"""Opt-in research voice references (ADR-0006). Port of
apple/Sources/A2AModels/VoiceMemoryStore.swift with the same on-disk format (binary plists
of the Swift Codable layout), so a store written on one platform loads on the other.

Kept in a private local directory outside diagnostics: 0700/0600 on POSIX; on Windows the
directory's ACL is reduced to the current user. At most 16 identities, two references each.
"""

from __future__ import annotations

import hashlib
import math
import os
import plistlib
import queue
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import numpy as np

from ..core.types import ReferenceQuality, SpeakerID, VoiceProfile
from ..media.resampler import StreamingResampler
from ..media.voiceprint import COEFFICIENTS, PITCH_BINS, Voiceprint, VoiceprintAnalyzer

MAXIMUM_VOICES = 16


def _valid_print(vp: Voiceprint) -> bool:
    return (len(vp.sum) == COEFFICIENTS and len(vp.pitch) == PITCH_BINS and math.isfinite(vp.weight)
            and 0 < vp.weight <= 10_000 and bool(np.all(np.isfinite(vp.sum)))
            and bool(np.all(np.isfinite(vp.pitch))) and bool(np.all(vp.pitch >= 0)))


class RememberedVoice:
    def __init__(self, id: str, person_id: str | None, reference_version: int | None, voiceprint: Voiceprint,
                 sample_rate: int, pcm: bytes, text: str) -> None:
        self.id = id
        self.person_id = person_id
        self.reference_version = reference_version
        self.voiceprint = voiceprint
        self.sample_rate = sample_rate
        self.pcm = pcm
        self.text = text

    @property
    def identity_id(self) -> str:
        return self.person_id or self.id

    @property
    def valid(self) -> bool:
        return (_valid_print(self.voiceprint) and 16_000 <= self.sample_rate <= 96_000 and len(self.pcm) % 4 == 0
                and self.sample_rate * 4 <= len(self.pcm) <= self.sample_rate * 4 * 15
                and bool(self.text) and len(self.text.encode("utf-8")) <= 32_768)

    def profile(self, speaker: SpeakerID) -> VoiceProfile | None:
        if not self.valid:
            return None
        samples = np.frombuffer(self.pcm, dtype="<f4").astype(np.float32)
        if not np.all(np.isfinite(samples)):
            return None
        return VoiceProfile(speaker, 1, samples, self.text, self.sample_rate, 1,
                            ReferenceQuality(len(samples) / self.sample_rate, float("nan"), 0, False))

    def encode(self) -> bytes:
        item = {"id": self.id, "voiceprint": {"sum": [float(x) for x in self.voiceprint.sum],
                                             "weight": float(self.voiceprint.weight),
                                             "pitch": [float(x) for x in self.voiceprint.pitch]},
                "sampleRate": self.sample_rate, "pcm": self.pcm, "text": self.text}
        if self.person_id:
            item["personID"] = self.person_id
        if self.reference_version is not None:
            item["referenceVersion"] = self.reference_version
        return plistlib.dumps(item, fmt=plistlib.FMT_BINARY)

    @staticmethod
    def decode(data: bytes) -> RememberedVoice:
        item = plistlib.loads(data)
        vp = Voiceprint()
        vp.sum = np.asarray(item["voiceprint"]["sum"], dtype=np.float64)
        vp.weight = float(item["voiceprint"]["weight"])
        vp.pitch = np.asarray(item["voiceprint"]["pitch"], dtype=np.float64)
        return RememberedVoice(str(item["id"]).upper(), str(item["personID"]).upper() if item.get("personID") else None,
                               item.get("referenceVersion"), vp, int(item["sampleRate"]), bytes(item["pcm"]),
                               str(item["text"]))


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("voice memory directory must not be a symbolic link")
    if sys.platform == "win32":
        user = os.environ.get("USERNAME")
        if user:
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F"],
                           capture_output=True, check=False)
    else:
        os.chmod(path, 0o700)


class VoiceMemoryStore:
    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        _private_directory(self.directory)
        loaded: list[RememberedVoice] = []
        files = sorted(p for p in self.directory.iterdir() if p.suffix == ".voice")[: MAXIMUM_VOICES * 2]
        for f in files:
            try:
                if f.is_symlink() or f.stat().st_size > 6_000_000:
                    continue
                item = RememberedVoice.decode(f.read_bytes())
            except Exception:
                continue
            if item.valid and item.id == f.stem.upper():
                loaded.append(item)
        identities = sorted({v.identity_id for v in loaded})[:MAXIMUM_VOICES]
        self._voices = [v for v in loaded if v.identity_id in identities]
        self._lock = threading.Lock()
        self._writes: queue.Queue = queue.Queue()
        self.save_failed = False
        self._writer = threading.Thread(target=self._write_loop, name="a2a.voice-memory", daemon=True)
        self._writer.start()

    @staticmethod
    def directory_for(root: str | Path, source: str) -> Path:
        return Path(root) / hashlib.sha256(source.encode("utf-8")).hexdigest()

    @property
    def count(self) -> int:
        with self._lock:
            return len({v.identity_id for v in self._voices})

    def match(self, vp: Voiceprint) -> RememberedVoice | None:
        """Reject weak and ambiguous matches; session-local speaker numbers never select a voice."""
        if not _valid_print(vp) or vp.voiced_frames < 40:
            return None
        with self._lock:
            candidates = []
            for item in self._voices:
                a, b = vp.median_pitch_hz, item.voiceprint.median_pitch_hz
                if a and b and abs(math.log2(a / b)) > 0.4:
                    continue
                candidates.append((item, item.voiceprint.distance(vp)))
        # Two references of one person are evidence, not competing identities.
        best_per_identity: dict[str, tuple] = {}
        for item, d in candidates:
            if item.identity_id not in best_per_identity or d < best_per_identity[item.identity_id][1]:
                best_per_identity[item.identity_id] = (item, d)
        ranked = sorted(best_per_identity.values(), key=lambda x: x[1])
        if not ranked or ranked[0][1] > 0.25 or (len(ranked) > 1 and ranked[1][1] - ranked[0][1] < 0.08):
            return None
        return ranked[0][0]

    def remember(self, profile: VoiceProfile, identity: str) -> None:
        pcm = np.asarray(profile.reference_pcm, dtype=np.float32)
        if (profile.channel_count != 1 or not 16_000 <= profile.sample_rate <= 96_000 or profile.version not in (1, 2)
                or len(pcm) > profile.sample_rate * 15 or not np.all(np.isfinite(pcm))
                or len(profile.reference_text.encode("utf-8")) > 32_768):
            return
        # The reference itself defines the saved print, rather than a possibly mixed cluster.
        vp = Voiceprint()
        VoiceprintAnalyzer().process(StreamingResampler.resample(pcm, profile.sample_rate, 16_000), vp)
        if not _valid_print(vp) or vp.voiced_frames < 40:
            return
        identity = identity.upper()
        item = RememberedVoice(identity if profile.version == 1 else str(uuid.uuid4()).upper(), identity,
                               profile.version, vp, profile.sample_rate, pcm.astype("<f4").tobytes(),
                               profile.reference_text)
        if item.valid:
            self._writes.put(item)

    def _write_loop(self) -> None:
        while True:
            item = self._writes.get()
            if item is None:
                self._writes.task_done()
                return
            try:
                path = self.directory / f"{item.id}.voice"
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(item.encode())
                os.replace(tmp, path)
                if sys.platform != "win32":
                    os.chmod(path, 0o600)
                with self._lock:
                    old = [v for v in self._voices if v.identity_id == item.identity_id
                           and v.reference_version == item.reference_version and v.id != item.id]
                    self._voices = [v for v in self._voices if not (v.identity_id == item.identity_id
                                                                    and v.reference_version == item.reference_version)]
                    self._voices.append(item)
                    while len({v.identity_id for v in self._voices}) > MAXIMUM_VOICES:
                        first = self._voices[0].identity_id
                        old += [v for v in self._voices if v.identity_id == first]
                        self._voices = [v for v in self._voices if v.identity_id != first]
                for v in old:
                    (self.directory / f"{v.id}.voice").unlink(missing_ok=True)
            except Exception:
                self.save_failed = True
            finally:
                self._writes.task_done()

    def flush(self) -> None:
        """Called during session teardown, after reference tasks have ended."""
        self._writes.join()
