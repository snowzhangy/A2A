"""Float32/PCM WAV I/O with numpy (no external audio libraries)."""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    """Write mono or (frames, channels) float32 WAV."""
    data = np.asarray(samples, dtype=np.float32)
    channels = 1 if data.ndim == 1 else data.shape[1]
    payload = data.reshape(-1).astype("<f4").tobytes()
    header = b"RIFF" + struct.pack("<I", 36 + len(payload)) + b"WAVE"
    fmt = b"fmt " + struct.pack("<IHHIIHH", 16, 3, channels, rate, rate * channels * 4, channels * 4, 32)
    Path(path).write_bytes(header + fmt + b"data" + struct.pack("<I", len(payload)) + payload)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """Return (samples, rate); samples are (frames,) for mono or (frames, channels)."""
    raw = Path(path).read_bytes()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"{path}: not RIFF/WAVE")
    offset, fmt, data = 12, None, None
    while offset + 8 <= len(raw):
        cid = raw[offset : offset + 4]
        size = struct.unpack("<I", raw[offset + 4 : offset + 8])[0]
        body = raw[offset + 8 : offset + 8 + size]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
        offset += 8 + size + (size & 1)
    if fmt is None or data is None:
        raise ValueError(f"{path}: missing fmt or data chunk")
    tag, channels, rate, _, _, bits = fmt
    if tag == 3 and bits == 32:
        x = np.frombuffer(data, dtype="<f4").astype(np.float32)
    elif tag == 1 and bits == 16:
        x = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    elif tag == 1 and bits == 32:
        x = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"{path}: unsupported format tag={tag} bits={bits}")
    if channels > 1:
        x = x.reshape(-1, channels)
    return x, rate
