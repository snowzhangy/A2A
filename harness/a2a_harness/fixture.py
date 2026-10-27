"""Fixture manifests: audio, reference alignment, scripted ASR, and MT dictionary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RefChar:
    char: str
    start: float  # seconds into the fixture audio
    end: float


@dataclass(frozen=True)
class RefUnit:
    text: str
    translation: str | None
    chars: tuple[RefChar, ...]


@dataclass(frozen=True)
class Fixture:
    path: Path
    id: str
    kind: str
    license: str
    audio: Path
    sample_rate: int
    duration_s: float
    warm_interval_s: tuple[float, float]
    units: tuple[RefUnit, ...]
    asr_script: Path | None
    mt_dictionary: Path | None
    alignment_uncertainty_ms: float

    @property
    def ref_chars(self) -> list[RefChar]:
        return [c for u in self.units for c in u.chars]


def load(path: Path | str) -> Fixture:
    path = Path(path).resolve()
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    base = path.parent

    def rel(key: str) -> Path | None:
        value = doc.get(key)
        return (base / value).resolve() if value else None

    units = tuple(
        RefUnit(
            text=u["text"],
            translation=u.get("translation"),
            chars=tuple(RefChar(c["c"], float(c["start"]), float(c["end"])) for c in u.get("chars", [])),
        )
        for u in doc.get("reference", {}).get("units", [])
    )
    warm = doc.get("warm_interval_s") or [0.0, float(doc["duration_s"])]
    for required in ("id", "kind", "license", "audio", "sample_rate", "duration_s"):
        if required not in doc:
            raise ValueError(f"{path}: missing {required}")
    if doc["kind"] not in {"synthetic", "licensed", "consented"}:
        raise ValueError(f"{path}: kind must be synthetic, licensed, or consented")
    return Fixture(
        path=path,
        id=str(doc["id"]),
        kind=doc["kind"],
        license=doc["license"],
        audio=rel("audio"),
        sample_rate=int(doc["sample_rate"]),
        duration_s=float(doc["duration_s"]),
        warm_interval_s=(float(warm[0]), float(warm[1])),
        units=units,
        asr_script=rel("asr_script"),
        mt_dictionary=rel("mt_dictionary"),
        alignment_uncertainty_ms=float(doc.get("alignment_uncertainty_ms", 0.0)),
    )
