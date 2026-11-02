"""Minimal RIFF/WAVE reader and writer: PCM 16/24/32-bit integer and 32-bit float.
Port of apple/Sources/A2AMedia/WAV.swift. Never used on a real-time thread."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class WAVAudio:
    sample_rate: int
    channels: int
    # Interleaved samples in [-1, 1].
    samples: np.ndarray

    @property
    def frame_count(self) -> int:
        return 0 if self.channels == 0 else len(self.samples) // self.channels

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / self.sample_rate

    def mono(self) -> np.ndarray:
        """Average of all channels (float32, summed in float32 like the Swift reader)."""
        if self.channels <= 1:
            return self.samples.astype(np.float32, copy=False)
        frames = self.samples[: self.frame_count * self.channels].reshape(-1, self.channels).astype(np.float32)
        acc = np.zeros(len(frames), dtype=np.float32)
        for c in range(self.channels):
            acc += frames[:, c]
        return acc * np.float32(1 / self.channels)


class WAVError(ValueError):
    pass


def decode(data: bytes) -> WAVAudio:
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise WAVError("not a RIFF/WAVE file")
    offset, tag, channels, rate, bits, payload = 12, 0, 0, 0, 0, None
    while offset + 8 <= len(data):
        cid = data[offset:offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        body = offset + 8
        if size > len(data) - body:
            raise WAVError(f"malformed WAV: truncated {cid!r} chunk")
        if cid == b"fmt ":
            if size < 16:
                raise WAVError("malformed WAV: fmt chunk is shorter than 16 bytes")
            tag, channels, rate = struct.unpack_from("<HHI", data, body)
            bits = struct.unpack_from("<H", data, body + 14)[0]
            if tag == 0xFFFE:
                if size < 40:
                    raise WAVError("malformed WAV: truncated extensible fmt chunk")
                tag = struct.unpack_from("<H", data, body + 24)[0]
        elif cid == b"data":
            payload = (body, body + size)
        offset = body + size + (size & 1)
    if channels <= 0 or rate <= 0:
        raise WAVError("missing fmt chunk")
    if payload is None:
        raise WAVError("missing data chunk")
    raw = data[payload[0]:payload[1]]
    if (tag == 1 and bits in (16, 24, 32)) or (tag == 3 and bits == 32):
        if len(raw) % (channels * bits // 8):
            raise WAVError("malformed WAV: data chunk contains an incomplete audio frame")
    if tag == 1 and bits == 16:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32_768
    elif tag == 1 and bits == 24:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)
        samples = v.astype(np.float32) / 8_388_608
    elif tag == 1 and bits == 32:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2_147_483_648
    elif tag == 3 and bits == 32:
        samples = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    else:
        raise WAVError(f"unsupported WAV format tag {tag} with {bits} bits")
    return WAVAudio(rate, channels, samples)


def read(path: str | Path) -> WAVAudio:
    return decode(Path(path).read_bytes())


def _header(sample_rate: int, channels: int, data_bytes: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE" + b"fmt " + struct.pack("<I", 16)
            + struct.pack("<HHIIHH", 3, channels, sample_rate, sample_rate * channels * 4, channels * 4, 32)
            + b"data" + struct.pack("<I", data_bytes))


def encode_float32(audio: WAVAudio) -> bytes:
    pcm = np.ascontiguousarray(audio.samples, dtype="<f4").tobytes()
    return _header(audio.sample_rate, audio.channels, len(pcm)) + pcm


def write(audio: WAVAudio, path: str | Path) -> None:
    Path(path).write_bytes(encode_float32(audio))


class WAVStreamWriter:
    """Streams float32 frames to a WAV file and patches the header on close."""

    def __init__(self, path: str | Path, sample_rate: int, channels: int) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.frames_written = 0
        self._fh = open(path, "wb")
        self._fh.write(_header(sample_rate, channels, 0))

    def append(self, interleaved) -> None:
        a = np.ascontiguousarray(interleaved, dtype="<f4")
        if not len(a):
            return
        self._fh.write(a.tobytes())
        self.frames_written += len(a) // self.channels

    def close(self) -> None:
        if self._fh.closed:
            return
        data_bytes = self.frames_written * self.channels * 4
        self._fh.seek(4)
        self._fh.write(struct.pack("<I", 36 + data_bytes))
        self._fh.seek(40)
        self._fh.write(struct.pack("<I", data_bytes))
        self._fh.close()
