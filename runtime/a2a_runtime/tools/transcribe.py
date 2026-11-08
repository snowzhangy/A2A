"""a2a-transcribe (portable): back-transcribe audio with the verified Whisper model, offline.

The counterpart of apple/Tools/a2a-transcribe for the portable runtime. It checks what was
actually played, not only the trace text (dub-anything's "transcribe it back to catch garbled
words" step). Plumbing and intelligibility evidence, never a model-quality metric.

    uv run --project runtime python -m a2a_runtime.tools.transcribe --input translated.wav [--locale en-US]

Prints JSON: {"locale", "duration_s", "segments": [{"start_s", "end_s", "text"}], "text"}.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np


def transcribe(path: str, locale: str = "en-US", model_id: str = "faster-whisper-large-v3", device: str = "auto") -> dict:
    os.environ["HF_HUB_OFFLINE"] = "1"
    from faster_whisper import WhisperModel
    import ctranslate2

    from ..media import wav
    from ..media.resampler import StreamingResampler
    from ..workers.model_paths import installed_model

    audio = wav.read(path)
    samples = audio.mono()
    if audio.sample_rate != 16_000:
        samples = StreamingResampler.resample(samples, audio.sample_rate, 16_000)
    cuda = ctranslate2.get_cuda_device_count() > 0 and device in ("auto", "cuda")
    model = WhisperModel(str(installed_model(model_id, "asr")), device="cuda" if cuda else "cpu",
                         compute_type="int8_float16" if cuda else "int8", local_files_only=True)
    language = locale.split("-")[0].lower()
    segments, _ = model.transcribe(np.asarray(samples, dtype=np.float32), language=language, beam_size=5,
                                   condition_on_previous_text=False, vad_filter=True)
    rows = [{"start_s": round(s.start, 3), "end_s": round(s.end, 3), "text": s.text.strip()} for s in segments]
    return {"locale": locale, "duration_s": round(len(samples) / 16_000, 3), "segments": rows,
            "text": " ".join(r["text"] for r in rows if r["text"])}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--input", required=True)
    ap.add_argument("--locale", default="en-US")
    ap.add_argument("--model", default="faster-whisper-large-v3")
    ap.add_argument("--device", default="auto", choices=("auto", "cuda", "cpu"))
    ap.add_argument("--out", help="write the JSON here as well as to stdout")
    args = ap.parse_args()
    report = transcribe(args.input, args.locale, args.model, args.device)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
