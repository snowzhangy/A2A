"""The stages: recognise, decide who spoke, translate, speak.

Each one is chosen by measurement (see `bench_stages.py` and the table in README.md) and each
has a dummy so the pipeline's timing behaviour can be tested with no model and no GPU:

    SenseVoiceSmall   30 ms per utterance, RTF 0.03   — recognition is not the bottleneck
    Qwen3-ASR 0.6B    170 ms per utterance            — better text, still cheap
    Qwen3-TTS worker  0.57 s to first audio, RTF ~0.8 — the only streaming clone measured here
"""
from __future__ import annotations

import json
import pathlib
import re
import threading
import time
import typing

import numpy as np

import paths
from paths import ASR_RATE
from segmenter import Utterance

ASR_REPOS = {
    "sensevoice": "mlx-community/SenseVoiceSmall",
    "qwen3-asr-0.6b": "mlx-community/Qwen3-ASR-0.6B-8bit",
    "qwen3-asr-1.7b": "mlx-community/Qwen3-ASR-1.7B-8bit",
}
MT_REPOS = {
    "a3b": "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit",      # MoE, 3B active
    "9b": "mlx-community/Qwen3.5-9B-MLX-4bit",
}


class Glossary:
    """Names and titles the recogniser and the translator both get, plus how to say them."""

    def __init__(self, terms: dict[str, str] | None = None, pronounce: dict[str, str] | None = None):
        self.terms = terms or {}
        self.pronounce = pronounce or {}

    @classmethod
    def load(cls, path: str | pathlib.Path | None) -> "Glossary":
        if not path:
            return cls()
        data = json.loads(pathlib.Path(path).read_text())
        return cls(data.get("terms", {}), data.get("pronounce", {}))

    def hint(self) -> str:
        if not self.terms:
            return ""
        return "Names: " + ", ".join(f"{k}={v}" for k, v in list(self.terms.items())[:12])

    def speakable(self, text: str) -> str:
        for word, say in self.pronounce.items():
            text = re.sub(rf"\b{re.escape(word)}\b", say, text, flags=re.I)
        return text


class Asr:
    """Utterance -> Mandarin text. One model resident; calls are serialised by the pipeline."""

    def __init__(self, model: str = "sensevoice", glossary: Glossary | None = None):
        self.name = model
        self.glossary = glossary or Glossary()
        from mlx_audio.stt import load as load_stt

        self.model = load_stt(paths.model_from_cache(ASR_REPOS[model]))

    def __call__(self, utter: Utterance) -> tuple[str, float]:
        t = time.perf_counter()
        try:
            if self.name.startswith("qwen3-asr"):
                words = list(self.glossary.terms) or None
                text = (self.model.generate(utter.samples, language="Chinese",
                                            hotwords=words).text or "")
            else:
                text = (self.model.generate(utter.samples, language="zh").text or "")
        except TypeError:                       # this build takes no hotwords
            text = self.model.generate(utter.samples, language="zh").text or ""
        return text.strip(), time.perf_counter() - t


class Mt:
    """Mandarin -> speakable English, with the previous lines as context and a word budget.

    The budget matters more than the style instructions: Mandarin is denser than English, and a
    translation that runs 1.5x longer than the actor's line cannot be spoken in the gap, so the
    translator is told how much English it is allowed.
    """

    def __init__(self, model: str = "a3b", glossary: Glossary | None = None,
                 context_lines: int = 3):
        self.glossary = glossary or Glossary()
        self.context_lines = context_lines
        from mlx_lm import load

        self.model, self.tokenizer = load(str(paths.model_from_cache(MT_REPOS[model])))
        self._lock = threading.Lock()

    def prompt(self, text: str, words: int, context: list[str]) -> str:
        system = ("You translate subtitles for voice-over. Say what the character says in "
                  "natural spoken English — short, plain, no literary flourish. Keep the given "
                  "names. Do not answer questions or add anything. "
                  + (self.glossary.hint() + " " if self.glossary.hint() else "")
                  + f"Use at most {words} words. Reply with the English only.")
        block = "\n".join(context[-self.context_lines:]) if context else ""
        user = (f"Earlier lines:\n{block}\n\n" if block else "") + f"Speak this line in English:\n{text}"
        return self.tokenizer.apply_chat_template(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            add_generation_prompt=True)

    def __call__(self, text: str, words: int,
                 context: list[str] | None = None) -> tuple[str, float, float]:
        """Returns (english, seconds, seconds to the first word)."""
        from mlx_lm import stream_generate

        ids = self.prompt(text, words, context or [])
        out, t, first = [], time.perf_counter(), None
        with self._lock:
            for piece in stream_generate(model=self.model, tokenizer=self.tokenizer,
                                         prompt=ids, max_tokens=48):
                if first is None:
                    first = time.perf_counter() - t
                out.append(piece.text)
                if len(out[-1].split()) > words * 2:      # it stopped obeying the budget
                    break
        return " ".join("".join(out).split()), time.perf_counter() - t, first or 0.0


class Speakers:
    """Who is speaking, from a speaker embedding, and which clone voice they got.

    Embeddings are campplus on CPU (a few ms per utterance). Clustering is online and
    conservative: a new voice is only declared when the match is poor against every known
    speaker, because a wrong merge takes a character's voice away for the rest of the episode.
    """

    def __init__(self, threshold: float = 0.42, min_seconds: float = 0.6):
        import sherpa_onnx as so

        self.threshold = threshold
        self.min_seconds = min_seconds
        self.extractor = so.SpeakerEmbeddingExtractor(
            config=so.SpeakerEmbeddingExtractorConfig(model=str(paths.EMBED_MODEL), num_threads=1))
        self.centroids: dict[str, np.ndarray] = {}
        self.counts: dict[str, int] = {}

    def embed(self, samples: np.ndarray, rate: int = ASR_RATE) -> np.ndarray | None:
        if len(samples) < self.min_seconds * rate:
            return None
        stream = self.extractor.create_stream()
        stream.accept_waveform(rate, np.asarray(samples, dtype=np.float32))
        stream.input_finished()
        vec = np.asarray(self.extractor.compute(stream), dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        return vec / norm if norm > 0 else None

    def assign(self, vec: np.ndarray | None) -> str:
        if vec is None:                      # too short to judge: keep it with the first voice
            return "s0"
        best, score = "", -1.0
        for key, centroid in self.centroids.items():
            s = float(np.dot(vec, centroid))
            if s > score:
                best, score = key, s
        if best and score >= self.threshold:
            n = self.counts[best]
            self.centroids[best] = (centroid * n + vec) / (n + 1)
            nrm = float(np.linalg.norm(self.centroids[best]))
            self.centroids[best] /= nrm
            self.counts[best] = n + 1
            return best
        key = f"s{len(self.centroids)}"
        self.centroids[key] = vec
        self.counts[key] = 1
        return key

    def who(self) -> dict[str, int]:
        return dict(self.counts)


class Speak:
    """The clone, streamed: A2A's resident worker with one prepared voice per speaker."""

    def __init__(self, engine: str = "qwen", temperature: float = 0.4, log=None):
        from ttsd import TtsWorker

        self.worker = TtsWorker(engine, temperature=temperature, log=log)
        ready = self.worker.wait_ready()
        self.load_s = ready.get("load_s")
        self.warmup_s = ready.get("warmup_s")
        self.prepared: set[str] = set()

    def prepare(self, voice: str, reference: np.ndarray, rate: int, text: str,
                language: str = "chinese") -> dict:
        from ttsd import RATE

        info = self.worker.prepare(voice, paths.resample(reference, rate, RATE), text,
                                   language=language, mode="icl" if text.strip() else "xvector")
        self.prepared.add(voice)
        return info

    @property
    def rate(self) -> int:
        return self.worker.rate

    def stream(self, voice: str, text: str,
               target_s: float | None = None) -> typing.Iterator[np.ndarray]:
        return self.worker.synth(voice, text, target_s)

    @property
    def stats(self) -> dict:
        return self.worker.stats

    def close(self) -> None:
        self.worker.close()


class DummySpeak:
    """A tone that lasts as long as the English would: tests the scheduler without a model."""

    def __init__(self, words_per_second: float = 2.6, load_s: float = 0.0, warmup_s: float = 0.0,
                 first_s: float = 0.0, rtf: float = 0.0, rate: int = 24_000):
        self.words_per_second = words_per_second
        self.load_s, self.warmup_s = load_s, warmup_s
        self.first_s, self.rtf, self.rate = first_s, rtf, rate
        self.prepared: set[str] = set()
        self.stats: dict = {}

    def prepare(self, voice: str, reference, rate, text, language: str = "chinese") -> dict:
        self.prepared.add(voice)
        return {"voice": voice, "seconds": len(reference) / rate, "prepare_s": 0.0}

    def stream(self, voice: str, text: str,
               target_s: float | None = None) -> typing.Iterator[np.ndarray]:
        seconds = target_s or max(0.4, len(text.split()) / self.words_per_second)
        total = int(seconds * self.rate)
        if self.first_s:
            time.sleep(self.first_s)
        step = int(0.32 * self.rate)
        done = 0
        t = time.perf_counter()
        while done < total:
            n = min(step, total - done)
            yield (0.2 * np.sin(2 * np.pi * 220 * np.arange(done, done + n) / self.rate
                                )).astype(np.float32)
            done += n
            if self.rtf:
                time.sleep(n / self.rate * self.rtf)
        self.stats = {"audio_s": total / self.rate, "gen_s": time.perf_counter() - t}

    def close(self) -> None:
        pass
