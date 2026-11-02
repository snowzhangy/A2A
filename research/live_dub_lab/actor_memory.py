"""Experimental local speaker-vector store. No automatic enrollment or model training.

The vectors are sensitive biometric data. This is a research prototype, not the app's
voice-memory format or a qualified identification system.
"""
from __future__ import annotations

import math
import os
import sqlite3
import struct
from pathlib import Path


def normalize(values):
    values = tuple(float(x) for x in values)
    norm = math.sqrt(sum(x * x for x in values))
    if not values or not math.isfinite(norm) or norm <= 1e-12:
        raise ValueError("invalid speaker vector")
    return tuple(x / norm for x in values)


class ActorMemory:
    def __init__(self, directory: Path, model_id: str):
        directory = Path(directory)
        if directory.is_symlink():
            raise ValueError("database directory cannot be a symlink")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        path = directory / "actors.sqlite3"
        if path.is_symlink():
            raise ValueError("database cannot be a symlink")
        # Create with restrictive permissions before SQLite ever writes data.
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        os.chmod(path, 0o600)
        self.db = sqlite3.connect(path)
        self.model_id = model_id
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("CREATE TABLE IF NOT EXISTS exemplars (actor TEXT NOT NULL, model TEXT NOT NULL, "
                        "source TEXT NOT NULL, dimensions INTEGER NOT NULL, vector BLOB NOT NULL, "
                        "PRIMARY KEY(actor, model, source))")
        self.db.commit()

    def enroll(self, actor: str, source: str, vector):
        if not actor or not source:
            raise ValueError("actor and source are required")
        v = normalize(vector)
        self.db.execute("INSERT INTO exemplars VALUES (?, ?, ?, ?, ?)",
                        (actor, self.model_id, source, len(v), struct.pack(f"<{len(v)}f", *v)))
        self.db.commit()

    def rank(self, vector, excluded_actor: str | None = None):
        v = normalize(vector)
        scores = {}
        for actor, dim, blob in self.db.execute(
            "SELECT actor, dimensions, vector FROM exemplars WHERE model = ?", (self.model_id,)
        ):
            if actor == excluded_actor:
                continue
            if dim != len(v) or len(blob) != 4 * dim:
                raise ValueError("incompatible or corrupt stored vector")
            saved = normalize(struct.unpack(f"<{dim}f", blob))
            score = sum(a * b for a, b in zip(v, saved))
            scores[actor] = max(scores.get(actor, -1.0), score)
        return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))

    def close(self):
        self.db.close()
