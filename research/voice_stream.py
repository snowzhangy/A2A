"""Measure isolated clone streaming with resident model/reference and in-memory PCM.

Only load input files before inference and write artifacts after all timed runs.
This is not an end-to-end latency, speaker-quality, or live worker qualification.
"""
from __future__ import annotations

import argparse
import json
import time
import wave
from pathlib import Path

from voice_clone import ROOT, verified_model  # Also enforces offline library configuration.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--reference-text", type=Path, required=True)
    parser.add_argument("--text", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--interval", type=float, default=0.5)
    args = parser.parse_args()
    args.out.resolve().relative_to((ROOT / ".local").resolve())
    if args.out.exists():
        raise FileExistsError("Use a new output directory")
    if not 1 <= args.runs <= 5 or not 0.08 <= args.interval <= 2:
        raise ValueError("Use 1–5 runs and an interval between 0.08 and 2 seconds")
    model_path, entry = verified_model()

    import mlx.core as mx
    import numpy as np
    from mlx_audio.tts.utils import load_model
    from mlx_audio.utils import load_audio

    reference = load_audio(str(args.reference.resolve()), sample_rate=24000)
    mx.eval(reference)
    reference_seconds = reference.size / 24000
    with wave.open(str(args.reference), "rb") as f:
        if f.getframerate() < 24000 or not 3 <= reference_seconds <= 10:
            raise ValueError("Use a full-band 3–10 second reference")
    reference_text = args.reference_text.read_text().strip()
    text = args.text.read_text().strip()
    if not reference_text or not text:
        raise ValueError("Reference and target text are required")
    start = time.perf_counter()
    print("Loading resident model", flush=True)
    model = load_model(str(model_path), strict=True)
    if (model.tokenizer is None or model.speech_tokenizer is None
            or not model.speech_tokenizer.has_encoder or model.speaker_encoder is None):
        raise RuntimeError("Required local tokenizers did not load")
    load_seconds = time.perf_counter() - start
    runs = []
    outputs = []
    for index in range(args.runs):
        mx.random.seed(42)
        chunks = []
        events = []
        emitted_seconds = 0.0
        start = time.perf_counter()
        for result in model.generate(
            text=text, ref_audio=reference, ref_text=reference_text,
            lang_code="english", stream=True, streaming_interval=args.interval,
            max_tokens=250, verbose=False,
        ):
            mx.eval(result.audio)
            pcm = np.asarray(result.audio, dtype=np.float32).reshape(-1).copy()
            arrival = time.perf_counter() - start
            if result.sample_rate != 24000 or not np.isfinite(pcm).all():
                raise RuntimeError("Invalid generated PCM")
            seconds = pcm.size / result.sample_rate
            events.append({
                "arrival_seconds": arrival,
                "audio_start_seconds": emitted_seconds,
                "audio_seconds": seconds,
                "peak": float(np.max(np.abs(pcm))) if pcm.size else 0,
            })
            emitted_seconds += seconds
            chunks.append(pcm)
        elapsed = time.perf_counter() - start
        if not chunks or emitted_seconds == 0:
            raise RuntimeError("No generated audio")
        record = {
            "run": index + 1,
            "reference_cache_warm": index > 0,
            "first_chunk_seconds": events[0]["arrival_seconds"],
            "first_signal_chunk_seconds": next((e["arrival_seconds"] for e in events if e["peak"] > 1e-4), None),
            "generation_seconds": elapsed,
            "audio_seconds": emitted_seconds,
            "real_time_factor": elapsed / emitted_seconds,
            "minimum_start_buffer_seconds": max(e["arrival_seconds"] - e["audio_start_seconds"] for e in events),
            "chunks": events,
        }
        runs.append(record)
        outputs.append(np.concatenate(chunks))
        print(json.dumps({k: v for k, v in record.items() if k != "chunks"}), flush=True)
    # Timed inference above has no audio/text file I/O or playback. Save research evidence now.
    args.out.mkdir(parents=True)
    for index, pcm in enumerate(outputs):
        peak = float(np.max(np.abs(pcm)))
        gain = min(1.0, 0.98 / max(peak, 1e-6))
        with wave.open(str(args.out / f"run-{index + 1}.wav"), "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(24000)
            f.writeframes(np.rint(np.clip(pcm * gain, -1, 1) * 32767).astype("<i2").tobytes())
    report = {
        "stage": "in_memory_clone_streaming",
        "scope": "isolated TTS; no ASR, MT, tap, output device, or concurrent inference",
        "converted_revision": entry["converted"]["revision"],
        "reference_seconds": reference_seconds,
        "reference_text": reference_text,
        "text": text,
        "streaming_interval": args.interval,
        "model_load_seconds": load_seconds,
        "peak_mlx_allocation_bytes": mx.get_peak_memory(),
        "runs": runs,
    }
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
