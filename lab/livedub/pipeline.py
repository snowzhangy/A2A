"""Live dub: Mandarin in, cloned English out, a couple of seconds behind.

One process, two threads, one clock. The output timeline is the input timeline shifted by
`--delay`: at output time T the listener hears the input from T - delay, and the English for a
line whose actor stopped at input time t is aimed at t + delay. Every stage has to beat that
delay, and the measured costs say it fits: endpoint 0.25 s + recogniser 0.03 s + translator 0.73 s
+ first audio of the clone 0.57 s ≈ 1.6 s (README.md has the runs).

What this answers: what a 2–3 s budget actually buys. It is not a lip-synced dub — that needs a
presentation delay of 10–15 s and the picture delayed with it (APPROACHES.md). This is following
a conversation: each line spoken in the clone of the person who said it, over the ducked original,
never cut off, and dropped rather than crowded when it cannot fit.

    python pipeline.py --file ../../.local/live-dub/input/ep08-16k.wav --start 583 --end 620 \
                       --delay 2.5 [--asr sensevoice] [--mt a3b] [--tts qwen|dummy] \
                       [--stages dummy] [--out scene]

Writes .local/livedub/<out>/out.wav, captions.srt, trace.jsonl, report.json and prints the table.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import queue
import sys
import threading
import time

import numpy as np

import paths
from mixer import Line, Mixer, budget_words, fit, match_gain
from paths import ASR_RATE, MIX_RATE
from segmenter import Segmenter
from stages import Asr, DummySpeak, Glossary, Mt, Speakers

TTS_RATE = 24_000
REFERENCE_MIN = 3.0          # seconds of one speaker before their clone can be built
REFERENCE_KEEP = 8.0


class Take:
    """One line's English, streaming in, waiting to be placed in the mix."""

    def __init__(self, utter, zh: str, en: str, estimate: float, asr_s: float, mt_s: float):
        self.utter, self.zh, self.en = utter, zh, en
        self.estimate = estimate                  # expected length at normal speed
        self.asr_s, self.mt_s = asr_s, mt_s
        self.line = Line(index=utter.index, start=0.0, planned=estimate, text=en,
                         speaker=utter.speaker)
        self.first_at: float | None = None        # output clock when the first audio existed
        self.tts_s = 0.0
        self.queued = False
        self.placed = False


class Pipeline:
    def __init__(self, args):
        self.args = args
        self.glossary = Glossary.load(args.glossary)
        self.delay = args.delay
        self.mixer = Mixer(delay=args.delay, duck=args.duck)
        self.segmenter = Segmenter(min_silence=args.min_silence, max_speech=args.max_speech)
        self.speakers = Speakers()
        self.dummy = args.stages == "dummy"
        self.asr = None if self.dummy else Asr(args.asr, self.glossary)
        self.mt = None if self.dummy else Mt(args.mt, self.glossary)
        if self.dummy or args.tts == "dummy":
            self.speak = DummySpeak(args.words_per_second, rtf=args.tts_rtf)
        else:
            from stages import Speak

            self.speak = Speak(args.tts, temperature=args.temperature,
                               log=open(paths.OUT / args.out / "tts.log", "w"))
        self.tts_rate = getattr(self.speak, "rate", TTS_RATE)
        self.utterances: queue.Queue = queue.Queue()
        self.ready: queue.Queue[Take] = queue.Queue()
        self.placed: list[Take] = []
        self.context: list[str] = []
        self.voice_audio: dict[str, np.ndarray] = {}
        self.voice_text: dict[str, str] = {}
        self.lock = threading.Lock()
        self.output_time = 0.0
        self.busy = False
        self.stop = threading.Event()
        self.events: list[dict] = []
        self.thread = threading.Thread(target=self._work, daemon=True)

    # ---------------------------------------------------------------- worker

    def _work(self) -> None:
        while not self.stop.is_set():
            try:
                utter = self.utterances.get(timeout=0.05)
            except queue.Empty:
                continue
            self.busy = True
            zh, asr_s = (self.scripted(utter), 0.0) if self.dummy else self.asr(utter)
            if len(zh.strip()) < 2:
                self._event("empty", utt=utter.index, start=round(utter.start, 2))
                self.busy = False
                continue
            utter.text = zh
            utter.speaker = self.speakers.assign(self.speakers.embed(utter.samples))
            self._learn(utter)
            # The word budget is the fit lever. "slot" spends only what the actor's own window
            # holds; "wide" adds the silence we are allowed to run into afterwards.
            room = utter.duration + (self.args.spill if self.args.budget == "wide" else 0.0)
            budget = budget_words(room, self.args.words_per_second)
            if self.dummy:
                en, mt_s = self.scripted_en(utter, budget), 0.0
            else:
                en, mt_s, _ = self.mt(zh, budget, self._context())
            if not en.strip():
                self._event("no-translation", utt=utter.index, zh=zh)
                self.busy = False
                continue
            self._context(en and f"{zh} -> {en}")
            self._speak(utter, zh, en, asr_s, mt_s, budget)
            self.busy = False

    def _speak(self, utter, zh: str, en: str, asr_s: float, mt_s: float, budget: int) -> None:
        if utter.speaker not in self.speak.prepared:
            self._event("no-voice", utt=utter.index, speaker=utter.speaker, zh=zh, en=en)
            return
        estimate = max(0.5, len(en.split()) / self.args.words_per_second)
        take = Take(utter, zh, en, estimate, asr_s, mt_s)
        t0 = time.perf_counter()
        room = utter.duration + self.args.spill
        for chunk in self.speak.stream(utter.speaker, self.glossary.speakable(en), target_s=room):
            if self.stop.is_set():
                return
            take.line.buffer(paths.resample(chunk, self.tts_rate, MIX_RATE))
            if take.first_at is None:
                take.first_at = self.output_time
                take.queued = True
                self.ready.put(take)               # playback can start on the first chunk
        take.tts_s = time.perf_counter() - t0
        take.line.close()
        if take.queued:
            take.line.level = match_gain(take.line.samples,
                                         paths.resample(utter.samples, ASR_RATE, MIX_RATE))

    def _learn(self, utter) -> None:
        """A speaker's clone is built out of the first thing they say, as it arrives."""
        key = utter.speaker
        if key in self.speak.prepared:
            return
        keep = self.voice_audio.get(key)
        self.voice_audio[key] = utter.samples if keep is None else np.concatenate(
            [keep, utter.samples])[-int(REFERENCE_KEEP * ASR_RATE):]
        self.voice_text[key] = (self.voice_text.get(key, "") + " " + utter.text).strip()[-200:]
        if len(self.voice_audio[key]) >= REFERENCE_MIN * ASR_RATE:
            info = self.speak.prepare(key, self.voice_audio[key], ASR_RATE, self.voice_text[key])
            self._event("voice", speaker=key, seconds=round(info.get("seconds", 0), 2),
                        prepare_s=round(info.get("prepare_s", 0), 3))

    def _context(self, add: str | None = None) -> list[str]:
        with self.lock:
            if add:
                self.context.append(add)
                self.context = self.context[-6:]
            return list(self.context)

    def _event(self, kind: str, **fields) -> None:
        self.events.append({"ev": kind, **fields})

    # --------------------------------------------------- scripted pairs (no models)

    def _lines(self):
        p = pathlib.Path(self.args.script)
        return json.loads(p.read_text())["lines"] if p.exists() else []

    def scripted(self, utter) -> str:
        best = min(self._lines(), key=lambda r: abs((r["start"] + r["end"]) / 2 -
                                                    (utter.start + utter.end) / 2), default=None)
        if best and _overlap((utter.start, utter.end), (best["start"], best["end"])) > 0.3:
            return best["zh"]
        return ""

    def scripted_en(self, utter, budget: int) -> str:
        best = min(self._lines(), key=lambda r: abs((r["start"] + r["end"]) / 2 -
                                                    (utter.start + utter.end) / 2), default=None)
        if best and _overlap((utter.start, utter.end), (best["start"], best["end"])) > 0.3:
            return " ".join(best["text"].split()[:budget])
        return ""

    # ---------------------------------------------------------------- main loop

    def run(self, audio48: np.ndarray) -> np.ndarray:
        self.thread.start()
        frame48 = int(0.02 * MIX_RATE)
        block = int(0.01 * MIX_RATE)
        out: list[np.ndarray] = []
        started = time.perf_counter()
        read = 0
        while True:
            now = time.perf_counter() - started
            self.output_time = now
            # whatever input has arrived by now goes to the bed and the recogniser
            while read < len(audio48) and read / MIX_RATE <= now:
                chunk = audio48[read: read + frame48]
                self.mixer.add_input(chunk)
                for utter in self.segmenter.push(paths.resample(chunk, MIX_RATE, ASR_RATE)):
                    self.utterances.put(utter)
                read += len(chunk)
            self._place()
            out.append(self.mixer.block(block))
            finished = (read >= len(audio48) and self.utterances.empty()
                        and self.ready.empty() and not self.mixer.lines and not self.busy)
            if finished and self.mixer.time > len(audio48) / MIX_RATE + self.delay + 0.3:
                break
            if self.mixer.time > len(audio48) / MIX_RATE + self.delay + 20:
                break                              # the tail: stop waiting on a stuck stage
            ahead = started + self.mixer.time - time.perf_counter()
            if ahead > 0:
                time.sleep(ahead)
        self.stop.set()
        self.thread.join(timeout=5)
        self.speak.close()
        return np.concatenate(out)

    def _place(self) -> None:
        known = max([self.mixer.time] + [l.end for l in self.mixer.lines])
        while True:
            try:
                take = self.ready.get_nowait()
            except queue.Empty:
                return
            if take.placed:                         # already committed to a slot
                continue
            # At a 2–3 s delay the English cannot also fit inside the actor's own window: it is
            # 1.5x longer than the Mandarin, so it necessarily runs over later speech. That is
            # allowed — the bed is ducked — and the only hard rule left is that our own two lines
            # never speak together. `--spill` stays as the window we prefer to finish within.
            duration = max(take.line.duration, take.estimate)
            slot = fit(end_input=take.utter.end, delay=self.delay,
                       ready_at=(take.first_at or self.output_time) + 0.02,
                       duration=duration,
                       free_until=take.utter.end + self.delay + self.args.spill + duration,
                       last_end=known, max_tempo=self.args.max_tempo)
            if slot.why.startswith("skipped"):
                self._event(slot.why, utt=take.utter.index, en=take.en,
                            seconds=round(take.line.duration, 2))
                continue
            take.placed = True
            take.line.start = slot.start
            self.mixer.lines.append(take.line)
            known = max(known, slot.start + take.line.duration)
            self.placed.append(take)
            self._event("placed", utt=take.utter.index, speaker=take.utter.speaker,
                        actor_end=round(take.utter.end, 3), start=round(slot.start, 3),
                        why=slot.why, lag=round(slot.start - take.utter.end, 3),
                        seconds=round(take.line.duration, 3),
                        ready_s=round((take.first_at or 0) - take.utter.end, 3)
                        if take.first_at else None,
                        asr_s=round(take.asr_s, 3), mt_s=round(take.mt_s, 3),
                        tts_s=round(take.tts_s, 3), zh=take.zh, en=take.en)

    # ---------------------------------------------------------------- report

    def report(self) -> dict:
        placed = [e for e in self.events if e["ev"] == "placed"]
        lags = [e["lag"] for e in placed]
        speech = sum(1 for e in self.events if e["ev"] in
                     ("placed", "empty", "no-voice", "no-translation", "skipped:busy",
                      "skipped:too-long"))
        return {
            "delay_s": self.delay,
            "units": speech,
            "placed": len(placed),
            "skips": {k: sum(1 for e in self.events if e["ev"] == k)
                      for k in ("skipped:busy", "skipped:too-long", "empty", "no-voice",
                                "no-translation")},
            "lag_actor_to_english": {"p50": _p(lags, 50), "p90": _p(lags, 90),
                                     "max": round(max(lags), 3) if lags else None},
            "processing_actor_to_first_audio_s": _p([e["ready_s"] for e in placed
                                                     if e.get("ready_s") is not None], 50),
            "stages_s": {"asr_p50": _p([e["asr_s"] for e in placed], 50),
                         "mt_p50": _p([e["mt_s"] for e in placed], 50),
                         "tts_all_p50": _p([e["tts_s"] for e in placed], 50)},
            "speakers": self.speakers.who(),
            "underruns": sum(t.line.underruns for t in self.placed),
            "clipped_blocks": self.mixer.clipped,
            "events": self.events,
        }


def _overlap(a, b) -> float:
    lo, hi = max(a[0], b[0]), min(a[1], b[1])
    return max(0.0, hi - lo) / max(0.1, min(a[1] - a[0], b[1] - b[0]))


def _p(xs, q: float) -> float | None:
    return round(float(np.percentile(xs, q)), 3) if xs else None


def captions(events) -> str:
    rows = [e for e in events if e["ev"] == "placed"]
    out = []
    for n, e in enumerate(rows, 1):
        out.append(f"{n}\n{_clock(e['start'])} --> {_clock(e['start'] + e['seconds'])}\n{e['en']}\n")
    return "\n".join(out)


def _clock(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    return f"00:{int(m):02d}:{s:06.3f}".replace(".", ",")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True, help="media or wav played as if it were live")
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--end", type=float, default=0.0, help="0 = to the end of the file")
    ap.add_argument("--delay", type=float, default=2.5)
    ap.add_argument("--duck", type=float, default=0.38, help="bed gain while the dub speaks")
    ap.add_argument("--spill", type=float, default=1.2, help="how far a line may run past its actor")
    ap.add_argument("--max-tempo", type=float, default=1.18)
    ap.add_argument("--min-silence", type=float, default=0.25)
    ap.add_argument("--max-speech", type=float, default=6.0)
    ap.add_argument("--asr", default="sensevoice")
    ap.add_argument("--mt", default="a3b")
    ap.add_argument("--tts", default="qwen")
    ap.add_argument("--stages", default="models", choices=["models", "dummy"])
    ap.add_argument("--tts-rtf", type=float, default=0.0, help="dummy TTS: pretend this RTF")
    ap.add_argument("--temperature", type=float, default=0.4)
    ap.add_argument("--words-per-second", type=float, default=2.7)
    ap.add_argument("--budget", default="wide", choices=["slot", "wide"],
                    help="how much English the translator is allowed per line")
    ap.add_argument("--glossary", default="")
    ap.add_argument("--script", default=str(pathlib.Path.home() / "Documents/github/Research/"
                                            "dub-anything/work/condor-01/script.json"))
    ap.add_argument("--out", default="run")
    args = ap.parse_args()

    (paths.OUT / args.out).mkdir(parents=True, exist_ok=True)
    audio48 = paths.load(args.file, args.start, args.end or None, MIX_RATE)
    pipe = Pipeline(args)
    audio = pipe.run(audio48)
    run = paths.OUT / args.out
    paths.save(run / "out.wav", audio, MIX_RATE)
    report = pipe.report()
    (run / "report.json").write_text(json.dumps({k: v for k, v in report.items()
                                                 if k != "events"}, indent=1, ensure_ascii=False))
    (run / "trace.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False)
                                               for e in report["events"]))
    (run / "captions.srt").write_text(captions(report["events"]))
    print(json.dumps({k: v for k, v in report.items() if k != "events"}, indent=1,
                     ensure_ascii=False))
    for e in report["events"]:
        if e["ev"] == "placed":
            print(f"  {e['start']:6.2f} (+{e['lag']:.2f}s) {e['speaker']:>3}  {e['en']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
