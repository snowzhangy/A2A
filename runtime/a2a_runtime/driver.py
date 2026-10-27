"""Pipeline driver: the portable counterpart of apple/Tools/a2a-pipeline/main.swift.

The driver owns I/O and adapters; SessionPipeline makes every decision. Output is rendered
through the same native two-bus mixer the live host uses. Inputs: a WAV file (virtual or
host clock), paced f32le stdin from the harness, or a live Windows app via WASAPI process
loopback. Outputs, trace events, and the closing summary match the Swift driver, so
harness/run.py, metrics, and live_check consume either.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from . import native
from .core import pipeline as P
from .core.direction import TranslationLanguage
from .core.reference_policy import ReferencePlan, ReferencePolicy, ReferencePolicyConfig, Rejected
from .core.speech_evidence import SpeechEvidence
from .core.trace import TraceEvent, TraceKind, f_int, f_str
from .core.types import (CANONICAL_SAMPLE_RATE, AudioFrame16k, PCMChunkHeader, PreparedVoice, ReferenceQuality,
                         SourceSpan, SpeakerAssign, SpeakerID, UnitID, VoiceProfile)
from .media import signal
from .media.resampler import StreamingResampler
from .media.wav import WAVStreamWriter
from .models.fixtures import DictionaryTranslator, EnergyVAD, ScriptedRecognizer, ToneSynthesizer
from .trace_writer import RTRecordDrain, TraceWriter

CANONICAL = CANONICAL_SAMPLE_RATE
# Translated audio the bus can queue: well above the fallback thresholds plus the longest phrase.
TRANSLATED_RING_SECONDS = 30.0
EDGE_FADE_SECONDS = 0.005


def log(message: str) -> None:
    sys.stderr.write(f"a2a-pipeline: {message}\n")
    sys.stderr.flush()


# MARK: Source


class Source:
    """Delivers 20 ms mono canonical chunks. Stdin is read on its own thread so the pipeline
    only sees audio that has arrived; a live tap delivers what the capture ring holds."""

    def __init__(self, options, live=None) -> None:
        self.o = options
        self.chunk = CANONICAL // 50
        self.live = live
        self.failure: str | None = None
        self.stop_requested = threading.Event()
        self._file: np.ndarray | None = None
        self._offset = 0
        self._stdin_q: queue.Queue | None = None
        self._pending = np.zeros(0, np.float32)
        if live is not None:
            self._tap_buffer = np.empty(4_096 * live.channels, np.float32)
            self._to_canonical = (StreamingResampler(live.sample_rate, CANONICAL)
                                  if live.sample_rate != CANONICAL else None)
        elif options.input:
            from .media import wav

            audio = wav.read(options.input)
            samples = audio.mono()
            if audio.sample_rate != CANONICAL:
                log(f"resampling input {audio.sample_rate} → {CANONICAL} Hz")
                samples = StreamingResampler.resample(samples, audio.sample_rate, CANONICAL)
            self._file = samples.astype(np.float32)
        else:
            if options.rate != CANONICAL:
                raise SystemExit(f"a2a-pipeline: --stdin expects {CANONICAL} Hz f32le mono")
            self._stdin_q = queue.Queue()
            threading.Thread(target=self._read_stdin, name="a2a.stdin", daemon=True).start()

    def _read_stdin(self) -> None:
        if sys.platform == "win32":
            import msvcrt

            msvcrt.setmode(0, os.O_BINARY)
        need = self.chunk * 4
        buf = b""
        while True:
            try:
                block = os.read(0, need - len(buf))
            except OSError:
                break
            if not block:
                break
            buf += block
            if len(buf) == need:
                self._stdin_q.put(np.frombuffer(buf, dtype="<f4").astype(np.float32))
                buf = b""
        if len(buf) >= 4:
            self._stdin_q.put(np.frombuffer(buf[: len(buf) // 4 * 4], dtype="<f4").astype(np.float32))
        self._stdin_q.put(None)

    @property
    def total_frames(self) -> int | None:
        return None if self._file is None else len(self._file)

    def next(self) -> np.ndarray | None:
        if self._file is not None:
            if self._offset >= len(self._file):
                return None
            n = min(self.chunk, len(self._file) - self._offset)
            out = self._file[self._offset:self._offset + n]
            self._offset += n
            return out
        return self._stdin_q.get()

    def next_live(self, until_stop: bool) -> np.ndarray | None:
        """Live chunks as the capture ring fills. With `until_stop`, returns None once
        --seconds elapse or a stop arrives; the tail reads on so playout finishes."""
        live = self.live
        while len(self._pending) < self.chunk:
            # A stop request ends the session at once; --seconds lets the tail finish playout.
            if self.stop_requested.is_set() or (until_stop and live.deadline_passed()):
                return None
            failure = live.health_check()
            if failure:
                self.failure = failure
                return None
            n = live.engine.capture.read_into(self._tap_buffer)
            if n == 0:
                if live.capture_stalled():
                    self.failure = "capture stalled; the session stopped and released its tap"
                    return None
                time.sleep(0.005)
                continue
            frames = self._tap_buffer[: n * live.channels].reshape(n, live.channels)
            mono = frames.mean(axis=1, dtype=np.float32).astype(np.float32)
            if self._to_canonical is not None:
                mono = self._to_canonical.process(mono)
            self._pending = np.concatenate([self._pending, mono])
        out, self._pending = self._pending[: self.chunk], self._pending[self.chunk:]
        return out


# MARK: Driver


@dataclass
class PlayRecord:
    unit: UnitID
    start: int | None = None
    end: int = 0
    complete: bool = False
    started: bool = False
    ended: bool = False
    underrun_reported: bool = False


@dataclass
class _Task:
    unit: UnitID
    cancelled: bool = False
    stream: object = None
    thread: threading.Thread | None = None


@dataclass(order=True)
class _Delivery:
    at: int
    seq: int
    event: tuple = field(compare=False)


class Driver:
    def __init__(self, options, recognizer, translator, synthesizer, live=None, controls=None) -> None:
        o = self.o = options
        self.virtual = o.clock == "virtual"
        self.controls = controls
        self.live = live
        config = P.PipelineConfig(session=o.session, direction=o.direction)
        config.trace_text = o.research_text
        config.emit_captions = o.events_stdout
        config.original_audio_enabled = not o.translation_only
        config.playback_mode = o.playback
        if o.fluent:
            config.commit.streaming_clauses = True
            config.finish_phrase_before_catch_up = True
            config.health.prebuffer_seconds = 0.24
        if o.max_debt_seconds is not None:
            config.health.max_debt_seconds = o.max_debt_seconds
        if o.max_source_lag_seconds is not None:
            config.health.max_source_lag_seconds = o.max_source_lag_seconds
        if o.max_unit_keys is not None:
            config.commit.max_unit_keys = o.max_unit_keys
        self.tracker = None
        if o.speakers == "auto":
            from .models.speaker_tracking import VoiceprintSpeakerTracker

            config.multi_speaker = True
            self.tracker = VoiceprintSpeakerTracker()
        self.pipeline = P.SessionPipeline(config)
        self.trace = TraceWriter(o.trace)
        self.vad = vad_for(o)
        self.speech_evidence = SpeechEvidence()
        self.to16k = StreamingResampler(CANONICAL, 16_000)
        self.recognizer = recognizer
        self.translator = translator
        self.synthesizer = synthesizer
        self.translator_latency = getattr(translator, "latency", None) if isinstance(translator, DictionaryTranslator) else None
        if live is not None:
            self.engine = live.engine
            self.out_rate = live.sample_rate
            self.out_channels = live.channels
            # An empty translated bus between units is normal; track_playout reports real underruns.
            self.record_drain = RTRecordDrain(self.engine, report_starvation=False)
        else:
            self.engine = native.Engine(channels=1, capture_ring_frames=CANONICAL * 2, max_delay_frames=0,
                                        translated_ring_frames=int(CANONICAL * TRANSLATED_RING_SECONDS),
                                        max_block_frames=4_096, record_capacity=4_096,
                                        rendered_ring_frames=0 if o.rendered_out is None else CANONICAL * 2,
                                        fade_frames=int(CANONICAL * EDGE_FADE_SECONDS))
            self.engine.set_render_output(True)
            self.out_rate = CANONICAL
            self.out_channels = 1
            self.record_drain = None
        self.translated_ring = self.engine.translated
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.deliveries: list[_Delivery] = []
        self.seq = 0
        self.tasks: dict[UnitID, _Task] = {}
        self.translation_tasks: dict[UnitID, _Task] = {}
        self.translation_queue: list = []
        self.synthesis_queue: list[P.SynthesisJob] = []
        self.active_synthesis: UnitID | None = None
        self.held: dict[UnitID, list] = {}
        self.unit_resamplers: dict[UnitID, StreamingResampler] = {}
        self.plays: dict[UnitID, PlayRecord] = {}
        self.current_chunk = None
        self.src_frames = 0
        self.out_frames = 0
        self.t0 = 0
        self.now = 0
        self.last_tick = 0
        self.last_resource = 0
        self.profile_ready = False
        self.prepared_voices: dict[str, PreparedVoice] = {}
        self.acquires_reference = False
        self.reference_policies: dict[SpeakerID, ReferencePolicy] = {}
        self.preparing_reference = False
        self.pending_references: list[ReferencePlan] = []
        self.deferred_references: list[tuple] = []
        self.profile_log: list[dict] = []
        self.history = np.zeros(0, np.float32)
        self.history_start = 0
        self.reference_audio: dict[SpeakerID, dict[SourceSpan, np.ndarray]] = {}
        self.known_speakers: set[SpeakerID] = set()
        self.stats: dict[str, int] = {}
        self.runtime_failed = False
        self.gain_target = (1.0, 0.0)
        self.last_consumed = 0
        self.last_out = 0
        self.unconsumed: list[np.ndarray] = []
        self.unconsumed_frames = 0
        self.unconsumed_base = 0
        self.rendered_frames = 0
        self.out_writer = WAVStreamWriter(o.out, CANONICAL, 1) if o.out else None
        self.stem_writer = WAVStreamWriter(o.stems, CANONICAL, 2) if o.stems else None
        self.source_writer = WAVStreamWriter(o.source_out, CANONICAL, 1) if o.source_out else None
        self.translated_writer = WAVStreamWriter(o.translated_out, self.out_rate, 1) if o.translated_out else None
        self.rendered_writer = None
        if o.rendered_out:
            self.rendered_writer = WAVStreamWriter(o.rendered_out, self.out_rate, self.out_channels)
            self.rendered_buffer = np.empty(4_096 * self.out_channels, np.float32)
            self.engine.set_capture_rendered(True)
        # Apply before the device starts: the engine defaults to original monitoring.
        if o.translation_only:
            self.engine.set_gains(0, 0, 0)
            self.gain_target = (0.0, 0.0)
        if o.tts == "clone":
            if o.voice_ref:
                self.prepare_fixed_reference(o.voice_ref, o.voice_ref_text)
            else:
                self.acquires_reference = True

    def _incr(self, key: str, by: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + by

    # MARK: Voice reference

    def prepare_fixed_reference(self, path: str, text_path: str | None) -> None:
        from .media import wav

        audio = wav.read(path)
        pcm = audio.mono()
        text = open(text_path, encoding="utf-8").read().strip() if text_path else ""
        seconds = len(pcm) / audio.sample_rate
        if not 1 <= seconds <= 15:
            raise SystemExit(f"a2a-pipeline: --voice-ref must be 1–15 s (got {seconds:.2f} s)")
        speaker = self.pipeline.config.default_speaker
        profile = VoiceProfile(speaker, 1, pcm, text, audio.sample_rate, 1,
                               ReferenceQuality(seconds, float("nan"), self.clipped_fraction(pcm), False))
        started = native.now_ns()
        voice = self.synthesizer.prepare(profile)
        self.inbox.put(("prepared", voice, seconds, native.now_ns() - started))

    @property
    def keeps_history(self) -> bool:
        if not self.acquires_reference:
            return False
        policy = self.reference_policies.get(self.pipeline.config.default_speaker)
        return self.pipeline.config.multi_speaker or not (policy and policy.is_complete)

    def acquire_reference(self, h) -> None:
        multi = self.pipeline.config.multi_speaker
        default = self.pipeline.config.default_speaker
        policy = self.reference_policies.get(default)
        if not self.acquires_reference or not h.is_final or (not multi and policy is not None and policy.is_complete):
            return
        span = ReferencePolicy(default).speech_span(h)
        if span is None:
            self._incr("reference_rejected_no_timing")
            self.publish_voice_progress(default, "collecting", reason="no_timing")
            return
        if multi:
            self.deferred_references.append((h, span, self.now))
            self.offer_deferred_references()
        else:
            self.offer_reference(h, span, default)

    def offer_deferred_references(self) -> None:
        waiting = []
        default = self.pipeline.config.default_speaker
        for h, span, at in self.deferred_references:
            timed = ReferencePolicy.token_span(h) or span
            coverage = self.pipeline.speaker_coverage(timed)
            if coverage is not None and coverage[2] >= 0.7:
                spk, share, labeled = coverage
                if share >= 0.9 * labeled:
                    self.offer_reference(h, span, spk)
                else:
                    self._incr("reference_rejected_mixed_speakers")
                    self.publish_voice_progress(default, "collecting", reason="mixed_speakers")
            elif self.now - at > 3_000_000_000:
                # Until the tracker labels anyone, references are acquired for S1.
                if not self.known_speakers:
                    self.offer_reference(h, span, default)
                else:
                    self._incr("reference_rejected_unlabeled")
                    self.publish_voice_progress(default, "collecting", reason="unlabeled")
            else:
                waiting.append((h, span, at))
        self.deferred_references = waiting

    def offer_reference(self, h, span: SourceSpan, speaker: SpeakerID) -> None:
        policy = self.reference_policies.get(speaker) or ReferencePolicy(speaker)
        self.reference_policies[speaker] = policy
        if policy.is_complete:
            return
        pcm = self.history_slice(span)
        if pcm is None:
            self._incr("reference_rejected_expired")
            self.publish_voice_progress(speaker, "collecting", policy.accepted_seconds, "expired")
            return
        result = policy.offer(span, h.text, signal.dbfs(signal.rms(pcm)), self.clipped_fraction(pcm))
        if isinstance(result, Rejected):
            self._incr(f"reference_rejected_{result.reason}")
            self.publish_voice_progress(speaker, "collecting", policy.accepted_seconds, result.reason)
            return
        self.publish_voice_progress(speaker, "collecting", policy.accepted_seconds)
        self.reference_audio.setdefault(speaker, {})[span] = pcm
        if result is not None:
            if self.preparing_reference:
                self.pending_references = [p for p in self.pending_references if p.speaker != result.speaker]
                self.pending_references.append(result)
            else:
                self.prepare_reference(result)

    def note_speaker(self, event) -> None:
        """Multi-speaker: a speaker's first label prepares their voice. Tone and system voices
        are ready at once (the system voice is cast by pitch); a clone waits for a reference."""
        if not isinstance(event, SpeakerAssign) or event.speaker in self.known_speakers:
            return
        speaker = event.speaker
        self.known_speakers.add(speaker)
        log(f"speaker {speaker} detected")
        if self.o.tts == "clone" or self.o.captions_only:
            return
        has_default = speaker == self.pipeline.config.default_speaker and self.profile_ready
        if self.o.tts == "tone":
            if not has_default:
                self.send(P.ProfileReady(speaker, 1))
            return
        profile = VoiceProfile(speaker, 2 if has_default else 1, np.zeros(0, np.float32), "", CANONICAL, 1,
                               ReferenceQuality(0, float("nan"), 0, False),
                               pitch_hz=self.tracker.pitch_hz(speaker) if self.tracker else None)

        def run() -> None:
            started = native.now_ns()
            try:
                voice = self.synthesizer.prepare(profile)
                self.inbox.put(("voice_ready", voice, native.now_ns() - started))
            except Exception as error:
                self.inbox.put(("voice_failed", speaker, f"{error}"))

        threading.Thread(target=run, daemon=True).start()

    def prepare_reference(self, plan: ReferencePlan) -> None:
        gap = np.zeros(CANONICAL * 15 // 100, np.float32)
        parts = []
        for i, segment in enumerate(plan.segments):
            piece = self.reference_audio.get(plan.speaker, {}).get(segment.span)
            if piece is None:
                self._incr("reference_missing_audio")
                return
            if i:
                parts.append(gap)
            parts.append(piece)
        pcm = np.concatenate(parts)
        profile = VoiceProfile(plan.speaker, plan.version, pcm, plan.text, CANONICAL, 1,
                               ReferenceQuality(plan.seconds, float("nan"), self.clipped_fraction(pcm), False))
        self.preparing_reference = True
        self.publish_voice_progress(plan.speaker, "preparing", plan.seconds)
        seconds = len(pcm) / CANONICAL

        def run() -> None:
            started = native.now_ns()
            try:
                voice = self.synthesizer.prepare(profile)
                self.inbox.put(("prepared", voice, seconds, native.now_ns() - started))
            except Exception as error:
                self.inbox.put(("prepare_failed", plan.speaker, plan.version, f"{error}"))

        threading.Thread(target=run, daemon=True).start()

    def publish_voice_progress(self, speaker: SpeakerID, state: str, seconds: float = 0.0,
                               reason: str | None = None) -> None:
        if not self.o.events_stdout:
            return
        self._stdout(json.dumps({"reused": False, "required_seconds": ReferencePolicyConfig().min_seconds,
                                 "seconds": seconds, "speaker": speaker.raw_value, "voice_status": state,
                                 **({"reason": reason} if reason else {})},
                                sort_keys=True))

    def history_slice(self, span: SourceSpan) -> np.ndarray | None:
        if span.start < self.history_start:
            return None
        lo = span.start - self.history_start
        hi = min(len(self.history), span.end - self.history_start)
        return self.history[lo:hi].copy() if lo < hi else None

    @staticmethod
    def clipped_fraction(pcm) -> float:
        a = np.asarray(pcm)
        return float(np.sum(np.abs(a) >= 0.999)) / max(1, len(a))

    def clock(self) -> int:
        if self.virtual:
            return self.t0 + self.src_frames * 1_000_000_000 // CANONICAL
        return native.now_ns()

    def _stdout(self, line: str) -> None:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    # MARK: Reducer plumbing

    def send(self, inp) -> None:
        self.execute(self.pipeline.handle(inp, self.now))

    def execute(self, effects: list) -> None:
        o = self.o
        for effect in effects:
            if isinstance(effect, P.TraceOut):
                e = effect.event
                self.trace.emit(e)
                if o.events_stdout and e.kind in (TraceKind.STATE_CHANGE, TraceKind.ERROR, TraceKind.QUEUE_DEPTH,
                                                  TraceKind.FALLBACK_ENTER, TraceKind.FALLBACK_EXIT, TraceKind.GAIN_RAMP):
                    self._stdout(e.json_line())
            elif isinstance(effect, P.Caption):
                if o.events_stdout:
                    caption = TraceEvent(self.now, o.session, self.pipeline.gate.epoch, self.pipeline.gate.generation,
                                         TraceKind.COMMIT_TARGET, (f_str("caption", effect.kind), f_str("id", effect.id),
                                                                   f_str("text", effect.text)))
                    self._stdout(caption.json_line())
            elif isinstance(effect, P.RequestFinalize):
                self.recognizer.request_finalize()
            elif isinstance(effect, P.Translate):
                self.start_translation(effect.request)
            elif isinstance(effect, P.Synthesize):
                self.start_synthesis(effect.job)
            elif isinstance(effect, P.CancelTranslation):
                task = self.translation_tasks.pop(effect.unit, None)
                if task:
                    task.cancelled = True
                self.translation_queue = [r for r in self.translation_queue if r.unit_id != effect.unit]
                self.deliveries = [d for d in self.deliveries
                                   if not (d.event[0] == "input" and isinstance(d.event[1], P.Translation)
                                           and d.event[1].unit == effect.unit)]
            elif isinstance(effect, P.CancelSynthesis):
                self._cancel_synthesis(effect.unit)
            elif isinstance(effect, P.HoldPCM):
                if self.current_chunk is not None:
                    self.held.setdefault(effect.header.unit_id, []).append(self.current_chunk)
            elif isinstance(effect, P.EnqueuePCM):
                if self.current_chunk is not None:
                    self.write(self.current_chunk, effect.header.unit_id)
            elif isinstance(effect, P.ReleaseHeld):
                for c in self.held.pop(effect.unit, []):
                    self.write(c, effect.unit)
                if self.plays.get(effect.unit) and self.plays[effect.unit].complete:
                    self.flush_resampler(effect.unit)
            elif isinstance(effect, P.DiscardHeld):
                self.held.pop(effect.unit, None)
                self.plays.pop(effect.unit, None)
            elif isinstance(effect, P.DropPCM):
                self._incr("pcm_dropped")
            elif isinstance(effect, P.RampGains):
                self.gain_target = (effect.gains.original, effect.gains.translated)
                self.engine.set_gains(effect.gains.original, effect.gains.translated,
                                      effect.duration_ms * self.out_rate // 1_000)
            elif isinstance(effect, P.FlushTranslatedBus):
                self.engine.flush_translated()
                self.plays.clear()
                # Whatever the mixer has not consumed yet is discarded, not played.
                self.write_played()
                self.unconsumed.clear()
                self.unconsumed_frames = 0
                self.unconsumed_base = self.translated_ring.total_written
            elif isinstance(effect, P.Teardown):
                pass
        self.launch_next_translation()
        if self.active_synthesis is None and self.synthesis_queue:
            self.start_synthesis(self.synthesis_queue.pop(0))

    def _cancel_synthesis(self, unit: UnitID) -> None:
        task = self.tasks.pop(unit, None)
        if task:
            task.cancelled = True
            if task.stream is not None and hasattr(task.stream, "cancel"):
                task.stream.cancel()
        self.synthesis_queue = [j for j in self.synthesis_queue if j.unit != unit]
        if self.active_synthesis == unit:
            self.active_synthesis = None
        self.unit_resamplers.pop(unit, None)
        self.deliveries = [d for d in self.deliveries
                           if not (d.event[0] in ("chunk", "finished", "failed") and d.event[1].unit == unit)]

    def schedule(self, event: tuple, after: int) -> None:
        self.seq += 1
        self.deliveries.append(_Delivery(self.now + after, self.seq, event))

    def start_translation(self, req) -> None:
        if self.virtual:
            latency = self.translator_latency.nanoseconds(len(req.uncommitted_source)) if self.translator_latency else 0
            try:
                outcome = P.Success(self.translator.translate(req))
            except Exception as error:
                outcome = P.Failure(f"{error}")
            self.schedule(("input", P.Translation(req.unit_id, outcome)), latency)
            return
        self.translation_queue.append(req)
        self.launch_next_translation()

    def launch_next_translation(self) -> None:
        if self.virtual or self.translation_tasks or not self.translation_queue:
            return
        req = self.translation_queue.pop(0)
        task = _Task(req.unit_id)
        modeled = (self.translator_latency.nanoseconds(len(req.uncommitted_source))
                   if self.translator_latency else 0)

        def run() -> None:
            started = native.now_ns()
            try:
                outcome = P.Success(self.translator.translate(req))
            except Exception as error:
                outcome = P.Failure(f"{type(error).__name__}: {error}")
            spent = native.now_ns() - started
            if modeled > spent:
                time.sleep((modeled - spent) / 1e9)
            if not task.cancelled:
                self.inbox.put(("input", P.Translation(req.unit_id, outcome)))

        self.translation_tasks[req.unit_id] = task
        task.thread = threading.Thread(target=run, name=f"a2a.mt.{req.unit_id}", daemon=True)
        task.thread.start()

    def start_synthesis(self, job: P.SynthesisJob) -> None:
        if not self.virtual:
            if self.active_synthesis is not None:
                self.synthesis_queue.append(job)
                return
            self.active_synthesis = job.unit
        synthesizer = self.synthesizer
        voice = self.prepared_voices.get(f"{job.speaker.raw_value}/{job.profile_version}") or PreparedVoice(
            job.speaker, job.profile_version, 0)
        self.plays[job.unit] = PlayRecord(job.unit)
        tone = synthesizer if isinstance(synthesizer, ToneSynthesizer) else None
        if self.virtual and tone is not None:
            at = tone.first_chunk.nanoseconds(0)
            for c in tone.synthesize(job.unit.raw_value, job.text):
                self.schedule(("chunk", job, c), at)
                at += int(c.frames / c.format.sample_rate * tone.real_time_factor * 1e9)
            self.schedule(("finished", job), at)
            return
        task = _Task(job.unit)

        def run() -> None:
            try:
                if tone is not None:
                    time.sleep(tone.first_chunk.nanoseconds(0) / 1e9)
                    stream = tone.synthesize(job.unit.raw_value, job.text)
                elif hasattr(synthesizer, "synthesize_voice"):
                    stream = synthesizer.synthesize_voice(job.unit.raw_value, job.text, voice, job.target_seconds)
                else:
                    stream = synthesizer.synthesize(job.unit.raw_value, job.text, voice, job.target_seconds)
                task.stream = stream
                if task.cancelled and hasattr(stream, "cancel"):
                    stream.cancel()
                    return
                for c in stream:
                    if task.cancelled:
                        if hasattr(stream, "cancel"):
                            stream.cancel()
                        return
                    self.inbox.put(("chunk", job, c))
                    if tone is not None:
                        time.sleep(c.frames / c.format.sample_rate * tone.real_time_factor)
                if not task.cancelled:
                    self.inbox.put(("finished", job))
            except Exception as error:
                if not task.cancelled:
                    self.inbox.put(("failed", job, f"{type(error).__name__}: {error}"))

        self.tasks[job.unit] = task
        task.thread = threading.Thread(target=run, name=f"a2a.tts.{job.unit}", daemon=True)
        task.thread.start()

    def deliver(self, event: tuple) -> None:
        kind = event[0]
        if kind == "input":
            inp = event[1]
            if isinstance(inp, P.Translation):
                self.translation_tasks.pop(inp.unit, None)
            self.send(inp)
        elif kind == "chunk":
            job, audio = event[1], event[2]
            # A canceled or completed unit has no play record.
            if job.unit not in self.plays:
                return
            header = PCMChunkHeader(job.unit, job.speaker, job.profile_version, job.epoch, job.generation,
                                    audio.format, job.span, False)
            self.current_chunk = audio
            self.send(P.PCM(header, audio.frames))
            self.current_chunk = None
        elif kind == "finished":
            job = event[1]
            self.tasks.pop(job.unit, None)
            if self.active_synthesis == job.unit:
                self.active_synthesis = None
            play = self.plays.get(job.unit)
            if play is None:
                return
            if play.start is None and not self.held.get(job.unit):
                self.send(P.SynthesisFailed(job.unit, "synthesizer returned no audio"))
                return
            play.complete = True
            if play.start is not None:
                self.flush_resampler(job.unit)
            self.send(P.SynthesisFinished(job.unit, job.generation))
        elif kind == "failed":
            job, message = event[1], event[2]
            self.tasks.pop(job.unit, None)
            if self.active_synthesis == job.unit:
                self.active_synthesis = None
            self.send(P.SynthesisFailed(job.unit, message))
        elif kind == "prepared":
            voice, seconds, prepare_ns = event[1], event[2], event[3]
            self.preparing_reference = False
            self.prepared_voices[f"{voice.speaker_id.raw_value}/{voice.profile_version}"] = voice
            policy = self.reference_policies.get(voice.speaker_id)
            if policy and policy.is_complete and not any(p.speaker == voice.speaker_id for p in self.pending_references):
                self.reference_audio.pop(voice.speaker_id, None)
            self.profile_log.append({"speaker": voice.speaker_id.raw_value, "version": voice.profile_version,
                                     "reference_seconds": seconds, "prepare_seconds": prepare_ns / 1e9,
                                     "source_seconds": self.src_frames / CANONICAL})
            multi = f"{voice.speaker_id} " if self.pipeline.config.multi_speaker else ""
            log(f"voice profile {multi}v{voice.profile_version} ready: {seconds:.1f} s reference, "
                f"prepared in {prepare_ns / 1e9:.2f} s")
            self.send(P.ProfileReady(voice.speaker_id, voice.profile_version))
            self.publish_voice_progress(voice.speaker_id, "ready", seconds)
            if self.pending_references:
                self.prepare_reference(self.pending_references.pop(0))
        elif kind == "prepare_failed":
            speaker, version, message = event[1], event[2], event[3]
            self.preparing_reference = False
            if version == 1:
                self.reference_policies[speaker] = ReferencePolicy(speaker)
                self.reference_audio.pop(speaker, None)
                self.pending_references = [p for p in self.pending_references if p.speaker != speaker]
            self.publish_voice_progress(speaker, "collecting")
            self._incr("reference_prepare_failed")
            log(f"voice profile v{version} failed: {message}" if self.o.research_text else f"voice profile v{version} failed")
            self.trace.emit(TraceEvent(self.now, self.o.session, self.pipeline.gate.epoch, self.pipeline.gate.generation,
                                       TraceKind.ERROR, (f_str("where", "profile"), f_int("prof", version),
                                                         f_str("message", message if self.o.research_text else "prepare failed"))))
            if self.pending_references:
                self.prepare_reference(self.pending_references.pop(0))
        elif kind == "voice_ready":
            voice, prepare_ns = event[1], event[2]
            self.prepared_voices[f"{voice.speaker_id.raw_value}/{voice.profile_version}"] = voice
            name = getattr(self.synthesizer, "voice_name", lambda v: None)(voice)
            if name:
                log(f"speaker {voice.speaker_id} speaks with the {name} voice")
            self.profile_log.append({"speaker": voice.speaker_id.raw_value, "version": voice.profile_version,
                                     "prepare_seconds": prepare_ns / 1e9, "source_seconds": self.src_frames / CANONICAL})
            self.send(P.ProfileReady(voice.speaker_id, voice.profile_version))
        elif kind == "voice_failed":
            self._incr("voice_prepare_failed")
            log(f"voice for {event[1]} failed: {event[2]}" if self.o.research_text else f"voice for {event[1]} failed")

    def write(self, audio, unit: UnitID) -> None:
        resampler = self.unit_resamplers.get(unit)
        if resampler is None:
            resampler = self.unit_resamplers[unit] = StreamingResampler(audio.format.sample_rate, self.out_rate)
        samples = audio.samples
        if audio.format.channel_count != 1:
            samples = samples.reshape(-1, audio.format.channel_count).mean(axis=1).astype(np.float32)
        self.write_ring(resampler.process(samples), unit)

    def flush_resampler(self, unit: UnitID) -> None:
        r = self.unit_resamplers.pop(unit, None)
        if r is not None:
            self.write_ring(r.flush(), unit)

    def write_ring(self, samples: np.ndarray, unit: UnitID) -> None:
        ring = self.translated_ring
        play = self.plays.get(unit)
        if play is None:
            play = self.plays[unit] = PlayRecord(unit)
        if play.start is None:
            play.start = ring.total_written
        if not len(samples):
            play.end = ring.total_written
            return
        written = ring.write(samples)
        if self.translated_writer is not None:
            self.unconsumed.append(samples[:written].copy())
            self.unconsumed_frames += written
        if written < len(samples):
            self.trace.emit(TraceEvent(self.now, self.o.session, self.pipeline.gate.epoch, self.pipeline.gate.generation,
                                       TraceKind.ERROR, (f_str("where", "translated_bus"), f_str("message", "overflow"),
                                                         f_int("frames", len(samples) - written))))
        play.end = ring.total_written

    # MARK: Output

    def render(self, inp: np.ndarray | None, frames: int) -> None:
        engine = self.engine
        if self.live is not None:
            # The engine thread already mixed and played; follow its counters.
            consumed = engine.counter("translated_consumed")
            out_now = engine.counter("output_frames")
            self.track_playout(self.last_consumed, consumed, self.last_out)
            self.last_consumed = consumed
            self.last_out = out_now
            self.write_rendered()
            return
        consumed_before = engine.counter("translated_consumed")
        out_before = self.out_frames
        out = engine.process(inp if inp is not None else None, frames)
        # Keep the capture ring from filling; the pipeline reads source audio directly.
        engine.capture.skip(frames)
        self.out_frames += frames
        self.write_rendered()
        if self.out_writer:
            self.out_writer.append(out)
        if self.stem_writer:
            stems = np.empty(frames * 2, np.float32)
            stems[0::2] = inp if inp is not None else 0
            stems[1::2] = out
            self.stem_writer.append(stems)
        self.trace.emit(TraceEvent(self.now, self.o.session, self.pipeline.gate.epoch, self.pipeline.gate.generation,
                                   TraceKind.BUF_OUTPUT, (f_int("out_sample", out_before), f_int("frames", frames),
                                                          f_int("rate", CANONICAL))))
        self.track_playout(consumed_before, engine.counter("translated_consumed"), out_before)

    def write_rendered(self) -> None:
        if self.rendered_writer is None or self.engine.rendered is None:
            return
        while n := self.engine.rendered.read_into(self.rendered_buffer):
            self.rendered_writer.append(self.rendered_buffer[: n * self.out_channels])
            self.rendered_frames += n

    def write_played(self) -> None:
        """Appends the translated audio the mixer has consumed since the last call."""
        if self.translated_writer is None:
            return
        consumed = self.engine.counter("translated_consumed")
        n = min(self.unconsumed_frames, consumed - self.unconsumed_base)
        while n > 0 and self.unconsumed:
            head = self.unconsumed[0]
            take = min(n, len(head))
            self.translated_writer.append(head[:take])
            if take == len(head):
                self.unconsumed.pop(0)
            else:
                self.unconsumed[0] = head[take:]
            self.unconsumed_frames -= take
            self.unconsumed_base += take
            n -= take

    def track_playout(self, consumed_before: int, consumed: int, out_before: int) -> None:
        """Turns mixer consumption of the translated ring into play.start / play.end / underrun."""
        self.write_played()
        ring_empty = self.translated_ring.readable == 0

        def out_sample(position: int) -> int:
            return out_before + max(0, position - consumed_before)

        for unit in sorted(self.plays):
            p = self.plays.get(unit)
            if p is None or p.start is None:
                continue
            if not p.started and p.start < consumed:
                p.started = True
                self.send(P.PlayStarted(unit, out_sample(p.start)))
                p = self.plays.get(unit, p)
            if p.started and p.complete and not p.ended and p.end <= consumed and unit not in self.unit_resamplers:
                p.ended = True
                self.plays.pop(unit, None)
                self.send(P.PlayEnded(unit, out_sample(p.end)))
                continue
            if p.started and not p.complete and ring_empty and not p.underrun_reported:
                p.underrun_reported = True
                self._incr("underruns")
                self.send(P.Underrun())

    # MARK: Loop

    @property
    def idle(self) -> bool:
        return (not self.deliveries and self.inbox.empty() and not self.tasks and not self.translation_tasks
                and not self.translation_queue and not self.synthesis_queue and not self.plays and not self.held
                and not getattr(self.recognizer, "pending", False))

    def check_recognition_tail(self, tail: int, tail_frames: int) -> None:
        if tail >= tail_frames and getattr(self.recognizer, "pending", False) and not self.runtime_failed:
            self.runtime_failed = True
            log("recognition is still processing when the tail limit expired; increase --tail-seconds or use a faster device")

    def step(self, chunk: np.ndarray | None, frames: int) -> None:
        self.now = self.clock()
        if self.controls is not None:
            mode = self.controls.take_latest()
            if mode is not None:
                self.send(P.SetPlaybackMode(mode))
        start = self.src_frames
        samples = chunk if chunk is not None else np.zeros(frames, np.float32)
        self.src_frames += frames
        span = SourceSpan(start, self.src_frames)
        if chunk is not None:
            self.send(P.Captured(span))
        if (not self.o.captions_only and self.o.tts != "clone" and not self.profile_ready
                and start * 1_000 // CANONICAL >= self.o.profile_ready_ms):
            self.profile_ready = True
            # Multi-speaker sessions too: speech the tracker never labels still has a voice.
            if self.pipeline.config.default_speaker not in self.known_speakers:
                self.send(P.ProfileReady(SpeakerID("S1"), 1))
        if chunk is not None and self.source_writer:
            self.source_writer.append(chunk)
        if self.keeps_history:
            self.history = np.concatenate([self.history, samples])
            keep = CANONICAL * 30
            if len(self.history) > keep + CANONICAL * 10:
                drop = len(self.history) - keep
                self.history_start += drop
                self.history = self.history[drop:]
        elif len(self.history):
            self.history = np.zeros(0, np.float32)
        if self.record_drain is not None:
            gate = self.pipeline.gate
            self.record_drain.drain(self.o.session, gate.epoch, gate.generation, float(self.out_rate), self.trace)
            if self.record_drain.invalid_timestamps > 0 or self.record_drain.overflow_frames > 0:
                self.runtime_failed = True
                self.send(P.Discontinuity("capture timing invalid or audio lost"))
                return
        low = self.to16k.process(samples)
        frame16 = AudioFrame16k(span, low)
        self.speech_evidence.advance(span.end)
        vad_events = self.vad.process(frame16)
        for event in vad_events:
            self.speech_evidence.process(event)
            if hasattr(self.recognizer, "on_vad_event"):
                self.recognizer.on_vad_event(event)
            self.send(P.Vad(event))
        # Labels reach the reducer before any commit they cover.
        if self.tracker is not None:
            for event in self.tracker.process(frame16, vad_events):
                self.send(P.Speaker(event))
                self.note_speaker(event)
        self.recognizer.accept(frame16)
        for h in self.recognizer.drain_hypotheses():
            if self.o.asr == "scripted" or self.speech_evidence.accepts(h):
                self.send(P.Hypothesis(h))
                self.acquire_reference(h)
            else:
                self._incr("silence_hypotheses_rejected")
        if self.deferred_references:
            self.offer_deferred_references()
        for adapter, label in ((self.recognizer, "recognition"), (self.translator, "translation"),
                               (self.synthesizer, "voice worker")):
            failure = getattr(adapter, "failure", None)
            if failure and not self.runtime_failed:
                log(f"{label} stopped: {failure}" if self.o.research_text else f"{label} stopped")
                self.runtime_failed = True
                self.send(P.WorkerHealthy(False))
        while True:
            try:
                event = self.inbox.get_nowait()
            except queue.Empty:
                break
            self.deliver(event)
        self.deliveries.sort()
        while self.deliveries and self.deliveries[0].at <= self.now:
            self.deliver(self.deliveries.pop(0).event)
        self.render(samples if chunk is not None else None, frames)
        if self.now >= self.last_tick + 100_000_000:
            self.last_tick = self.now
            self.send(P.TranslatedBacklog(self.translated_ring.readable / self.out_rate))
            self.send(P.Tick())
        if self.now >= self.last_resource + 1_000_000_000:
            self.last_resource = self.now
            self.emit_resource()
        self.trace.flush()

    def emit_resource(self) -> None:
        resident = _resident_bytes()
        if resident is None:
            return
        gate = self.pipeline.gate
        self.trace.emit(TraceEvent(self.now, self.o.session, gate.epoch, gate.generation, TraceKind.RESOURCE_SAMPLE,
                                   (f_str("process", "pipeline"), f_int("phys_footprint_bytes", resident[0]),
                                    f_int("resident_bytes", resident[1]))))

    def run(self, source: Source) -> None:
        o = self.o
        self.t0 = 1_000_000_000 if self.virtual else native.now_ns()
        self.now = self.clock()
        self.send(P.Arm())
        if self.controls is not None:
            mode = self.controls.take_latest()
            if mode is not None:
                self.send(P.SetPlaybackMode(mode))
        self.send(P.ArmSucceeded())
        frames = source.chunk
        tail_frames = int(o.tail_seconds * CANONICAL)
        tail = 0
        if self.live is not None:
            self.live.start_clock(o.seconds)
            while (chunk := source.next_live(until_stop=True)) is not None:
                self.step(chunk, len(chunk))
                if self.runtime_failed:
                    break
            log(f"stopping: finishing in-flight units over live audio (up to {o.tail_seconds} s)")
            self.recognizer.request_finalize()
            while not self.runtime_failed and source.failure is None and tail < tail_frames:
                chunk = source.next_live(until_stop=False)
                if chunk is None:
                    break
                self.step(chunk, len(chunk))
                tail += len(chunk)
                if tail >= CANONICAL and self.idle:
                    break
        else:
            realtime_file = not self.virtual and source.total_frames is not None and o.pace == "realtime"
            wall_start = native.now_ns()
            while (chunk := source.next()) is not None:
                self.step(chunk, len(chunk))
                if self.runtime_failed:
                    break
                if realtime_file:
                    self.pace(wall_start)
            self.recognizer.request_finalize()
            # Trailing silence lets finals, translation, and playout complete.
            while not self.runtime_failed and tail < tail_frames:
                self.step(None, frames)
                tail += frames
                if not self.virtual:
                    self.pace(wall_start)
                if tail >= CANONICAL and self.idle:
                    break
        # A bounded tail must not turn unprocessed utterances into a successful run.
        if not source.stop_requested.is_set():
            self.check_recognition_tail(tail, tail_frames)
        self.now = self.clock()
        if source.failure:
            self.runtime_failed = True
            log(source.failure)
            self.send(P.Discontinuity(source.failure))
        self.send(P.Stop())
        for adapter in (self.synthesizer, self.translator, self.recognizer):
            if hasattr(adapter, "shutdown"):
                adapter.shutdown()
        if self.live is not None:
            self.live.settle_gains(self.gain_target, self.pipeline.config.health.ramp_ms + 50)
            self.live.stop()
        self.write_rendered()
        self.trace.close()
        for w in (self.out_writer, self.stem_writer, self.source_writer, self.translated_writer, self.rendered_writer):
            if w:
                w.close()
        self.summary()

    def pace(self, wall_start: int) -> None:
        target = wall_start + self.src_frames * 1_000_000_000 // CANONICAL
        current = native.now_ns()
        if target > current:
            time.sleep((target - current) / 1e9)

    def summary(self) -> None:
        o = self.o
        caps = getattr(self.synthesizer, "capabilities", None)
        s = {
            "session": o.session, "clock": o.clock, "asr": o.asr, "mt": o.mt, "tts": o.tts, "mode": "file",
            "direction": o.direction.value, "pair": o.direction.trace_pair,
            "tts_preserves_speaker_identity": bool(caps.preserves_speaker_identity) if caps else False,
            # No synthesizer has passed the identity study (M2); never gate evidence.
            "tts_identity_qualified": False,
            "source_frames": self.src_frames, "output_frames": self.out_frames, "trace_events": self.trace.event_count,
            "pcm_dropped": self.stats.get("pcm_dropped", 0), "underruns": self.stats.get("underruns", 0),
            "final_state": str(self.pipeline.state), "runtime_failed": self.runtime_failed,
            "captions_only": o.captions_only, "translation_only": o.translation_only,
            "output_sample_rate": self.out_rate, "playback": self.pipeline.playback_mode.value, "fluent": o.fluent,
            "rendered_frames_recorded": self.rendered_frames,
            "rendered_capture_overflow_frames": self.engine.rendered.overflow_frames if self.engine.rendered else 0,
            "silence_hypotheses_rejected": self.stats.get("silence_hypotheses_rejected", 0),
            "translated_starved_frames": self.engine.counter("translated_starved_frames"),
            "max_debt_seconds": self.pipeline.config.health.max_debt_seconds,
            "max_source_lag_seconds": self.pipeline.config.health.max_source_lag_seconds,
            "max_unit_keys": self.pipeline.config.commit.max_unit_keys,
            "speakers": {"mode": o.speakers, "detected": 1 if self.tracker is None else len(self.known_speakers)},
            "runtime": {"implementation": "portable", "platform": sys.platform, "vad": o.vad,
                        "adapters": {k: getattr(a, "describe", lambda: getattr(a, "name", type(a).__name__))()
                                     for k, a in (("asr", self.recognizer), ("mt", self.translator),
                                                  ("tts", self.synthesizer))}},
        }
        if self.tracker is not None and o.tts != "clone":
            s["voices"] = self.profile_log
        info = getattr(self.synthesizer, "info", None)
        if o.tts == "clone":
            s["clone"] = {"model": info.model if info else "", "selected_model": o.clone_model,
                          "audio_output_streaming": bool(caps.audio_output_streaming) if caps else False,
                          "conditioning": o.clone_mode, "reference": "fixed" if o.voice_ref else "acquired",
                          "profiles": self.profile_log, "load_seconds": info.load_seconds if info else 0,
                          "warmup_seconds": info.warmup_seconds if info else 0,
                          "reference_stats": {k: v for k, v in self.stats.items() if k.startswith("reference_")}}
        if self.live is not None:
            s["mode"] = "tap"
            s["output_frames"] = self.engine.counter("output_frames")
            s["tap"] = self.live.summary()
        self._stdout(json.dumps(s, sort_keys=True))


def vad_for(o):
    if o.vad == "silero":
        from .models.silero import SileroVAD

        return SileroVAD()
    return EnergyVAD(threshold_db=o.vad_threshold_db)


def _resident_bytes() -> tuple[int, int] | None:
    """(private/footprint, resident) bytes of this process, or None when unavailable."""
    if sys.platform == "win32":
        import ctypes as C
        import ctypes.wintypes as W

        class PMC(C.Structure):
            _fields_ = [("cb", W.DWORD), ("PageFaultCount", W.DWORD), ("PeakWorkingSetSize", C.c_size_t),
                        ("WorkingSetSize", C.c_size_t), ("QuotaPeakPagedPoolUsage", C.c_size_t),
                        ("QuotaPagedPoolUsage", C.c_size_t), ("QuotaPeakNonPagedPoolUsage", C.c_size_t),
                        ("QuotaNonPagedPoolUsage", C.c_size_t), ("PagefileUsage", C.c_size_t),
                        ("PeakPagefileUsage", C.c_size_t), ("PrivateUsage", C.c_size_t)]

        pmc = PMC()
        pmc.cb = C.sizeof(PMC)
        psapi = C.WinDLL("psapi")
        k32 = C.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = W.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [W.HANDLE, C.c_void_p, W.DWORD]
        psapi.GetProcessMemoryInfo.restype = W.BOOL
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), C.byref(pmc), pmc.cb):
            return None
        return int(pmc.PrivateUsage), int(pmc.WorkingSetSize)
    try:
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss = rss if sys.platform == "darwin" else rss * 1024
        return rss, rss
    except Exception:
        return None


class PlaybackControls:
    """A bounded, latest-value control channel on stdin, read independently of warm-up:
    one mode name per line (`original`, `translated`), or `stop`."""

    def __init__(self, stop_event: threading.Event, allow_playback: bool = True) -> None:
        self._latest = None
        self._lock = threading.Lock()
        self._stop = stop_event
        self._allow_playback = allow_playback
        threading.Thread(target=self._read, name="a2a.controls", daemon=True).start()

    def _read(self) -> None:
        # Raw reads (no BufferedReader lock), bounded lines: an oversized line is ignored.
        line, oversized = bytearray(), False
        while True:
            try:
                block = os.read(0, 256)
            except OSError:
                break
            if not block:
                break
            for byte in block:
                if byte == 10:
                    if not oversized:
                        self._apply(line.decode("utf-8", "replace").strip())
                    line.clear()
                    oversized = False
                elif len(line) < 32 and not oversized:
                    line.append(byte)
                else:
                    oversized = True
        self._stop.set()  # the controlling app went away

    def _apply(self, command: str) -> None:
        if command == "stop":
            self._stop.set()
            return
        if not self._allow_playback:
            return
        try:
            mode = P.PlaybackMode(command)
        except ValueError:
            return
        with self._lock:
            self._latest = mode

    def take_latest(self):
        with self._lock:
            mode, self._latest = self._latest, None
            return mode


_ = TranslationLanguage  # re-exported for adapters that build voices by language
