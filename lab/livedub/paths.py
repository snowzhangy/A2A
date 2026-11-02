"""Paths and audio helpers for the live dub lab. Research only; nothing here is product code.

Kept independent of lab/live-dub/ on purpose: that lab belongs to another session, and this
one has to keep running while it changes.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time

import numpy as np
import soundfile as sf

REPO = pathlib.Path(__file__).resolve().parents[2]
LAB = REPO / ".local/live-dub"           # models and fetched assets, shared read-only
OUT = REPO / ".local/livedub"            # this lab's runs
MODELS = LAB / "models"
HF = LAB / "hf"
A2A_MODELS = REPO / ".local/models"      # MLX TTS checkpoints from the product manifest

VAD_MODEL = MODELS / "onnx/silero_vad.onnx"
EMBED_MODEL = MODELS / "onnx/3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx"

ASR_RATE = 16_000       # what the recogniser and speaker embedder want
MIX_RATE = 48_000       # the output timeline
TTS_RATE = 24_000       # what the synthesis engines return

os.environ.setdefault("HF_HOME", str(HF))
os.environ.setdefault("HF_HUB_OFFLINE", "1")   # every model here is already on disk and verified


def load(path, start: float = 0.0, end: float | None = None, rate: int | None = None) -> np.ndarray:
    """Mono float32 from a file, optional [start, end) seconds and resample."""
    info = sf.info(str(path))
    a, sr = sf.read(str(path), start=int(start * info.samplerate),
                    stop=None if end is None else int(end * info.samplerate),
                    dtype="float32", always_2d=True)
    a = a.mean(axis=1)
    return a if rate in (None, sr) else resample(a, sr, rate)


def save(path, audio: np.ndarray, rate: int) -> None:
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.asarray(audio, dtype=np.float32), rate, subtype="PCM_16")


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or len(audio) == 0:
        return np.asarray(audio, dtype=np.float32)
    from scipy.signal import resample_poly
    g = np.gcd(src, dst)
    return resample_poly(np.asarray(audio, dtype=np.float32), dst // g, src // g).astype(np.float32)


def decode(path, start: float, end: float, rate: int) -> np.ndarray:
    """Mono float32 out of any media file with ffmpeg, at an exact rate."""
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start}", "-i", str(path), "-t", f"{end - start}",
           "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.float32).copy()


def rms(audio: np.ndarray) -> float:
    if len(audio) == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


def trim_silence(audio: np.ndarray, rate: int, threshold: float = 0.004,
                 pad: float = 0.02) -> tuple[np.ndarray, float]:
    """Strip leading and trailing quiet, keeping `pad` seconds of margin. Returns (audio, shift)."""
    frame = max(1, int(rate * 0.01))
    n = len(audio) // frame
    if n == 0:
        return audio, 0.0
    energy = np.sqrt(np.square(np.asarray(audio[: n * frame], dtype=np.float32).reshape(n, frame)).mean(axis=1))
    loud = np.nonzero(energy > threshold)[0]
    if len(loud) == 0:
        return audio, 0.0
    p = int(pad * rate)
    start = max(0, int(loud[0]) * frame - p)
    end = min(len(audio), (int(loud[-1]) + 1) * frame + p)
    return audio[start:end], start / rate


def a2a_model(name: str) -> pathlib.Path:
    """A checkpoint in .local/models/<name>/, which stores its files under the revision."""
    base = A2A_MODELS / name
    if (base / "config.json").exists():
        return base
    for sub in sorted(base.iterdir()):
        if (sub / "config.json").exists():
            return sub
    raise FileNotFoundError(f"no config.json under {base}")


def model_from_cache(repo_id: str) -> pathlib.Path:
    """A snapshot already in the HF cache, without asking the hub (offline-safe)."""
    d = HF / "hub" / ("models--" + repo_id.replace("/", "--")) / "snapshots"
    subs = sorted(d.iterdir()) if d.exists() else []
    if not subs:
        raise FileNotFoundError(f"{repo_id} is not in {HF}/hub")
    return subs[-1]


def clock() -> float:
    return time.perf_counter()


def write_json(path, obj) -> None:
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=str))
