"""Shared paths and helpers for the live-dub lab. Research only; nothing here is product code."""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import time

import numpy as np
import soundfile as sf

REPO = pathlib.Path(__file__).resolve().parents[2]
DATA = REPO / ".local/live-dub"
INPUT = DATA / "input"
RUNS = DATA / "runs"
MODELS = DATA / "models"
ACTORS = DATA / "actors"
os.environ.setdefault("HF_HOME", str(DATA / "hf"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

PUNCT = re.compile(r"[\s，。？！、；：,.?!;:「」『』“”\"'…—\-·《》()（）]")


def load(path: str | os.PathLike, start: float = 0.0, end: float | None = None, mono: bool = True) -> tuple[np.ndarray, int]:
    info = sf.info(str(path))
    a, rate = sf.read(str(path), start=int(start * info.samplerate),
                      stop=None if end is None else int(end * info.samplerate), dtype="float32", always_2d=True)
    return (a.mean(axis=1) if mono else a), rate


def save(path: str | os.PathLike, audio: np.ndarray, rate: int) -> None:
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.asarray(audio, dtype=np.float32), rate, subtype="PCM_16")


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return audio
    from scipy.signal import resample_poly
    g = np.gcd(src, dst)
    return resample_poly(audio, dst // g, src // g, axis=0).astype(np.float32)


def episode_audio(name: str, rate: int, start: float = 0.0, end: float | None = None, stereo: bool = False) -> np.ndarray:
    """Decode an episode's audio track with ffmpeg (exact sample rate and channels)."""
    src = INPUT / f"{name}.webm"
    cmd = ["ffmpeg", "-v", "error", "-ss", f"{start}", "-i", str(src)]
    if end is not None:
        cmd += ["-t", f"{end - start}"]
    cmd += ["-ac", "2" if stereo else "1", "-ar", str(rate), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    a = np.frombuffer(raw, dtype=np.float32)
    return a.reshape(-1, 2) if stereo else a


_t2s = None


def hans(text: str) -> str:
    global _t2s
    if _t2s is None:
        import opencc
        _t2s = opencc.OpenCC("t2s")
    return _t2s.convert(text)


PARTICLES = re.compile(r"[啊呢呐吧呀哎哦嘛啦哪喔哼嗯唉哈噢诶欸吗么]")


def norm_zh(text: str) -> str:
    return PUNCT.sub("", hans(text))


def norm_zh_loose(text: str) -> str:
    """For scoring against non-verbatim subtitles: drops sentence particles and interjections."""
    return PARTICLES.sub("", norm_zh(text).replace("甚么", "什么"))


def cer(ref: str, hyp: str) -> tuple[int, int]:
    """Character edit distance and reference length after Hans + punctuation normalization."""
    r, h = norm_zh(ref), norm_zh(hyp)
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hc in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc))
        prev = cur
    return prev[-1], len(r)


def subtitles(name: str = "ep08") -> list[dict]:
    """Published subtitle events: start/end seconds and Simplified text (newlines joined)."""
    d = json.load(open(INPUT / f"{name}.zh-HK.json3"))
    out = []
    for e in d["events"]:
        if "segs" not in e:
            continue
        text = "".join(s.get("utf8", "") for s in e["segs"]).replace("\n", "").strip()
        if text:
            start = e["tStartMs"] / 1000
            out.append({"start": start, "end": start + e.get("dDurationMs", 0) / 1000, "zh": hans(text)})
    return out


class Timer:
    def __init__(self):
        self.t = time.perf_counter()

    def lap(self) -> float:
        now = time.perf_counter()
        dt, self.t = now - self.t, now
        return dt


def write_json(path: str | os.PathLike, obj) -> None:
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(path).write_text(json.dumps(obj, ensure_ascii=False, indent=1))


def available_gb() -> float:
    """Free + inactive + speculative + purgeable memory, from vm_stat (what a new process can claim)."""
    out = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
    page = int(re.search(r"page size of (\d+)", out).group(1))
    pages = sum(int(m) for m in re.findall(
        r"Pages (?:free|inactive|speculative|purgeable):\s+(\d+)", out))
    return pages * page / 2**30


def wait_for_memory(need_gb: float, poll_s: float = 15.0) -> None:
    """Block until `need_gb` is available: other labs share this Mac and the GPU's unified memory."""
    waited = 0.0
    while available_gb() < need_gb:
        if waited == 0:
            print(f"waiting for {need_gb:.0f} GB of free memory ({available_gb():.1f} GB now)", flush=True)
        time.sleep(poll_s)
        waited += poll_s
    if waited:
        print(f"memory available after {waited:.0f} s", flush=True)
