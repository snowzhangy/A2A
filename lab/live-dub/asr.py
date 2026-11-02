"""Speech segmentation (Silero VAD via sherpa-onnx) and Mandarin recognition candidates.

Recognizers take one 16 kHz mono utterance and return text. The benchmark segments a span of an
episode with VAD, recognizes each segment, and scores character error rate against the published
subtitles in 60 s windows (both sides assigned to a window by their midpoint; Hans, no punctuation).
Subtitles drop most particles (啊, 呢, 呐), so `cer_loose` also removes particles and interjections from
both sides. Neither is accuracy against a verbatim transcript; use them to compare recognizers.

usage: python asr.py --episode ep08 --start 0 --end 600 --model qwen3-asr-1.7b [--vad-source vocals]
"""
from __future__ import annotations

import argparse
import time

import numpy as np

from common import MODELS, RUNS, episode_audio, load, resample, subtitles, write_json

RATE = 16_000

ASR_MODELS = {
    "qwen3-asr-1.7b": "mlx-community/Qwen3-ASR-1.7B-8bit",
    "qwen3-asr-0.6b": "mlx-community/Qwen3-ASR-0.6B-8bit",
    "sensevoice": "mlx-community/SenseVoiceSmall",
}


def vad_segments(audio: np.ndarray, threshold: float = 0.5, min_silence: float = 0.3,
                 min_speech: float = 0.25, max_speech: float = 12.0) -> list[tuple[float, float]]:
    """Speech spans in seconds from Silero VAD (sherpa-onnx), on a 16 kHz mono signal."""
    import sherpa_onnx
    cfg = sherpa_onnx.VadModelConfig()
    cfg.silero_vad.model = str(MODELS / "onnx/silero_vad.onnx")
    cfg.silero_vad.threshold = threshold
    cfg.silero_vad.min_silence_duration = min_silence
    cfg.silero_vad.min_speech_duration = min_speech
    cfg.silero_vad.max_speech_duration = max_speech
    cfg.sample_rate = RATE
    vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=max(60, int(len(audio) / RATE) + 1))
    win = cfg.silero_vad.window_size
    spans = []

    def drain():
        while not vad.empty():
            seg = vad.front
            spans.append((seg.start / RATE, (seg.start + len(seg.samples)) / RATE))
            vad.pop()

    for i in range(0, len(audio), win):
        vad.accept_waveform(audio[i:i + win])
        drain()
    vad.flush()
    drain()
    return spans


class Recognizer:
    def __init__(self, name: str, hotwords: list[str] | None = None):
        from mlx_audio.stt import load as load_stt
        self.name = name
        self.model = load_stt(ASR_MODELS[name])
        self.hotwords = hotwords or None

    def __call__(self, audio: np.ndarray, context: str | None = None) -> str:
        if self.name.startswith("qwen3-asr"):
            out = self.model.generate(audio, language="Chinese", hotwords=self.hotwords, system_prompt=context)
        else:
            out = self.model.generate(audio, language="zh")
        return (out.text or "").strip()


def score(segments: list[dict], subs: list[dict], start: float, end: float, window: float = 60.0) -> dict:
    from rapidfuzz.distance import Levenshtein
    from common import norm_zh, norm_zh_loose
    edits = loose = ref_chars = 0
    t = start
    while t < end:
        ref = "".join(s["zh"] for s in subs if t <= (s["start"] + s["end"]) / 2 < t + window)
        hyp = "".join(s["text"] for s in segments if t <= (s["start"] + s["end"]) / 2 < t + window)
        r, h = norm_zh(ref), norm_zh(hyp)
        if r:
            edits += Levenshtein.distance(r, h)
            loose += Levenshtein.distance(norm_zh_loose(ref), norm_zh_loose(hyp))
            ref_chars += len(r)
        t += window
    return {"cer": round(edits / max(ref_chars, 1), 4), "cer_loose": round(loose / max(ref_chars, 1), 4),
            "ref_chars": ref_chars}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", default="ep08")
    ap.add_argument("--start", type=float, default=0)
    ap.add_argument("--end", type=float, default=600)
    ap.add_argument("--model", default="qwen3-asr-1.7b", choices=sorted(ASR_MODELS))
    ap.add_argument("--vad-source", default="mix", choices=["mix", "vocals"],
                    help="segment on the mix or on the separated vocals (full-episode Roformer run)")
    ap.add_argument("--asr-source", default="mix", choices=["mix", "vocals"])
    ap.add_argument("--hotwords", default="", help="comma-separated terms passed to Qwen3-ASR")
    a = ap.parse_args()
    mix = episode_audio(a.episode, RATE, a.start, a.end)
    vocals = None
    if "vocals" in (a.vad_source, a.asr_source):
        v, rate = load(RUNS / f"sep-{a.episode}-full/vocals.wav", a.start, a.end)
        vocals = resample(v, rate, RATE)[: len(mix)]
    spans = vad_segments(vocals if a.vad_source == "vocals" else mix)
    rec = Recognizer(a.model, [w for w in a.hotwords.split(",") if w])
    rec(mix[:RATE])  # warm-up
    src = vocals if a.asr_source == "vocals" else mix
    segments, busy = [], 0.0
    for s, e in spans:
        t = time.perf_counter()
        text = rec(src[int(s * RATE):int(e * RATE)])
        busy += time.perf_counter() - t
        segments.append({"start": round(a.start + s, 3), "end": round(a.start + e, 3), "text": text})
    result = {"model": a.model, "vad_source": a.vad_source, "asr_source": a.asr_source, "hotwords": bool(a.hotwords),
              "seconds": a.end - a.start, "segments": len(segments),
              "speech_s": round(sum(e - s for s, e in spans), 1), "compute_s": round(busy, 1),
              "rtf": round(busy / (a.end - a.start), 3),
              "segment_s_p50": round(float(np.median([e - s for s, e in spans])), 2) if spans else None,
              **score(segments, subtitles(a.episode), a.start, a.end)}
    tag = f"asr-{a.episode}-{int(a.start)}-{int(a.end)}-{a.model}-vad{a.vad_source}-asr{a.asr_source}" + ("-hot" if a.hotwords else "")
    write_json(RUNS / tag / "segments.json", segments)
    write_json(RUNS / tag / "result.json", result)
    print(result)


if __name__ == "__main__":
    main()
