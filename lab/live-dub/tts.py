"""Cloned English takes for dubbing: OmniVoice (whole line, exact duration) and Qwen3-TTS (streaming).

Both read the pinned weights already in `.local/models/` (read-only). The reference for each line follows
dub-anything's finding: the actor's stored clip(s) followed by the actor's own delivery of this very line
(separated vocals), so likeness comes from the clip and the acting from the line. OmniVoice is told the
slot length directly (`duration_s`), which is dub-anything's "fill" without re-rendering.
"""
from __future__ import annotations

import time

import mlx.core as mx
import numpy as np

from common import REPO

RATE = 24_000
WEIGHTS = {
    "omnivoice": REPO / ".local/models/omnivoice-0.6b-mlx-bf16/c5fdb5ccb189668d56333f77ba2629f4cd7535f4",
    "qwen": REPO / ".local/models/qwen3-tts-12hz-0.6b-base/5d83992436eae1d760afd27aff78a71d676296fc",
}


def _join(chunks) -> np.ndarray:
    out = [np.array(c.audio, dtype=np.float32).reshape(-1) for c in chunks]
    return np.concatenate(out) if out else np.zeros(0, np.float32)


class Voice:
    """Reference audio (24 kHz mono) with its transcript."""

    def __init__(self, pcm: np.ndarray, text: str):
        self.pcm, self.text = pcm.astype(np.float32), text

    @staticmethod
    def concat(parts: list["Voice"], gap: float = 0.15, max_s: float = 10.0) -> "Voice":
        """Join references, keeping the last parts (the line itself goes last) within max_s."""
        kept, total = [], 0.0
        for p in reversed(parts):
            d = len(p.pcm) / RATE
            if kept and total + d > max_s:
                break
            kept.insert(0, p)
            total += d + gap
        silence = np.zeros(int(gap * RATE), np.float32)
        pcm = np.concatenate([x for p in kept for x in (p.pcm, silence)][:-1])
        return Voice(pcm, " ".join(p.text for p in kept))


class OmniVoiceTTS:
    streaming = False

    def __init__(self, num_steps: int = 32):
        from mlx_audio.tts.utils import load_model
        self.model = load_model(str(WEIGHTS["omnivoice"]), strict=True)
        self.num_steps = num_steps
        self.token_cache: dict[str, mx.array] = {}  # encoded stored clips, by key (at most a few per actor)

    def encode(self, pcm: np.ndarray, key: str | None = None) -> mx.array:
        if key is not None and key in self.token_cache:
            return self.token_cache[key]
        tokens = self.model.audio_tokenizer.encode(mx.array(pcm)[None, :, None])[0]
        mx.eval(tokens)
        if key is not None:
            self.token_cache[key] = tokens
        return tokens

    def render_parts(self, parts: list[tuple[str | None, np.ndarray, str]], text: str,
                     duration_s: float | None) -> tuple[np.ndarray, float]:
        """Reference = stored clips (encoded once, cached by key) followed by this line (encoded now).

        Codec tokens of separately encoded clips are concatenated in time, which skips re-encoding the
        actor's clips on every line."""
        t = time.perf_counter()
        ref = mx.concatenate([self.encode(pcm, key) for key, pcm, _ in parts], axis=0)
        audio = _join(self.model.generate(text=text, language="en", ref_tokens=ref,
                                          ref_text=" ".join(x for _, _, x in parts), duration_s=duration_s,
                                          num_steps=self.num_steps))
        return audio, time.perf_counter() - t

    def natural_parts(self, parts: list[tuple[str | None, np.ndarray, str]], text: str) -> float:
        from mlx_audio.tts.models.omnivoice.duration import RuleDurationEstimator
        frames = sum(int(len(pcm) / RATE * 25) for _, pcm, _ in parts)
        return RuleDurationEstimator().estimate_duration(text, " ".join(x for _, _, x in parts), frames) / 25.0

    def natural_duration(self, voice: Voice, text: str) -> float:
        from mlx_audio.tts.models.omnivoice.duration import RuleDurationEstimator
        frames = RuleDurationEstimator().estimate_duration(text, voice.text, int(len(voice.pcm) / RATE * 25))
        return frames / 25.0

    def __call__(self, voice: Voice, text: str, duration_s: float | None) -> tuple[np.ndarray, float]:
        t = time.perf_counter()
        ref = self.model.audio_tokenizer.encode(mx.array(voice.pcm)[None, :, None])[0]
        audio = _join(self.model.generate(text=text, language="en", ref_tokens=ref, ref_text=voice.text,
                                          duration_s=duration_s, num_steps=self.num_steps))
        return audio, time.perf_counter() - t


class QwenTTS:
    streaming = True

    def __init__(self, temperature: float = 0.6):
        from mlx_audio.tts.utils import load_model
        self.model = load_model(str(WEIGHTS["qwen"]), strict=True)
        self.temperature = temperature

    def __call__(self, voice: Voice, text: str, duration_s: float | None = None) -> tuple[np.ndarray, float]:
        t = time.perf_counter()
        audio = _join(self.model.generate(text=text, ref_audio=mx.array(voice.pcm), ref_text=voice.text,
                                          lang_code="english", temperature=self.temperature, verbose=False))
        return audio, time.perf_counter() - t


def fit(audio: np.ndarray, target_s: float, lo: float = 0.8, hi: float = 1.25) -> tuple[np.ndarray, float]:
    """Trim edge silence, then change tempo toward the slot length, clamped to [lo, hi] speed."""
    a = trim(audio)
    if not len(a) or target_s <= 0:
        return a, 1.0
    speed = float(np.clip(len(a) / RATE / target_s, lo, hi))
    if abs(speed - 1) < 0.03:
        return a, 1.0
    return wsola(a, speed), speed


def trim(audio: np.ndarray, floor_db: float = -45.0) -> np.ndarray:
    if not len(audio):
        return audio
    frame = int(0.01 * RATE)
    n = len(audio) // frame
    if n == 0:
        return audio
    rms = np.sqrt((audio[: n * frame].reshape(n, frame) ** 2).mean(axis=1) + 1e-12)
    on = np.where(20 * np.log10(rms / (rms.max() + 1e-12)) > floor_db)[0]
    if not len(on):
        return audio[:0]
    return audio[max(0, on[0] - 2) * frame:min(n, on[-1] + 3) * frame]


def wsola(x: np.ndarray, speed: float, rate: int = RATE) -> np.ndarray:
    """Offline WSOLA tempo change (speed > 1 shortens). 40 ms Hann windows, 20 ms hop, ±10 ms search."""
    win = rate // 25
    hop = win // 2
    search = rate // 100
    w = np.hanning(win).astype(np.float32)
    n_out = int(len(x) / speed)
    out = np.zeros(n_out + win, np.float32)
    norm = np.zeros(n_out + win, np.float32)
    xp = np.pad(x, (search, win + search))
    prev = None
    for o in range(0, n_out, hop):
        nominal = int(o * speed) + search
        if prev is None:
            pos = nominal
        else:
            # Best match to the natural continuation of the previous frame.
            target = xp[prev + hop:prev + hop + win]
            lo, hi = max(0, nominal - search), min(len(xp) - win, nominal + search)
            region = xp[lo:hi + win]
            if len(target) < win or len(region) < win:
                pos = nominal
            else:
                c = np.correlate(region, target, mode="valid")
                pos = lo + int(np.argmax(c))
        frame = xp[pos:pos + win]
        if len(frame) < win:
            break
        out[o:o + win] += frame * w
        norm[o:o + win] += w
        prev = pos
    return (out / np.maximum(norm, 1e-3))[:n_out]
