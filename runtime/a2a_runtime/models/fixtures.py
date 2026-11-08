"""Deterministic adapters for CI, replay, and plumbing tests. Port of
apple/Sources/A2AModels/FixtureAdapters.swift. They model latency but no model quality;
reports must never cite them as model results."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ..core.types import (ASRHypothesis, ASRToken, AudioFrame16k, PCMFormat, SourceSpan, SpeechEnd, SpeechStart,
                          TranslationCandidate, TranslationRequest)
from .base import SynthesisCapabilities, SynthesizedAudio


@dataclass(frozen=True)
class LatencyModel:
    """Modeled compute time, applied by drivers either virtually or by sleeping."""

    fixed_ms: float = 0.0
    per_unit_ms: float = 0.0

    def nanoseconds(self, units: int) -> int:
        return int(max(0.0, self.fixed_ms + self.per_unit_ms * units) * 1_000_000)


# MARK: Scripted recognizer


def load_asr_script(path: str | Path) -> list[dict]:
    """`{"hypotheses":[{"at_sample":96000,"utt":1,"text":"我们","final":false,"tokens":[...]}]}`"""
    return list(json.loads(Path(path).read_text(encoding="utf-8"))["hypotheses"])


class NullRecognizer:
    """Direct-audio sessions send spans to the speech translator, with no separate ASR stage."""

    name = "audio"
    pending = False
    failure = None

    def accept(self, frame):
        pass

    def request_finalize(self):
        pass

    def drain_hypotheses(self):
        return []

    def reset(self):
        pass


class ScriptedRecognizer:
    """Replays logged hypotheses once the accepted audio frontier reaches their time. It
    cannot emit a hypothesis for audio it has not received."""

    name = "scripted"

    def __init__(self, entries: list[dict]) -> None:
        self._entries = sorted(entries, key=lambda e: e["at_sample"])
        self._next = 0
        self._frontier = 0
        self._pending: list[ASRHypothesis] = []
        self.finalize_requests = 0

    def accept(self, frame: AudioFrame16k) -> None:
        self._frontier = max(self._frontier, frame.span.end)
        while self._next < len(self._entries) and self._entries[self._next]["at_sample"] <= self._frontier:
            e = self._entries[self._next]
            raw = e.get("tokens")
            if raw is None:
                tokens = (ASRToken(e["text"]),)
            else:
                tokens = tuple(ASRToken(t["text"], SourceSpan(t["start"], t["end"])
                                        if t.get("start") is not None and t.get("end") is not None else None) for t in raw)
            self._pending.append(ASRHypothesis(e["utt"], tokens, bool(e["final"]), self._frontier))
            self._next += 1

    def request_finalize(self) -> None:
        self.finalize_requests += 1

    def drain_hypotheses(self) -> list[ASRHypothesis]:
        out, self._pending = self._pending, []
        return out

    def reset(self) -> None:
        self._pending = []

    @property
    def failure(self) -> str | None:
        return None


# MARK: Dictionary translator


class DictionaryTranslator:
    """Looks up committed source text; unknown text gets a deterministic English placeholder."""

    name = "dictionary"
    supports_prefix = False
    supports_context = False

    def __init__(self, table: dict[str, str], latency: LatencyModel = LatencyModel(120, 8)) -> None:
        self.table = table
        self.latency = latency

    @staticmethod
    def load(path: str | Path | None) -> DictionaryTranslator:
        table = json.loads(Path(path).read_text(encoding="utf-8")) if path else {}
        return DictionaryTranslator(table)

    def translate(self, req: TranslationRequest) -> TranslationCandidate:
        key = req.uncommitted_source.strip(" ")
        from ..core.text import char_count

        text = self.table.get(key, f"Segment {req.unit_id.raw_value} with {char_count(key)} characters.")
        return TranslationCandidate(req.unit_id, text)


# MARK: Tone synthesizer


class ToneSynthesizer:
    """Emits a pitched tone whose length follows the English text, in 100 ms chunks.
    Plumbing only: it preserves no speaker identity."""

    name = "tone"

    def __init__(self, sample_rate: int = 24_000, seconds_per_word: float = 0.32, chunk_seconds: float = 0.1,
                 first_chunk: LatencyModel = LatencyModel(180), real_time_factor: float = 0.4) -> None:
        self.sample_rate = sample_rate
        self.seconds_per_word = seconds_per_word
        self.chunk_seconds = chunk_seconds
        self.first_chunk = first_chunk
        self.real_time_factor = real_time_factor

    capabilities = SynthesisCapabilities(audio_output_streaming=True, incremental_text_input=False,
                                         continuation_across_units=False, preserves_speaker_identity=False)

    def duration(self, text: str) -> float:
        return max(0.3, max(1, len(text.split())) * self.seconds_per_word)

    def synthesize(self, unit: int, text: str, target_seconds: float | None = None):
        rate = self.sample_rate
        total = int(self.duration(text) * rate)
        chunk = int(self.chunk_seconds * rate)
        f0 = 140.0 + (unit % 5) * 20
        fmt = PCMFormat(rate, 1)
        offset = 0
        while offset < total:
            n = min(chunk, total - offset)
            t = (offset + np.arange(n)) / rate
            envelope = np.minimum(1.0, np.minimum(t, total / rate - t) / 0.02)
            block = (0.2 * envelope * np.sin(2 * math.pi * f0 * t)).astype(np.float32)
            yield SynthesizedAudio(block, fmt)
            offset += n


# MARK: Energy VAD


class EnergyVAD:
    """RMS threshold VAD with hangover on 20 ms frames. Events are canonical source samples."""

    name = "energy"

    def __init__(self, threshold_db: float = -40, min_speech_ms: int = 60, hangover_ms: int = 300) -> None:
        self.threshold_db = np.float32(threshold_db)
        self.min_speech_ms = min_speech_ms
        self.hangover_ms = hangover_ms
        self._in_speech = False
        self._speech_run_ms = 0
        self._silence_run_ms = 0
        self._run_start = 0
        self._last_voiced = 0

    @property
    def last_voiced_sample(self) -> int:
        return self._last_voiced

    @property
    def voiced_end(self) -> int | None:
        return self._last_voiced if self._in_speech else None

    def process(self, frame: AudioFrame16k) -> list:
        samples = np.asarray(frame.samples, dtype=np.float32)
        if not len(samples):
            return []
        level = float(np.sqrt(np.sum(samples * samples, dtype=np.float32) / np.float32(len(samples))))
        db = 20 * math.log10(level) if level > 0 else -120.0
        frame_ms = len(samples) * 1000 // 16_000
        events = []
        if db >= self.threshold_db:
            if self._speech_run_ms == 0:
                self._run_start = frame.span.start
            self._speech_run_ms += frame_ms
            self._silence_run_ms = 0
            self._last_voiced = frame.span.end
            if not self._in_speech and self._speech_run_ms >= self.min_speech_ms:
                self._in_speech = True
                events.append(SpeechStart(self._run_start))
        else:
            self._speech_run_ms = 0
            if self._in_speech:
                self._silence_run_ms += frame_ms
                if self._silence_run_ms >= self.hangover_ms:
                    self._in_speech = False
                    events.append(SpeechEnd(self._last_voiced))
        return events
