"""Speaker embeddings, episode clustering, and the per-series actor database.

Embeddings come from 3D-Speaker models trained on Mandarin (CAM++ or ERes2NetV2, 192-d, sherpa-onnx on
CPU, so they never compete with the MLX models for the GPU). Segments are cut by Silero VAD on the
separated vocals, so music does not trigger speech and the embedding sees the voice, not the score.

The actor database (`.local/live-dub/actors/<series>/`, 0700) keeps, per actor: several embedding
centroids, the best clean references (separated vocals with their transcripts), and a display name.
Nothing is trained; recognition is nearest-centroid cosine scoring with an open-set threshold, so an
unknown voice becomes a new actor instead of being forced onto the closest known one.

usage:
  python speakers.py segment --episode ep08            # VAD on vocals + embeddings -> runs/spk-ep08/
  python speakers.py cluster --episode ep08 --threshold 0.55
"""
from __future__ import annotations

import argparse
import json

import numpy as np

from common import MODELS, RUNS, load, resample, write_json

RATE = 16_000
EMBEDDERS = {
    "campplus": "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx",
    "eres2netv2": "3dspeaker_speech_eres2netv2_sv_zh-cn_16k-common.onnx",
}


class Embedder:
    def __init__(self, name: str = "campplus", threads: int = 4):
        import sherpa_onnx
        cfg = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODELS / "onnx" / EMBEDDERS[name]),
                                                          num_threads=threads)
        self.ex = sherpa_onnx.SpeakerEmbeddingExtractor(cfg)
        self.name = name

    def __call__(self, audio: np.ndarray) -> np.ndarray:
        if len(audio) < RATE // 2:  # the extractor needs frames; tile very short clips to 0.5 s
            audio = np.tile(audio, RATE // 2 // max(1, len(audio)) + 1)[: RATE // 2] if len(audio) else np.zeros(RATE // 2, np.float32)
        s = self.ex.create_stream()
        s.accept_waveform(RATE, audio)
        s.input_finished()
        e = np.array(self.ex.compute(s), dtype=np.float32)
        return e / (np.linalg.norm(e) + 1e-9)


def episode_vocals(episode: str) -> np.ndarray:
    v, rate = load(RUNS / f"sep-{episode}-full/vocals.wav")
    return resample(v, rate, RATE)


def segment(episode: str, embedder: str = "campplus", min_len: float = 0.8) -> list[dict]:
    from asr import vad_segments
    vocals = episode_vocals(episode)
    spans = vad_segments(vocals, threshold=0.5, min_silence=0.2, max_speech=8.0)
    emb = Embedder(embedder)
    out = []
    for s, e in spans:
        if e - s < min_len:
            continue
        a = vocals[int(s * RATE):int(e * RATE)]
        if np.sqrt((a ** 2).mean()) < 0.003:
            continue
        out.append({"start": round(s, 3), "end": round(e, 3), "emb": emb(a).tolist()})
    return out


def cluster(embs: np.ndarray, threshold: float) -> np.ndarray:
    """Average-linkage agglomerative clustering on cosine distance; returns labels 0..k-1."""
    from scipy.cluster.hierarchy import fcluster, linkage
    z = linkage(embs, method="average", metric="cosine")
    return fcluster(z, t=1 - threshold, criterion="distance") - 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["segment", "cluster"])
    ap.add_argument("--episode", default="ep08")
    ap.add_argument("--embedder", default="campplus", choices=sorted(EMBEDDERS))
    ap.add_argument("--threshold", type=float, default=0.55, help="cosine similarity to merge")
    a = ap.parse_args()
    path = RUNS / f"spk-{a.episode}-{a.embedder}/segments.json"
    if a.cmd == "segment":
        segs = segment(a.episode, a.embedder)
        write_json(path, segs)
        print(f"{len(segs)} segments, {sum(s['end'] - s['start'] for s in segs):.0f} s of speech")
    else:
        segs = json.load(open(path))
        labels = cluster(np.array([s["emb"] for s in segs]), a.threshold)
        dur = {}
        for s, l in zip(segs, labels):
            dur[int(l)] = dur.get(int(l), 0) + s["end"] - s["start"]
        top = sorted(dur.items(), key=lambda x: -x[1])
        print(f"{len(top)} clusters; top 12 by speech seconds:", [(k, round(v)) for k, v in top[:12]])
        write_json(path.with_name(f"clusters-{a.threshold:g}.json"),
                   [{"start": s["start"], "end": s["end"], "cluster": int(l)} for s, l in zip(segs, labels)])


if __name__ == "__main__":
    main()
