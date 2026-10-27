"""Isolated local Qwen3 voice-cloning probe; no capture, playback, or runtime downloads.

Explicit setup: scripts/fetch_models.py --models qwen3-tts-12hz-0.6b-base
Run with research's locked uv environment. All research outputs must stay in .local/.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
import wave
from pathlib import Path

# Set before importing any model libraries. Setup owns downloads; inference is offline.
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ["TOKENIZERS_PARALLELISM"] = "false"

ROOT = Path(__file__).resolve().parent.parent
MODEL_ID = "qwen3-tts-12hz-0.6b-base"


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def verified_model(model_id: str = MODEL_ID) -> tuple[Path, dict]:
    import yaml

    manifest = yaml.safe_load((ROOT / "models/manifest.yaml").read_text())
    entry = next(e for e in manifest["entries"] if e["id"] == model_id)
    if not entry["enabled"] or entry["status"] not in ("verified", "qualified"):
        raise ValueError("Model must be explicitly enabled and verified in the manifest")
    model = ROOT / manifest["cache_dir"] / model_id / entry["upstream"]["revision"]
    if not model.is_dir():
        raise FileNotFoundError("Run the explicit fetch_models.py setup step first")
    for item in entry["files"]:
        path = model / item["path"]
        if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
            raise ValueError(f"Model integrity check failed: {item['path']}")
    return model.resolve(), entry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--reference-text", type=Path, required=True)
    parser.add_argument("--text", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--language", default="english", choices=("english", "chinese"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-tokens", type=int, default=250)
    args = parser.parse_args()
    args.out.resolve().relative_to((ROOT / ".local").resolve())
    if args.out.exists():
        raise FileExistsError("Use a new output directory to preserve previous evidence")
    with wave.open(str(args.reference), "rb") as f:
        reference_rate = f.getframerate()
        reference_seconds = f.getnframes() / reference_rate
        if f.getnchannels() != 1 or f.getsampwidth() != 2:
            raise ValueError("Reference must be mono PCM16 WAV")
        if reference_rate < 24000 or not 3 <= reference_seconds <= 10:
            raise ValueError("Use a 3–10 second full-band source reference, at least 24 kHz")
    text = args.text.read_text().strip()
    ref_text = args.reference_text.read_text().strip()
    if not text or not ref_text:
        raise ValueError("Both target text and the reference clip's own ASR transcript are required")
    if not 1 <= args.max_tokens <= 500:
        raise ValueError("max-tokens must be between 1 and 500")
    print("Verifying local model artifacts", flush=True)
    model_path, entry = verified_model()

    import mlx.core as mx
    import numpy as np
    from mlx_audio.tts.utils import load_model

    mx.random.seed(args.seed)
    start = time.perf_counter()
    print("Loading verified model on MLX", flush=True)
    model = load_model(str(model_path), strict=True)
    if model.tokenizer is None or model.speech_tokenizer is None:
        raise RuntimeError("Required local text/audio tokenizer failed to load")
    if not model.speech_tokenizer.has_encoder or model.speaker_encoder is None:
        raise RuntimeError("Reference-conditioned cloning requires the codec encoder and speaker encoder")
    load_seconds = time.perf_counter() - start
    args.out.mkdir(parents=True)
    start = time.perf_counter()
    first_audio_seconds = None
    chunks = []
    sample_rate = None
    print("Generating reference-conditioned speech (offline)", flush=True)
    for result in model.generate(
        text=text,
        ref_audio=str(args.reference.resolve()),
        ref_text=ref_text,
        lang_code=args.language,
        max_tokens=args.max_tokens,
        stream=False,
        verbose=False,
    ):
        mx.eval(result.audio)
        pcm = np.asarray(result.audio, dtype=np.float32).reshape(-1)
        if first_audio_seconds is None:
            first_audio_seconds = time.perf_counter() - start
        if sample_rate is not None and result.sample_rate != sample_rate:
            raise ValueError("Unexpected sample-rate change")
        sample_rate = result.sample_rate
        if not np.isfinite(pcm).all():
            raise ValueError("Model generated non-finite samples")
        chunks.append(pcm)
    generation_seconds = time.perf_counter() - start
    if not chunks:
        raise RuntimeError("Model returned no audio")
    pcm = np.concatenate(chunks)
    peak = float(np.max(np.abs(pcm)))
    if peak < 1e-5:
        raise RuntimeError("Model generated silence")
    gain = min(1.0, 0.98 / peak)
    pcm16 = np.rint(np.clip(pcm * gain, -1, 1) * 32767).astype("<i2")
    with wave.open(str(args.out / "speech.wav"), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sample_rate)
        f.writeframes(pcm16.tobytes())
    report = {
        "stage": "reference_conditioned_tts",
        "model_id": MODEL_ID,
        "upstream_revision": entry["upstream"]["revision"],
        "converted_revision": entry["converted"]["revision"],
        "runtime": "mlx-audio 0.6.1; MLX 0.32.4; research/uv.lock",
        "offline": True,
        "voice_conditioning": "speaker embedding and reference codec/text (ICL)",
        "identity_qualified": False,
        "reference": str(args.reference.resolve()),
        "reference_sha256": digest(args.reference),
        "reference_sample_rate": reference_rate,
        "reference_seconds": reference_seconds,
        "reference_text": ref_text,
        "text": text,
        "language": args.language,
        "seed": args.seed,
        "max_tokens": args.max_tokens,
        "load_seconds": load_seconds,
        "generation_seconds": generation_seconds,
        "first_result_seconds": first_audio_seconds,
        "streaming": False,
        "sample_rate": sample_rate,
        "frames": len(pcm),
        "audio_seconds": len(pcm) / sample_rate,
        "real_time_factor": generation_seconds / (len(pcm) / sample_rate),
        "peak_memory_bytes": mx.get_peak_memory(),
        "raw_peak": peak,
        "pcm_gain": gain,
        "speech_sha256": digest(args.out / "speech.wav"),
    }
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ("audio_seconds", "generation_seconds", "real_time_factor", "peak_memory_bytes")}))


if __name__ == "__main__":
    main()
