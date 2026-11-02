"""Matched OmniVoice step-count experiment; local verified weights and private outputs."""
from __future__ import annotations

import argparse
import array
import json
import os
import sys
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "research"))
import voice_clone  # noqa: E402
from tts_model_adapters import OmniVoiceAdapter  # noqa: E402
from tts_worker import ModelBackend, Shaper, Voice  # noqa: E402


class StepAdapter(OmniVoiceAdapter):
    def __init__(self, original, steps):
        super().__init__(original.model, original.mx, original.duration_estimator)
        self.steps = steps

    def generate(self, voice, text, interval, max_tokens, temperature):
        yield from self.model.generate(text=text, language="en", ref_tokens=voice.native,
            ref_text=voice.text, duration_s=self.estimated_duration(voice, text), num_steps=self.steps)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--plan", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--steps", nargs="+", type=int, default=[32, 16, 8])
    a = p.parse_args()
    for path in (a.plan, a.out):
        path.resolve().relative_to((ROOT / ".local").resolve())
    if a.out.exists() or any(s not in (4, 8, 16, 32) for s in a.steps):
        raise ValueError("use a new output directory and supported step counts")
    a.out.mkdir(parents=True, mode=0o700)
    os.chmod(a.out, 0o700)
    import numpy as np
    backend = ModelBackend("omnivoice", None)
    backend.load()
    backend.warmup()
    original = backend.adapter
    records = []
    for row in json.loads(a.plan.read_text())["rows"]:
        if not row["id"].replace("-", "").isalnum():
            raise ValueError("invalid line ID")
        Path(row["reference"]).resolve().relative_to((ROOT / ".local").resolve())
        with wave.open(row["reference"], "rb") as f:
            if (f.getnchannels(), f.getframerate(), f.getsampwidth()) != (1, 24000, 2):
                raise ValueError("expected mono 24kHz PCM16")
            pcm = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2").astype(np.float32) / 32768
        voice = Voice(array.array("f", pcm), row["ref_text"], "icl")
        backend.prepare(voice)
        for steps in a.steps:
            backend.adapter = StepAdapter(original, steps)
            backend.mx.random.seed(42)
            voice.gain = None
            shaper = Shaper(voice.target_rms, None, sentence_end=True)
            start = time.perf_counter()
            chunks = []
            first = None
            for chunk in backend.generate(voice, row["text"], 0.32):
                if first is None:
                    first = time.perf_counter() - start
                shaper.gain = voice.gain
                chunks.extend(shaper.process(chunk))
            seconds = time.perf_counter() - start
            path = a.out / f"{row['id']}-steps{steps}.wav"
            with wave.open(str(path), "wb") as f:
                f.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
                f.writeframes(np.rint(np.clip(chunks, -1, 1) * 32767).astype("<i2").tobytes())
            records.append(dict(id=row["id"], steps=steps, first_s=first, generation_s=seconds,
                                audio_s=len(chunks) / 24000, file=path.name))
            (a.out / "results.json").write_text(json.dumps(dict(model=backend.name, rows=records,
                scope="isolated TTS, matched directed input, no identity qualification"), indent=2) + "\n")
            print(json.dumps(records[-1]), flush=True)


if __name__ == "__main__":
    main()
