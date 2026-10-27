"""Speaker-embedding cosine similarity with Qwen3-TTS's own speaker encoder.

NOT independent: the synthesizer is conditioned on this same encoder, so a high score is
circular and is never identity evidence (M2 needs an independent verifier and listeners).
It is a cheap regression check between conditioning modes and references. Voiced 10 ms
frames only (-42 dBFS), first 20 s, so silence between units does not dilute the embedding.

usage: uv run --offline --locked --project research python research/speaker_similarity.py \
           REFERENCE.wav CANDIDATE.wav [CANDIDATE.wav ...]
"""
from __future__ import annotations

import sys

import voice_clone  # noqa: F401  Sets offline mode before model libraries load.
from voice_clone import verified_model


def main() -> None:
    import mlx.core as mx
    import numpy as np
    from mlx_audio.tts.utils import load_model
    from mlx_audio.utils import load_audio

    path, _ = verified_model()
    model = load_model(str(path), strict=True)

    def embedding(file: str) -> tuple[np.ndarray, float]:
        pcm = np.asarray(load_audio(file, sample_rate=24000), dtype=np.float32)
        frames = pcm[: len(pcm) // 240 * 240].reshape(-1, 240)
        voiced = frames[np.sqrt((frames ** 2).mean(axis=1)) >= 10 ** (-42 / 20)].reshape(-1)[: 24000 * 20]
        if voiced.size < 24000:
            raise ValueError(f"{file}: under 1 s of voiced audio")
        e = np.asarray(model.extract_speaker_embedding(mx.array(voiced))).reshape(-1)
        return e / np.linalg.norm(e), voiced.size / 24000

    reference, _ = embedding(sys.argv[1])
    for file in sys.argv[2:]:
        e, seconds = embedding(file)
        print(f"{float(reference @ e):.3f}  voiced {seconds:5.1f} s  {file}")


if __name__ == "__main__":
    main()
