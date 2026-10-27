"""Trace v0 loading and validation (IMPLEMENTATION_PLAN.md section 4.5)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Iterator

import jsonschema

from . import SCHEMA_PATH


@lru_cache(maxsize=1)
def _schema() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_schema())


@lru_cache(maxsize=None)
def _kind_validator(kind: str) -> jsonschema.Draft202012Validator:
    """Equivalent per-kind schema: every conditional in the full schema keys only on `ev`,
    so the applicable `then` clauses can be selected once instead of per event."""
    schema = _schema()
    thens = []
    for clause in schema.get("allOf", []):
        cond = clause["if"]["properties"]["ev"]
        kinds = cond["enum"] if "enum" in cond else [cond["const"]]
        if kind in kinds:
            thens.append(clause["then"])
    specialized = {k: v for k, v in schema.items() if k != "allOf"}
    specialized["allOf"] = thens
    return jsonschema.Draft202012Validator(specialized)


def iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:  # pragma: no cover - surfaced in validate()
                raise ValueError(f"{path}:{n}: invalid JSON: {exc}") from exc


def load(path: Path) -> list[dict]:
    return list(iter_jsonl(Path(path)))


@dataclass
class ValidationReport:
    events: int = 0
    errors: list[str] = field(default_factory=list)
    trace_drops: int = 0
    kinds: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def measurements_valid(self) -> bool:
        """Dropped trace records invalidate affected gate measurements."""
        return self.ok and self.trace_drops == 0


def validate(events: Iterable[dict], *, check_spans: bool = True) -> ValidationReport:
    report = ValidationReport()
    known = set(_schema()["properties"]["ev"]["enum"])
    sessions: set[str] = set()
    for i, ev in enumerate(events, 1):
        report.events += 1
        kind = ev.get("ev")
        validator = _kind_validator(kind) if kind in known else _validator()
        for err in validator.iter_errors(ev):
            path = "/".join(str(p) for p in err.absolute_path) or "<root>"
            report.errors.append(f"event {i} ({ev.get('ev')}): {path}: {err.message}")
        kind = ev.get("ev", "?")
        report.kinds[kind] = report.kinds.get(kind, 0) + 1
        if kind == "trace.drop":
            report.trace_drops += int(ev.get("count", 0))
        if check_spans and isinstance(ev.get("span"), list) and len(ev["span"]) == 2:
            start, end = ev["span"]
            if isinstance(start, int) and isinstance(end, int) and end < start:
                report.errors.append(f"event {i} ({kind}): span end {end} < start {start}")
        if "session" in ev:
            sessions.add(ev["session"])
    if len(sessions) > 1:
        report.errors.append(f"multiple sessions in one trace: {sorted(sessions)}")
    return report


def validate_file(path: Path) -> ValidationReport:
    return validate(load(path))
