"""Streaming utterance detection: silero VAD wrapped into timed utterances.

The VAD is CPU (sherpa-onnx ships its own onnxruntime) and costs about 0.3% of real time, so it
is not part of the latency argument — the endpoint rule is. `min_silence` is how long we wait
after the actor stops before we dare to recognise, so it is the first term in the delay budget.

Measured against the actor windows measured by hand for the offline dub of the same scene: at
0.25 s the boundaries agree to within ~0.15 s, two lines closer together than that merge into one
utterance (the gap-based alternative to cutting mid-sentence, which recognition and translation
both like), and quiet onsets begin ~0.3 s late — which `preroll` recovers.

Two things the VAD does not do for us, so this does:
* a ceiling on a run of continuous speech, which is what caps latency on a monologue;
* monotonic, non-overlapping units. Asking sherpa to cut an open segment leaves its buffer
  inconsistent (it logs `Invalid n` and re-emits audio), and a repeated unit means a repeated
  dubbed line, so every window is clamped to start after the last one ended.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from paths import ASR_RATE, VAD_MODEL


@dataclass
class Utterance:
    index: int
    start: float              # input seconds, window start after preroll
    end: float                # input seconds where the actor's voice ends
    endpoint_at: float        # input seconds when we know it ended
    samples: np.ndarray       # 16 kHz float32
    forced: bool = False      # cut by max_speech rather than by silence
    embedding: list[float] | None = None
    text: str = ""
    speaker: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def wait(self) -> float:
        """Seconds after the actor's last sample before this utterance could be worked on."""
        return self.endpoint_at - self.end


class History:
    """Recent input in a ring, addressable by absolute sample index, so windows can grow past
    the VAD's cuts. A ring rather than a growing list: this runs on every 20 ms frame."""

    def __init__(self, rate: int, keep: float = 90.0):
        self.rate = rate
        self.cap = max(1, int(keep * rate))
        self.buf = np.zeros(self.cap, dtype=np.float32)
        self.total = 0

    def append(self, frame: np.ndarray) -> None:
        n = len(frame)
        if n >= self.cap:
            self.buf[:] = frame[-self.cap:]
            self.total += n
            return
        pos = self.total % self.cap
        first = min(n, self.cap - pos)
        self.buf[pos:pos + first] = frame[:first]
        if n > first:
            self.buf[:n - first] = frame[first:]
        self.total += n

    def slice(self, start: int, stop: int) -> np.ndarray:
        start = max(start, self.total - self.cap, 0)
        stop = min(stop, self.total)
        if stop <= start:
            return np.zeros(0, dtype=np.float32)
        s, e = start % self.cap, stop % self.cap
        return np.ascontiguousarray(self.buf[s:e] if s < e
                                    else np.concatenate([self.buf[s:], self.buf[:e]]))


class Segmenter:
    """Feed 20 ms frames of 16 kHz audio; get utterances out."""

    def __init__(self, threshold: float = 0.5, min_silence: float = 0.25, min_speech: float = 0.2,
                 max_speech: float = 6.0, preroll: float = 0.20, postroll: float = 0.10,
                 rate: int = ASR_RATE):
        import sherpa_onnx as so

        self.rate = rate
        self.min_silence = min_silence
        self.min_speech = min_speech
        self.max_speech = max_speech
        self.preroll_n = int(preroll * rate)
        self.postroll_n = int(postroll * rate)
        cfg = so.VadModelConfig()
        cfg.silero_vad.model = str(VAD_MODEL)
        cfg.silero_vad.threshold = threshold
        cfg.silero_vad.min_silence_duration = min_silence
        cfg.silero_vad.min_speech_duration = min_speech
        cfg.silero_vad.max_speech_duration = max_speech
        cfg.sample_rate = rate
        self._vad = so.VoiceActivityDetector(cfg, buffer_size_in_seconds=60)
        self.hist = History(rate)
        self._since: int | None = None       # input sample where the open speech began
        self._cut = 0                         # input sample up to which units have been emitted
        self._n = 0

    # --- windows ---------------------------------------------------------------

    def _window(self, start: int, stop: int, forced: bool) -> Utterance | None:
        start = max(start, self._cut)
        stop = min(stop, self.hist.total)
        if stop - start < self.min_speech * self.rate:
            return None
        # Grow back 0.2 s to recover a soft onset, but never past the end of the unit before it:
        # audio already handed on would come back as a repeated word in the translation.
        s0 = max(0, start - self.preroll_n, self._cut)
        utter = Utterance(index=self._n, start=s0 / self.rate, end=stop / self.rate,
                          endpoint_at=self.hist.total / self.rate,
                          samples=self.hist.slice(s0, stop + self.postroll_n), forced=forced)
        self._cut = stop
        self._n += 1
        return utter

    def _split(self, utter: Utterance) -> list[Utterance]:
        """Cut an over-long utterance at the quietest points available.

        Length is the live budget: recognition, translation and speech all cost per second of
        source, and the listener waits on the whole unit."""
        if utter.duration <= self.max_speech:
            return [utter]
        rate, limit = self.rate, self.max_speech
        frame = max(1, int(0.01 * rate))
        n = len(utter.samples) // frame
        power = np.sqrt((utter.samples[: n * frame].reshape(n, frame).astype(np.float64) ** 2
                         ).mean(axis=1))
        smooth = int(0.16 * rate / frame)
        if smooth > 1:
            power = np.convolve(power, np.ones(smooth) / smooth, mode="same")
        cuts, done = [], 0
        while (len(utter.samples) - done) / rate > limit:
            low, high = done + int(0.55 * limit * rate), done + int(limit * rate)
            f0, f1 = low // frame, min(len(power) - 1, high // frame)
            cut = ((f0 + int(np.argmin(power[f0: f1 + 1]))) * frame if f1 > f0
                   else min(high, len(power) * frame))
            cuts.append(cut)
            done = cut
        pieces: list[Utterance] = []
        for a, b in zip([0, *cuts], [*cuts, len(utter.samples)]):
            if (b - a) / rate < self.min_speech:
                continue
            piece = Utterance(index=self._n, start=utter.start + a / rate,
                              end=utter.start + b / rate, endpoint_at=utter.endpoint_at,
                              samples=utter.samples[a:b], forced=True)
            piece.extra["from"] = utter.index
            pieces.append(piece)
            self._n += 1
        return pieces

    # --- streaming -------------------------------------------------------------

    def _drain(self) -> list[Utterance]:
        out: list[Utterance] = []
        while not self._vad.empty():
            front = self._vad.front
            start = int(front.start)
            samples = np.array(front.samples, dtype=np.float32, copy=True)   # front dies on pop
            self._vad.pop()
            utter = self._window(start, start + len(samples), forced=False)
            if utter is not None:
                out += self._split(utter)       # no-op unless the VAD let it run long
        return out

    def _ceiling(self, tail: bool = False) -> list[Utterance]:
        """Take a piece of an open run of speech at the ceiling.

        Waiting for a gap that may never come is how a monologue turns into a ten-second wait.
        The VAD is left completely alone here — neither flush nor reset, both of which lose
        detection for seconds in this build — and its eventual long segment is clamped to start
        where this piece ended, which yields the remainder as the next unit."""
        start = max(self._cut, self._since or self._cut)
        stop = self.hist.total if tail else min(self.hist.total, start + int(self.max_speech * self.rate))
        utter = self._window(start, stop, forced=True)
        if utter is None:
            return []
        if not tail:
            self._since = self._cut             # the next ceiling is measured from the cut
        return self._split(utter)

    def push(self, frame: np.ndarray) -> list[Utterance]:
        frame = np.asarray(frame, dtype=np.float32)
        self.hist.append(frame)
        self._vad.accept_waveform(frame)
        if self._vad.is_speech_detected():
            # Timing the open segment by asking the VAD for it copies the audio every frame;
            # watch the transition instead — same cut, 0.3% of real time rather than 1.6%.
            if self._since is None:
                self._since = self.hist.total
            since = max(self._since, self._cut)
            if self.hist.total - since >= self.max_speech * self.rate:
                return self._ceiling() + self._drain()
        else:
            self._since = None
        return self._drain()

    def finish(self) -> list[Utterance]:
        """End of input: take whatever speech is still open, then whatever the VAD is holding."""
        out = self._ceiling(tail=True) if self._vad.is_speech_detected() else []
        return out + self._drain()
