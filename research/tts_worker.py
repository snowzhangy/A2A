"""Resident reference-conditioned TTS worker for a2a-pipeline (`--tts clone`).

One process keeps the selected verified model resident on MLX and delivers PCM for one
unit at a time. The host (A2AModels.CloneVoiceSynthesizer) talks to it over stdin/stdout
with length-prefixed frames:

    <u32 header bytes, little endian> <u32 payload bytes> <header JSON> <payload>

Requests:  prepare {voice, text, mode: icl|xvector, rate, language: english|chinese}
                      payload = f32le mono reference PCM (language defaults to english)
           synth   {id, voice, text, target_s?, fill?}     cancel {id}    quit {}
                      `target_s` is the seconds of source speech the text translates; with
                      `fill` the worker may also slow a take that runs short of it, so the line
                      ends with the actor instead of leaving their tail silent.
Events:    ready {model, load_s, warmup_s}   prepared {voice, seconds}
           chunk {id, rate}  payload = f32le mono PCM      done {id, canceled, audio_s, gen_s, first_s,
                                                                  budget_s, generated_s, truncated,
                                                                  retries}
           error {id?, voice?, message}

Stdin EOF ends the process immediately, so a killed host never leaves the model resident.
Reference audio and text stay in memory; nothing is written to disk. Messages carry no
text unless the host passes --research-text. `--fake` replaces the model with a tone
generator using the standard library only, for deterministic adapter tests.
"""
from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import queue
import struct
import subprocess
import sys
import threading
import time

# The protocol owns the original stdout. Anything a library prints goes to stderr.
_PROTOCOL_FD = os.dup(1)
os.dup2(2, 1)
sys.stdout = sys.stderr

RATE = 24_000
TOKENS_PER_SECOND = 12.5
# Two profile versions for each of up to four tracked speakers (--speakers auto).
MAX_VOICES = 8

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audio_pacing  # noqa: E402  (NumPy-optional; pacing is skipped without it)


class Channel:
    def __init__(self) -> None:
        self.out = os.fdopen(_PROTOCOL_FD, "wb", buffering=0)
        self.lock = threading.Lock()
        self.inp = sys.stdin.buffer

    def send(self, header: dict, payload: bytes = b"") -> None:
        data = json.dumps(header, ensure_ascii=False).encode()
        with self.lock:
            self.out.write(struct.pack("<II", len(data), len(payload)) + data + payload)

    def _read_exact(self, n: int) -> bytes | None:
        chunks = []
        while n:
            block = self.inp.read(n)
            if not block:
                return None
            chunks.append(block)
            n -= len(block)
        return b"".join(chunks)

    def receive(self) -> tuple[dict, bytes] | None:
        prefix = self._read_exact(8)
        if prefix is None:
            return None
        header_len, payload_len = struct.unpack("<II", prefix)
        if header_len > 1 << 20 or payload_len > 64 << 20:
            raise ValueError("frame exceeds limits")
        header = self._read_exact(header_len)
        payload = self._read_exact(payload_len) if payload_len else b""
        if header is None or payload is None:
            return None
        return json.loads(header), payload


# MARK: Signal helpers (standard library only; chunks are well under a second)

FRAME = RATE // 100  # 10 ms


def voiced_rms(pcm: array.array, floor_db: float = -45.0) -> float | None:
    """RMS over 10 ms frames louder than floor_db, or None when nothing is voiced."""
    floor = 10 ** (floor_db / 20)
    total, count = 0.0, 0
    for start in range(0, len(pcm) - FRAME + 1, FRAME):
        energy = sum(x * x for x in pcm[start:start + FRAME]) / FRAME
        if energy >= floor * floor:
            total += energy * FRAME
            count += FRAME
    return math.sqrt(total / count) if count else None


def last_voiced_end(pcm: array.array, floor: float) -> int:
    """Index just past the last 10 ms frame whose peak reaches floor (0 if none)."""
    for start in range((len(pcm) - 1) // FRAME * FRAME, -1, -FRAME):
        if max(abs(x) for x in pcm[start:start + FRAME]) >= floor:
            return min(len(pcm), start + FRAME)
    return 0


def first_voiced_start(pcm: array.array, floor: float) -> int | None:
    for start in range(0, len(pcm), FRAME):
        if max(abs(x) for x in pcm[start:start + FRAME]) >= floor:
            return start
    return None


class Shaper:
    """Per-unit leading/trailing silence trim and loudness match, with no added latency.

    Leading silence is dropped up to a short pre-roll. The quiet tail of each chunk is held
    back and released only if more voiced audio follows, so trailing silence never reaches
    the host beyond the post-roll: a sentence keeps a sentence-length pause, a clause flows
    into the next unit. One gain per unit avoids pumping inside a sentence.
    """

    PRE_ROLL = RATE * 30 // 1000
    POST_ROLL = RATE * 60 // 1000
    SENTENCE_POST_ROLL = RATE * 150 // 1000
    FLOOR = 10 ** (-42 / 20)

    def __init__(self, target_rms: float | None, gain_hint: float | None, sentence_end: bool = False) -> None:
        self.target = target_rms
        self.gain = gain_hint
        self.post_roll = self.SENTENCE_POST_ROLL if sentence_end else self.POST_ROLL
        self.started = False
        self.pending = array.array("f")
        self.voiced_energy = 0.0
        self.voiced_frames = 0

    def process(self, pcm: array.array) -> array.array:
        if not self.started:
            first = first_voiced_start(pcm, self.FLOOR)
            if first is None:
                return array.array("f")
            pcm = pcm[max(0, first - self.PRE_ROLL):]
            self.started = True
            if self.gain is None and self.target:
                # Without a calibrated level, the first chunk's onset is a poor estimate: stay near unity.
                level = voiced_rms(pcm)
                self.gain = min(2.0, max(0.5, self.target / level)) if level else 1.0
        level = voiced_rms(pcm)
        if level:
            self.voiced_energy += level * level * len(pcm)
            self.voiced_frames += len(pcm)
        joined = self.pending + pcm
        end = last_voiced_end(joined, self.FLOOR)
        cut = min(len(joined), end + self.post_roll) if end else 0
        self.pending = joined[cut:]
        return self.apply(joined[:cut])

    def apply(self, pcm: array.array) -> array.array:
        gain = min(4.0, max(0.25, self.gain or 1.0))
        out = array.array("f", pcm)
        for i, x in enumerate(out):
            y = x * gain
            # Soft knee above 0.9 keeps peaks below full scale without a hard clip.
            if abs(y) > 0.9:
                y = math.copysign(0.9 + 0.1 * math.tanh((abs(y) - 0.9) / 0.1), y)
            out[i] = y
        return out

    def unit_gain(self) -> float | None:
        """Gain that would have matched this unit's level, for the next unit's estimate."""
        if not self.target or not self.voiced_frames:
            return None
        return self.target / math.sqrt(self.voiced_energy / self.voiced_frames)


# MARK: Backends

class Voice:
    def __init__(self, pcm: array.array, text: str, mode: str, language: str = "english") -> None:
        if language not in ("english", "chinese"):
            raise ValueError("language must be english or chinese")
        self.pcm = pcm
        self.text = text
        self.mode = mode
        self.language = language
        # In-memory only. A speaker's sampling must not depend on which other actor
        # spoke first, cancellation, or how many jobs consumed the shared MLX RNG.
        digest = hashlib.sha256(pcm.tobytes() + mode.encode() + b"\0" + language.encode()).digest()
        self.sampling_seed = int.from_bytes(digest[:4], "little")
        self.target_rms = voiced_rms(pcm)
        self.gain: float | None = None
        self.seconds_per_character = 0.25 if language == "chinese" else 0.075
        self.native = None  # backend-specific prepared reference


# Slowest speech worth emitting, in characters per second. Real pace on this model is 4-5
# characters per second in Mandarin and 13-15 in English; measured on a live session, lines that
# came out at about 2 characters per second doubled the queue and the session caught up by
# skipping other lines (reports/week2/live-session-recovery.md). A unit that reaches this pace
# stops where it is: one truncated tail costs less than a backlog nobody hears.
MIN_CHARS_PER_SECOND = {"chinese": 3.2, "english": 11.0}
# Room at the end for the trailing silence the shaper trims and one breath of decay.
SPEECH_TAIL_SECONDS = 0.75
# A take that has produced this much audio without one voiced frame is a failed start, not a
# slow one. On a live en→zh session, 9 of 213 short Mandarin lines (2–10 characters, often
# shortened by the target budget) came back as near-silence until the token cap: zero shaped
# audio, a skipped line, and about a second of generation spent on nothing. The take stops
# there and is generated once more with a different sampling seed. The per-voice seed is
# fixed, so retrying the same request would reproduce the same silent take.
SILENT_TAKE_SECONDS = 1.0
TAKE_ATTEMPTS = 2


def speech_budget_seconds(text: str, language: str = "english") -> float:
    """Longest audio this text may produce, from its character count alone."""
    characters = max(1, len(text.strip()))
    return characters / MIN_CHARS_PER_SECOND.get(language, 11.0) + SPEECH_TAIL_SECONDS


def max_tokens_for(text: str, language: str = "english") -> int:
    """Token cap that enforces `speech_budget_seconds` (missed EOS cannot run past it)."""
    return max(25, min(500, math.ceil(speech_budget_seconds(text, language) * TOKENS_PER_SECOND)))


def take_seed(voice_seed: int, attempt: int) -> int:
    """Sampling seed of one take: the voice's own seed first, a fixed offset per retry."""
    return (voice_seed + attempt * 0x9E3779B1) & 0xFFFFFFFF


def retry_text(text: str, language: str) -> str:
    """Model input for a retried take. A short fragment cut without final punctuation is where
    takes failed to start; a full stop gives the model an utterance end. It is not spoken."""
    if text[-1] in ".!?。！？…,，、;；:：":
        return text
    return text + ("。" if language == "chinese" else ".")


class FakeBackend:
    name = "fake-tone"

    def __init__(self, model_key: str = "qwen") -> None:
        self.streaming = model_key == "qwen"

    def load(self) -> None:
        pass

    def prepare(self, voice: Voice) -> None:
        voice.native = 220.0 + (len(voice.pcm) % 7) * 20

    def generate(self, voice: Voice, text: str, interval: float, attempt: int = 0):
        units = len(text) if voice.language == "chinese" else len(text.split())
        seconds = 0.12 + 0.05 * units
        step = int(RATE * interval)
        total = int(RATE * seconds)
        lead = RATE // 20  # leading silence the shaper must trim
        if not self.streaming:
            step = total + lead
        for start in range(0, total + lead, step):
            time.sleep(interval * 0.25)  # stream slower than instant so cancellation is observable
            out = array.array("f")
            for i in range(start, min(total + lead, start + step)):
                out.append(0.0 if i < lead else 0.2 * math.sin(2 * math.pi * voice.native * i / RATE))
            yield out


class ModelBackend:
    def __init__(self, model_key: str, temperature: float | None, reference_transcriber: str | None = None, reference_locale: str = "zh-CN") -> None:
        self.reference_transcriber = reference_transcriber
        self.reference_locale = reference_locale
        self.model_key = model_key
        self.temperature = temperature
        self.model = None
        self.mx = None
        self.np = None
        self.name = ""

    def load(self) -> None:
        # Offline before importing model libraries; setup owns downloads (D8).
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["TOKENIZERS_PARALLELISM"] = "false"
        from voice_clone import verified_model  # verifies every manifest hash
        from voice_models import selection
        from tts_model_adapters import load_adapter

        import mlx.core as mx
        import numpy as np
        entries = selection(self.model_key)
        paths = {entry["id"]: verified_model(entry["id"])[0] for entry in entries}
        entry = entries[-1]
        path = paths[entry["id"]]
        self.mx = mx
        self.np = np
        self.name = f"{entry['id']}@{entry['converted']['revision'][:12]}"
        self.adapter = load_adapter(self.model_key, path, paths, mx)
        self.model = self.adapter.model
        self.streaming = self.adapter.streaming

    def prepare(self, voice: Voice) -> None:
        if self.model_key in ("audio8", "omnivoice") and not voice.text.strip():
            if not self.reference_transcriber:
                raise ValueError("this model needs a local reference transcriber")
            result = subprocess.run(
                [self.reference_transcriber, "--stdin-f32", "--rate", str(RATE), "--locale", self.reference_locale],
                input=voice.pcm.tobytes(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=60, check=True)
            if len(result.stdout) > 65536:
                raise ValueError("reference transcription exceeded its size limit")
            voice.text = str(json.loads(result.stdout).get("text", "")).strip()
            if not voice.text:
                raise ValueError("no words detected in the speaker reference")
        voice.native = self.adapter.prepare(voice)
        if self.model_key in ("qwen", "omnivoice", "audio8"):
            # Global warmup compiles generation. Reference preparation primes codec
            # and speaker conditioning directly; an extra synthesized sentence delays
            # every new speaker and profile upgrade. Real units calibrate level/pace.
            return
        # Remaining backends calibrate output level and pacing with a warm-up sentence.
        warm = array.array("f")
        warm_text = "好的，我们开始吧。" if voice.language == "chinese" else "Okay, let's begin."
        for pcm in self.generate(voice, warm_text, 2.0):
            warm.extend(pcm)
        level = voiced_rms(warm)
        if level and voice.target_rms:
            voice.gain = voice.target_rms / level
        # Calibrate pacing on shaped audio, as synthesize() measures it: the raw warm-up
        # includes leading and trailing silence and would over-speed the first units.
        shaped = len(Shaper(None, 1.0).process(warm)) or len(warm)
        voice.seconds_per_character = shaped / RATE / len(warm_text)

    def generate(self, voice: Voice, text: str, interval: float, attempt: int = 0):
        mx = self.mx
        if self.model_key == "qwen":
            # Deterministic per voice and attempt: a retry of a failed take samples differently.
            mx.random.seed(take_seed(voice.sampling_seed, attempt))
        for result in self.adapter.generate(voice, text, interval, max_tokens_for(text, voice.language), self.temperature):
            mx.eval(result.audio)
            samples = self.np.asarray(result.audio, dtype=self.np.float32).reshape(-1)
            if result.sample_rate != RATE:
                from mlx_audio.resample import resample_audio_array
                samples = resample_audio_array(samples, result.sample_rate, RATE)
            pcm = array.array("f")
            pcm.frombytes(samples.tobytes())
            # Whole-phrase models already have the complete waveform. Calibrate the
            # first unit from its voiced level before chunking, avoiding an extra
            # warm-up utterance and an unreliable estimate from a quiet onset.
            if not self.streaming and voice.gain is None and voice.target_rms:
                level = voiced_rms(pcm)
                if level:
                    voice.gain = voice.target_rms / level
            # Bound IPC frame size even for models returning a completed phrase.
            step = max(1, int(RATE * interval))
            for offset in range(0, len(pcm), step):
                yield pcm[offset:offset + step]

    def estimated_duration(self, voice: Voice, text: str):
        estimate = getattr(self.adapter, "estimated_duration", None)
        return estimate(voice, text) if estimate else None

    def warmup(self) -> None:
        # Compile Metal kernels and fill allocator caches before the first real unit.
        tone = array.array("f", (0.1 * math.sin(2 * math.pi * 180 * i / RATE) * (1 + 0.3 * math.sin(i / 900))
                                 for i in range(RATE * 3)))
        # Both conditioning paths: ICL also compiles the reference codec encoder.
        for mode in (("icl", "xvector") if self.model_key == "qwen" else ("icl",)):
            voice = Voice(tone, "Warm up.", mode)
            self.prepare(voice)
            for _ in self.generate(voice, "Warm up.", 0.32):
                pass


# MARK: Worker

class Worker:
    def __init__(self, backend, channel: Channel, interval: float, research_text: bool) -> None:
        self.backend = backend
        self.channel = channel
        self.interval = interval
        self.research_text = research_text
        self.voices: dict[str, Voice] = {}
        self.jobs: queue.Queue = queue.Queue()
        self.canceled: set[int] = set()
        self.lock = threading.Lock()

    def message(self, error: BaseException) -> str:
        return f"{type(error).__name__}: {error}" if self.research_text else type(error).__name__

    def read_loop(self) -> None:
        try:
            while True:
                frame = self.channel.receive()
                if frame is None:
                    os._exit(0)  # host closed stdin or died: never leave the model resident
                header, payload = frame
                op = header.get("op")
                if op == "cancel":
                    with self.lock:
                        self.canceled.add(int(header["id"]))
                elif op in ("prepare", "synth"):
                    self.jobs.put((header, payload))
                elif op == "quit":
                    os._exit(0)
                else:
                    self.channel.send({"ev": "error", "message": f"unknown op {op!r}"})
        except Exception as error:  # malformed frame: the stream cannot be resynchronized
            sys.stderr.write(f"tts_worker: protocol error: {self.message(error)}\n")
            os._exit(3)

    def is_canceled(self, job: int) -> bool:
        with self.lock:
            return job in self.canceled

    def run(self) -> None:
        threading.Thread(target=self.read_loop, daemon=True).start()
        while True:
            header, payload = self.jobs.get()
            if header["op"] == "prepare":
                self.prepare(header, payload)
            else:
                self.synthesize(header)

    def prepare(self, header: dict, payload: bytes) -> None:
        key = str(header["voice"])
        try:
            if int(header.get("rate", RATE)) != RATE:
                raise ValueError("reference must be 24 kHz mono")
            pcm = array.array("f")
            pcm.frombytes(payload)
            seconds = len(pcm) / RATE
            if not 1.0 <= seconds <= 15.0:
                raise ValueError(f"reference is {seconds:.2f} s; use 1–15 s")
            mode = header.get("mode", "icl")
            text = str(header.get("text", "")).strip()
            if mode == "icl" and not text:
                raise ValueError("icl mode needs the reference transcript")
            voice = Voice(pcm, text, mode, str(header.get("language", "english")))
            started = time.perf_counter()
            self.backend.prepare(voice)
            self.voices[key] = voice
            while len(self.voices) > MAX_VOICES:
                self.voices.pop(next(iter(self.voices)))
            self.channel.send({"ev": "prepared", "voice": key, "seconds": seconds,
                               "prepare_s": time.perf_counter() - started, "mode": mode,
                               "language": voice.language})
        except Exception as error:
            self.channel.send({"ev": "error", "voice": key, "message": self.message(error)})

    def synthesize(self, header: dict) -> None:
        job = int(header["id"])
        started = time.perf_counter()
        first = None
        emitted = 0
        try:
            voice = self.voices.get(str(header["voice"]))
            if voice is None:
                raise KeyError("voice not prepared")
            text = str(header["text"]).strip()
            if not text:
                raise ValueError("empty text")
            pacing = None
            compactor = None
            speed = 1.0
            budget_s = speech_budget_seconds(text, voice.language)
            if header.get("target_s") is not None and audio_pacing.available:
                estimate = getattr(self.backend, "estimated_duration", None)
                predicted = estimate(voice, text) if estimate else None
                speed = audio_pacing.choose_speed(text, header.get("target_s"), voice.seconds_per_character,
                                                  predicted, allow_fill=bool(header.get("fill")))
            # Never past the budget: a two-character line still gets its whole budget on the last take.
            silent_limit = int(RATE * min(SILENT_TAKE_SECONDS, budget_s - 0.1))
            retries = 0
            for attempt in range(TAKE_ATTEMPTS):
                # A silent take emitted nothing, so every take starts from a clean shaper and pacer.
                shaper = Shaper(voice.target_rms, voice.gain, sentence_end=text[-1] in ".!?。！？")
                if header.get("target_s") is not None and audio_pacing.available:
                    pacing = audio_pacing.StreamingWSOLA(speed)
                    compactor = audio_pacing.QuietRunCompactor()
                native_samples = 0
                generated_samples = 0
                silent = False
                last_take = attempt + 1 == TAKE_ATTEMPTS
                if attempt:
                    retries += 1
                    generator = self.backend.generate(voice, retry_text(text, voice.language), self.interval,
                                                      attempt=attempt)
                else:
                    generator = self.backend.generate(voice, text, self.interval)
                try:
                    for pcm in generator:
                        if self.is_canceled(job):
                            break
                        generated_samples += len(pcm)
                        if shaper.gain is None and voice.gain is not None:
                            shaper.gain = voice.gain
                        out = shaper.process(pcm)
                        native_samples += len(out)
                        if not shaper.started and not last_take and generated_samples >= silent_limit:
                            silent = True
                            break
                        if compactor is not None:
                            out = compactor.process(out)
                            out = pacing.process(out)
                        if len(out):
                            if first is None:
                                first = time.perf_counter() - started
                            emitted += len(out)
                            self.channel.send({"ev": "chunk", "id": job, "rate": RATE}, out.tobytes())
                finally:
                    generator.close()
                if not silent or self.is_canceled(job):
                    break
            canceled = self.is_canceled(job)
            if not canceled and pacing is not None:
                tail = pacing.process(compactor.process([], final=True), final=True)
                if len(tail):
                    if first is None:
                        first = time.perf_counter() - started
                    emitted += len(tail)
                    self.channel.send({"ev": "chunk", "id": job, "rate": RATE}, tail.tobytes())
                if generated_samples and native_samples:
                    # Pace of the model's own audio, silence trim included: the estimate the next
                    # unit's speed-up is chosen from. A take with no speech says nothing about
                    # pace; counting its cap-length silence would speed up the following lines.
                    observed = generated_samples / RATE / max(1, len(text))
                    voice.seconds_per_character = 0.7 * voice.seconds_per_character + 0.3 * observed
            # Reaching the budget means the cap stopped generation, not the model: the line was
            # truncated. It is reported rather than hidden, so a run can be read correctly.
            truncated = not canceled and generated_samples / RATE >= budget_s - 0.05
            if not canceled:
                gain = shaper.unit_gain()
                if gain:
                    voice.gain = gain if voice.gain is None else 0.5 * voice.gain + 0.5 * gain
            self.channel.send({"ev": "done", "id": job, "canceled": canceled, "audio_s": emitted / RATE,
                               "gen_s": time.perf_counter() - started, "first_s": first,
                               "language": voice.language,
                               "pace": speed, "native_s": native_samples / RATE,
                               "budget_s": budget_s, "generated_s": generated_samples / RATE,
                               "truncated": truncated, "retries": retries,
                               "quiet_samples_removed": compactor.removed if compactor else 0})
        except Exception as error:
            self.channel.send({"ev": "error", "id": job, "message": self.message(error)})
        finally:
            with self.lock:
                self.canceled.discard(job)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--model", choices=("qwen", "audio8", "omnivoice", "chatterbox"), default="qwen")
    parser.add_argument("--fake", action="store_true", help="tone generator, standard library only")
    parser.add_argument("--reference-transcriber")
    parser.add_argument("--reference-locale", default="zh-CN")
    parser.add_argument("--interval", type=float, default=0.32, help="streaming chunk interval in seconds")
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--research-text", action="store_true", help="include error text in messages")
    args = parser.parse_args()
    if not 0.08 <= args.interval <= 2.0:
        parser.error("--interval must be between 0.08 and 2 seconds")
    channel = Channel()
    backend = FakeBackend(args.model) if args.fake else ModelBackend(args.model, args.temperature, args.reference_transcriber, args.reference_locale)
    started = time.perf_counter()
    try:
        if not args.fake and args.model in ("audio8", "omnivoice"):
            if not args.reference_transcriber:
                raise RuntimeError("Audio8 and OmniVoice require a local reference transcriber")
            try:
                subprocess.run([args.reference_transcriber, "--check-assets", "--locale", args.reference_locale],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=True)
            except (OSError, subprocess.SubprocessError):
                raise RuntimeError("Install the source-language speech assets to prepare voice references") from None
        backend.load()
        loaded = time.perf_counter() - started
        if hasattr(backend, "warmup"):
            backend.warmup()
    except Exception as error:
        channel.send({"ev": "error", "message": f"load failed: {type(error).__name__}: {error}"})
        sys.exit(1)
    channel.send({"ev": "ready", "model": getattr(backend, "name", "fake"), "load_s": loaded,
                  "warmup_s": time.perf_counter() - started - loaded, "rate": RATE,
                  "streaming": backend.streaming, "selected_model": args.model})
    Worker(backend, channel, args.interval, args.research_text).run()


if __name__ == "__main__":
    main()
