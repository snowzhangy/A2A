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
from .core.audio_segment import (AdaptiveSpeechThreshold, Append, AudioSegmentPolicy, AudioStreamFeed,
                                 Close, Discard)
from .core.direction import TranslationLanguage
from .core.reference_policy import ReferencePlan, ReferencePolicy, ReferencePolicyConfig, Rejected
from .core.speech_evidence import SpeechEvidence
from .core.pipeline import round_half_away
from .core.term_map import TermMap
from .core.trace import TraceEvent, TraceKind, f_bool, f_double, f_int, f_str, f_unit
from .core.types import (CANONICAL_SAMPLE_RATE, ASRHypothesis, ASRToken, AudioFrame16k, PCMChunkHeader,
                         PreparedVoice, ReferenceQuality, SourceSpan, SpeakerAssign, SpeakerID, UnitID, VoiceProfile)
from .media import signal
from .media.background_bed import BackgroundBed
from .media.resampler import StreamingResampler
from .media.wav import WAVStreamWriter
from .models.base import AdapterError, DeliveryClip
from .models.fixtures import DictionaryTranslator, EnergyVAD, ScriptedRecognizer, ToneSynthesizer
from .trace_writer import RTRecordDrain, TraceWriter

CANONICAL = CANONICAL_SAMPLE_RATE
# Translated audio the bus can queue: well above the fallback thresholds plus the longest phrase.
TRANSLATED_RING_SECONDS = 30.0
EDGE_FADE_SECONDS = 0.005


def log(message: str) -> None:
    sys.stderr.write(f"a2a-pipeline: {message}\n")
    sys.stderr.flush()


def load_terms(path: str) -> dict[str, str]:
    """A JSON object of term -> replacement (--glossary, --pronounce)."""
    try:
        with open(path, encoding="utf-8") as fh:
            terms = json.load(fh)
    except (OSError, ValueError) as error:
        raise SystemExit(f"a2a-pipeline: {path}: {error}") from None
    if not isinstance(terms, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in terms.items()):
        raise SystemExit(f"a2a-pipeline: {path}: expected a JSON object of strings")
    return terms


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
        """Live chunks as the capture ring fills; an empty chunk after 20 ms without capture.
        With `until_stop`, returns None once --seconds elapse or a stop arrives; the tail reads
        on so playout finishes."""
        live = self.live
        wait_start = time.perf_counter()
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
                # An empty chunk: the driver keeps playout and phrase slots moving meanwhile.
                if time.perf_counter() - wait_start >= 0.02:
                    return np.zeros(0, np.float32)
                continue
            frames = self._tap_buffer[: n * live.channels].reshape(n, live.channels)
            mono = frames.mean(axis=1, dtype=np.float32).astype(np.float32)
            if self._to_canonical is not None:
                mono = self._to_canonical.process(mono)
            self._pending = np.concatenate([self._pending, mono])
        out, self._pending = self._pending[: self.chunk], self._pending[self.chunk:]
        return out


# MARK: Driver


def prepare_all(synthesizer, profile) -> list:
    """One prepared voice per worker lane. A single-worker adapter yields one; a multi-lane
    adapter yields its own hand-off per lane, because a handle is only meaningful to the
    worker that produced it."""
    if hasattr(synthesizer, "prepare_lanes"):
        return synthesizer.prepare_lanes(profile)
    return [synthesizer.prepare(profile)]


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
    def __init__(self, options, recognizer, translator, synthesizer, live=None, controls=None,
                 separator=None, lid=None) -> None:
        o = self.o = options
        self.virtual = o.clock == "virtual"
        self.controls = controls
        self.live = live
        config = P.PipelineConfig(session=o.session, direction=o.direction)
        config.direct_audio_translation = o.asr == "audio"
        config.background_audio_enabled = o.background
        config.trace_text = o.research_text
        config.emit_captions = o.events_stdout
        config.original_audio_enabled = not o.translation_only
        config.playback_mode = o.playback
        # A clone keeps each speaker's first voice for the session, and in a dialogue a line
        # waits for its own speaker's label instead of borrowing the previous actor's voice.
        config.pin_speaker_profiles = o.tts == "clone"
        config.require_speaker_attribution = o.tts == "clone"
        if o.fluent:
            config.commit.streaming_clauses = True
            config.finish_phrase_before_catch_up = True
            config.health.prebuffer_seconds = 0.24
        config.adaptive_streaming_prebuffer = o.adaptive_prebuffer
        if o.quality_first:
            config.finish_phrase_before_catch_up = True
            config.complete_phrases_before_playback = True
            config.commit.streaming_clauses = False
            config.health.max_debt_seconds = 20
            config.health.max_source_lag_seconds = 20
            config.max_pending_units = 16
            # Quality-first buys whole phrases with latency: a unit closes early only once it
            # would exceed the session's own source-lag budget.
            config.commit.max_unit_source_seconds = config.health.max_source_lag_seconds
        if o.max_debt_seconds is not None:
            config.health.max_debt_seconds = o.max_debt_seconds
        if o.max_source_lag_seconds is not None:
            config.health.max_source_lag_seconds = o.max_source_lag_seconds
        # With a picture delay the lag limit counts from each unit's slot (ADR-0012).
        config.presentation_delay_seconds = o.presentation_delay_ms / 1_000
        config.turn_fill_enabled = o.turn_fill
        config.clone_delivery_line = o.clone_delivery == "line"
        self.restores_voice = (o.passthrough and o.asr == "audio" and o.background and o.tts == "clone"
                               and o.presentation_delay_ms >= 4_000)
        config.restores_original_voice = self.restores_voice
        if self.restores_voice:
            config.original_voice_decision_deadline_seconds = max(
                0.1, min(6.0, config.presentation_delay_seconds - 1.5) - 0.7)
        if o.max_unit_keys is not None:
            config.commit.max_unit_keys = o.max_unit_keys
        if o.max_unit_seconds is not None:
            config.commit.max_unit_source_seconds = o.max_unit_seconds
        if o.speaker_carry is not None:
            config.unlabeled_speaker_carry_seconds = o.speaker_carry
        if o.speaker_carry_pause is not None:
            config.unlabeled_speaker_carry_after_pause_seconds = o.speaker_carry_pause
        if o.speaker_carry_piece is not None:
            config.unlabeled_speaker_carry_max_piece_seconds = o.speaker_carry_piece
        # Quality-first preserves the complete target as well as complete PCM; the validator's
        # implausible-output cap still applies, matching the Swift driver.
        config.validator.shortens_over_budget_targets = o.shorten and not o.quality_first
        config.validator.silences_fillers = o.filler_silence
        if o.glossary:
            config.glossary = load_terms(o.glossary)
        # Spoken respellings applied to the synthesizer's text only (--pronounce).
        self.pronunciation = TermMap(load_terms(o.pronounce) if o.pronounce else {}, ignores_case=True)
        self.tracker = None
        if o.speakers == "auto":
            from .models.speaker_tracking import VoiceprintSpeakerTracker

            config.multi_speaker = True
            self.tracker = VoiceprintSpeakerTracker()
        # Mandarin recognition hands back one key per character, so only a word segmenter can
        # keep a clause cut out of the middle of a word (CommitPolicy `WordSegmenter`).
        self.word_segmenter = word_segmenter_for(o.direction.source) if o.word_cuts else None
        self.target_word_segmenter = word_segmenter_for(o.direction.target) if o.word_cuts else None
        self.pipeline = P.SessionPipeline(config, word_segmenter=self.word_segmenter,
                                          target_word_segmenter=self.target_word_segmenter)
        self.trace = TraceWriter(o.trace)
        self.vad = vad_for(o)
        self.speech_evidence = SpeechEvidence()
        self.to16k = StreamingResampler(CANONICAL, 16_000)
        self.separator = separator
        self.lid = lid
        self.recognizer = recognizer
        self.translator = translator
        self.synthesizer = synthesizer
        self.prepared_lane_voices: dict[str, list] = {}
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
        self.audio_segments = (AudioSegmentPolicy.sentences(o.presentation_delay_ms / 1000)
                               if o.asr == "audio" and o.sentence_units and o.background
                               and o.presentation_delay_ms > 0 else AudioSegmentPolicy())
        self.background_ring = self.engine.enable_background(int(self.out_rate * (3 + o.presentation_delay_ms / 1000))) if o.background else None
        self.background_resampler = StreamingResampler(CANONICAL, self.out_rate) if o.background else None
        self.bed = BackgroundBed(int(o.presentation_delay_ms / 1000 * CANONICAL)) if o.background else None
        self.background_frames = 0
        self.background_failed = False
        self.voice_history = np.zeros(0, np.float32)
        self.voice_history_start = 0
        self.separator_input = np.zeros(0, np.float32)
        self.voice_lag = int(round(separator.latency_seconds * CANONICAL)) if separator else 0
        self.separation_output = 0
        self.segment_vad = vad_for(o) if o.asr == "audio" and o.background else None
        if self.segment_vad is not None:
            self.segment_vad.threshold_db = np.float32(o.vad_threshold_db - 5)
        self.segment_threshold = (AdaptiveSpeechThreshold(o.vad_threshold_db - 5)
                                  if self.audio_segments.dip_window_samples else None)
        self.voice_to16k = StreamingResampler(CANONICAL, 16_000) if self.segment_vad is not None else None
        self.recent_segments: list[SourceSpan] = []
        self.segment_frontier = 0
        self.last_capture_now = 0
        self.inbox: queue.SimpleQueue = queue.SimpleQueue()
        self.deliveries: list[_Delivery] = []
        self.seq = 0
        self.tasks: dict[UnitID, _Task] = {}
        self.translation_tasks: dict[UnitID, _Task] = {}
        self.translation_queue: list = []
        self.translation_audio: dict[UnitID, np.ndarray] = {}
        self.translation_tickets: dict[UnitID, object] = {}
        self.audio_feed = AudioStreamFeed(self.audio_segments.stream_limit_samples)
        self.hy_tickets: list[tuple[SourceSpan, object]] = []
        self.hy_streaming = None
        if (o.asr == "audio" and o.mt == "hy-local" and o.hy_stream and not self.virtual
                and getattr(getattr(translator, "info", None), "streams_spans", False)):
            self.hy_streaming = translator
        self.synthesis_queue: list[P.SynthesisJob] = []
        # unit -> lane. One worker by default; two with --dual-clone when the cards allow it.
        self.active_synthesis: dict[UnitID, int] = {}
        self.synthesis_lanes = int(getattr(self.synthesizer, "lanes", 1))
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
        # Opt-in research voice references (ADR-0006).
        self.voice_memory = None
        if o.voice_memory:
            from .models.voice_memory import VoiceMemoryStore

            self.voice_memory = VoiceMemoryStore(o.voice_memory)
        self.memory_preparing: set[SpeakerID] = set()
        self.memory_reused: set[SpeakerID] = set()
        self.memory_failed: set[SpeakerID] = set()
        self.remembered_ids: dict[SpeakerID, str] = {}
        self.stats: dict[str, int] = {}
        # `late_ms` of every scheduled phrase under a picture delay (ADR-0012).
        self.slot_late_ms: list[int] = []
        # Virtual time spent without capture (--simulate-capture-gap); always 0 on the host clock.
        self.idle_ns = 0
        # Synthetic silence fed at a pause (idle_step); it is not source time on the virtual clock.
        self.synthetic_frames = 0
        self.idle_since: int | None = None
        self.pause_flushed = False
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
        self.background_writer = WAVStreamWriter(o.background_out, self.out_rate, 1) if o.background_out else None
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
        if self.o.mt in ("hy", "hy-local"):
            return True
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
        policy = ReferencePolicy(default)
        candidates = policy.reference_candidates(h)
        if not candidates:
            self._incr("reference_rejected_no_timing")
            self.publish_voice_progress(default, "collecting", reason="no_timing")
            return
        for candidate in candidates:
            span = policy.speech_span(candidate)
            if span is None:
                continue
            if multi:
                # Each cut must independently match a single tracked speaker.
                self.deferred_references.append((candidate, span, self.now))
            else:
                self.offer_reference(candidate, span, default)
        if multi:
            self.offer_deferred_references()

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
        if speaker in self.memory_preparing or speaker in self.memory_reused:
            return
        policy = self.reference_policies.get(speaker) or self.live_reference_policy(speaker)
        self.reference_policies[speaker] = policy
        if policy.is_complete:
            return
        isolated = self.separator is not None and self.o.reference_isolation
        pcm = self.voice_history_slice(span) if isolated else self.history_slice(span)
        if pcm is None:
            reason = "not_voice" if isolated else "expired"
            self._incr(f"reference_rejected_{reason}")
            self.publish_voice_progress(speaker, "collecting", policy.accepted_seconds, reason)
            return
        voice_share = None
        if isolated:
            mix = self.history_slice(span)
            if mix is not None:
                voice_share = signal.dbfs(signal.rms(pcm)) - signal.dbfs(signal.rms(mix))
        result = policy.offer(span, h.text, signal.dbfs(signal.rms(pcm)), self.clipped_fraction(pcm),
                              voice_share_db=voice_share)
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

    def live_reference_policy(self, speaker: SpeakerID) -> ReferencePolicy:
        # A pinned session voice is never upgraded: the actor sounds the same all session.
        return ReferencePolicy(speaker, ReferencePolicyConfig(
            allow_upgrade=not self.pipeline.config.pin_speaker_profiles,
            allow_empty_text=self.o.asr == "audio",
            min_voice_share_db=(-10.0 if self.o.background and self.o.reference_isolation
                                and self.o.asr == "audio" else None)))

    def note_speaker(self, event) -> None:
        """Multi-speaker: a speaker's first label prepares their voice. Tone and system voices
        are ready at once (the system voice is cast by pitch); a clone waits for a reference."""
        if isinstance(event, SpeakerAssign):
            self.try_remembered_voice(event.speaker)
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

    def try_remembered_voice(self, speaker: SpeakerID) -> None:
        """Reuse a saved reference only after a conservative voice match (ADR-0006)."""
        memory = self.voice_memory
        if (self.o.tts != "clone" or memory is None or self.tracker is None or speaker in self.memory_preparing
                or speaker in self.memory_reused or speaker in self.memory_failed):
            return
        policy = self.reference_policies.get(speaker)
        if policy is not None and policy.has_issued_profile:
            return
        vp = self.tracker.voiceprint(speaker)
        saved = memory.match(vp) if vp is not None else None
        profile = saved.profile(speaker) if saved is not None else None
        if profile is None:
            return
        self.memory_preparing.add(speaker)
        self.remembered_ids[speaker] = saved.identity_id
        self.publish_voice_progress(speaker, "preparing", reused=True)

        def run() -> None:
            started = native.now_ns()
            try:
                voices = prepare_all(self.synthesizer, profile)
                self.inbox.put(("remembered_ready", voices[0], profile.quality.duration_seconds,
                                native.now_ns() - started, voices))
            except Exception:
                self.inbox.put(("remembered_failed", speaker))

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
        memory = self.voice_memory
        import uuid

        memory_id = self.remembered_ids.get(plan.speaker) or str(uuid.uuid4()).upper()
        self.remembered_ids[plan.speaker] = memory_id

        def run() -> None:
            started = native.now_ns()
            try:
                voices = prepare_all(self.synthesizer, profile)
                if memory is not None:
                    memory.remember(profile, memory_id)
                self.inbox.put(("prepared", voices[0], seconds, native.now_ns() - started, voices))
            except Exception as error:
                self.inbox.put(("prepare_failed", plan.speaker, plan.version, f"{error}"))

        threading.Thread(target=run, daemon=True).start()

    def publish_voice_progress(self, speaker: SpeakerID, state: str, seconds: float = 0.0,
                               reason: str | None = None, reused: bool = False) -> None:
        if not self.o.events_stdout:
            return
        self._stdout(json.dumps({"reused": reused, "required_seconds": ReferencePolicyConfig().min_seconds,
                                 "seconds": seconds, "speaker": speaker.raw_value, "voice_status": state,
                                 **({"reason": reason} if reason else {})},
                                sort_keys=True))

    def history_slice(self, span: SourceSpan) -> np.ndarray | None:
        if span.start < self.history_start:
            return None
        lo = span.start - self.history_start
        hi = min(len(self.history), span.end - self.history_start)
        return self.history[lo:hi].copy() if lo < hi else None

    def voice_history_slice(self, span: SourceSpan) -> np.ndarray | None:
        if span.start < self.voice_history_start:
            return None
        lo = span.start - self.voice_history_start
        hi = min(len(self.voice_history), span.end - self.voice_history_start)
        pcm = self.voice_history[lo:hi].copy() if lo < hi else None
        return pcm if pcm is not None and len(pcm) == span.count else None

    def source_clock_frontier(self) -> int:
        elapsed = max(0, self.now - self.last_capture_now)
        return self.src_frames + elapsed * CANONICAL // 1_000_000_000

    def flush_bed(self, final: bool = False) -> None:
        if self.bed is None or self.background_failed:
            return
        mixed = self.bed.drain(self.source_clock_frontier())
        pcm = self.background_resampler.process(mixed)
        if final:
            pcm = np.concatenate([pcm, self.background_resampler.flush()])
        if not len(pcm):
            return
        written = self.background_ring.write(pcm)
        self.background_frames += written
        if self.background_writer is not None:
            self.background_writer.append(pcm[:written])
        if written != len(pcm):
            raise AdapterError("background bus overflow")

    def fail_background(self) -> None:
        if self.background_failed:
            return
        self.background_failed = True
        # A failed separator mutes only the bed; original audio is never substituted for it.
        self.engine.set_background_gain(0, 0)
        if self.background_ring is not None:
            self.background_ring.skip(self.background_ring.readable)

    def process_separation(self, samples: np.ndarray) -> list[SourceSpan]:
        if self.separator is None or self.background_failed:
            return []
        self.separator_input = np.concatenate([self.separator_input, samples])
        complete = len(self.separator_input) // 480 * 480
        if complete:
            self.separator.separate(self.separator_input[:complete])
            self.separator_input = self.separator_input[complete:].copy()
        segments: list[SourceSpan] = []
        for voice, bed in self.separator.drain():
            self.bed.push(bed)
            self.separation_output += len(voice)
            if self.separation_output <= self.voice_lag:
                continue  # discard separator cold-start lookahead, aligning voice to source time
            corrected_start = self.separation_output - self.voice_lag - len(voice)
            self.voice_history = np.concatenate([self.voice_history, voice])
            keep = CANONICAL * 30
            if len(self.voice_history) > keep + CANONICAL * 10:
                drop = len(self.voice_history) - keep
                self.voice_history_start += drop
                self.voice_history = self.voice_history[drop:]
            if self.segment_vad is None:
                continue
            span = SourceSpan(corrected_start, corrected_start + len(voice))
            self.segment_frontier = span.end
            db = signal.dbfs(signal.rms(voice))
            if self.segment_threshold is not None:
                self.segment_vad.threshold_db = np.float32(self.segment_threshold.update(span, db))
            low = self.voice_to16k.process(voice)
            events = self.segment_vad.process(AudioFrame16k(span, low))
            segments += self.audio_segments.process(events, span.end, (span, db))
        return segments

    def following_silence_seconds(self, span: SourceSpan) -> float:
        next_start = next((other.start for other in self.recent_segments if other.start >= span.end), None)
        if next_start is not None:
            return max(0.0, (next_start - span.end) / CANONICAL)
        if self.segment_vad is not None:
            return max(0.0, self.segment_frontier - self.segment_vad.last_voiced_sample) / CANONICAL
        return 0.0

    @staticmethod
    def clipped_fraction(pcm) -> float:
        a = np.asarray(pcm)
        return float(np.sum(np.abs(a) >= 0.999)) / max(1, len(a))

    def clock(self) -> int:
        if self.virtual:
            return self.t0 + (self.src_frames - self.synthetic_frames) * 1_000_000_000 // CANONICAL + self.idle_ns
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
                # Picture delay: how far from its slot each phrase was scheduled (positive = late).
                if e.kind == TraceKind.PLAY_SCHEDULED and e.value("late_ms") is not None:
                    self.slot_late_ms.append(e.value("late_ms"))
                # Run-summary counters: how each line left the session.
                if e.kind == TraceKind.SOURCE_DISPOSITION and e.value("disposition") == "silence":
                    self._incr("silence_units")
                if e.kind == TraceKind.COMMIT_TARGET:
                    if e.value("shortened") is True:
                        self._incr("shortened_targets")
                    if e.value("speaker_carried") is True:
                        self._incr("speaker_carried_units")
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
                if self.virtual:
                    self.start_synthesis(effect.job)
                else:
                    self.synthesis_queue.append(effect.job)
            elif isinstance(effect, P.CancelTranslation):
                task = self.translation_tasks.pop(effect.unit, None)
                if task:
                    task.cancelled = True
                    if hasattr(self.translator, "cancel_unit"):
                        self.translator.cancel_unit(effect.unit)  # skip it in the worker queue
                self.translation_queue = [r for r in self.translation_queue if r.unit_id != effect.unit]
                self.translation_audio.pop(effect.unit, None)
                self.translation_tickets.pop(effect.unit, None)
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
            elif isinstance(effect, P.BackgroundGain):
                self.engine.set_background_gain(0 if self.background_failed else effect.gain,
                                                effect.duration_ms * self.out_rate // 1_000)
            elif isinstance(effect, P.RestoreOriginalVoice):
                if self.restores_voice and not self.background_failed:
                    pcm = self.voice_history_slice(effect.span)
                    replaced = self.bed.replace(effect.span, pcm) if pcm is not None else 0
                    self._incr("voice_restored_units")
                    if replaced < effect.span.count:
                        self._incr("voice_restored_late")
            elif isinstance(effect, (P.Teardown, P.EnqueueProviderPCM)):
                pass  # no cloud provider stream in this driver
        self.launch_next_translation()
        if self.can_start_synthesis and self.synthesis_queue:
            self.start_synthesis(self.synthesis_queue.pop(0))

    @property
    def can_start_synthesis(self) -> bool:
        """With two workers the back lane starts only once the front phrase is playing: order is
        kept and at most one future phrase is buffered."""
        if self.virtual:
            return True
        if len(self.active_synthesis) >= self.synthesis_lanes:
            return False
        if self.synthesis_lanes > 1 and self.o.presentation_delay_ms == 0:
            if len(self.plays) >= 2:
                return False
            if self.plays and not any(play.started for play in self.plays.values()):
                return False
        return True

    def _cancel_synthesis(self, unit: UnitID) -> None:
        task = self.tasks.pop(unit, None)
        if task:
            task.cancelled = True
            if task.stream is not None and hasattr(task.stream, "cancel"):
                task.stream.cancel()
        self.synthesis_queue = [j for j in self.synthesis_queue if j.unit != unit]
        self.active_synthesis.pop(unit, None)
        self.unit_resamplers.pop(unit, None)
        self.deliveries = [d for d in self.deliveries
                           if not (d.event[0] in ("chunk", "finished", "failed") and d.event[1].unit == unit)]

    def schedule(self, event: tuple, after: int) -> None:
        self.seq += 1
        self.deliveries.append(_Delivery(self.now + after, self.seq, event))

    def feed_hy(self, closed: list[SourceSpan]) -> None:
        """Stream open speech and redeem closed spans in order without blocking capture."""
        hy = self.hy_streaming
        if hy is None:
            return
        voiced_end = self.segment_vad.voiced_end if self.segment_vad is not None else self.vad.voiced_end
        actions = self.audio_feed.update(closed, self.audio_segments.open_start, voiced_end)
        for action in actions:
            if isinstance(action, Append):
                pcm = self.history_slice(action.span)
                if pcm is None or len(pcm) != action.span.count:
                    hy.discard()
                    self.audio_feed = AudioStreamFeed(self.audio_segments.stream_limit_samples)
                    return
                hy.stream(pcm)
            elif isinstance(action, Close):
                if len(self.hy_tickets) >= 16:
                    self.hy_tickets.pop(0)
                self.hy_tickets.append((action.span, hy.close(span_samples=action.span.count)))
            elif isinstance(action, Discard):
                hy.discard()

    def reset_audio_translation(self) -> None:
        self.audio_segments = (AudioSegmentPolicy.sentences(self.o.presentation_delay_ms / 1000)
                               if self.o.asr == "audio" and self.o.sentence_units and self.o.background
                               and self.o.presentation_delay_ms > 0 else AudioSegmentPolicy())
        self.audio_feed = AudioStreamFeed(self.audio_segments.stream_limit_samples)
        self.hy_tickets.clear()
        self.translation_audio.clear()
        self.translation_tickets.clear()
        self.recent_segments.clear()
        self.segment_frontier = self.src_frames
        if self.separator is not None:
            self.separator.reset()
            self.separation_output = self.src_frames
            self.voice_history = np.zeros(0, np.float32)
            self.voice_history_start = self.src_frames
            self.separator_input = np.zeros(0, np.float32)
            self.bed.reset(self.src_frames)
            self.background_ring.skip(self.background_ring.readable)
            self.background_resampler = StreamingResampler(CANONICAL, self.out_rate)
            self.segment_vad = vad_for(self.o) if self.o.asr == "audio" else None
            if self.segment_vad is not None:
                self.segment_vad.threshold_db = np.float32(self.o.vad_threshold_db - 5)
            self.segment_threshold = (AdaptiveSpeechThreshold(self.o.vad_threshold_db - 5)
                                      if self.audio_segments.dip_window_samples else None)
            self.voice_to16k = StreamingResampler(CANONICAL, 16_000) if self.segment_vad is not None else None
        if self.o.mt in ("hy", "hy-local") and hasattr(self.translator, "reset"):
            self.translator.reset()

    def start_translation(self, req) -> None:
        if self.o.asr == "audio":
            span = req.source_span
            ticket_index = next((i for i, item in enumerate(self.hy_tickets) if item[0] == span), None)
            if ticket_index is not None:
                self.translation_tickets[req.unit_id] = self.hy_tickets[ticket_index][1]
                del self.hy_tickets[: ticket_index + 1]
                self.translation_queue.append(req)
                self.launch_next_translation()
                return
            if self.hy_streaming is not None:
                # Whole-span sending now would interleave with the next open streamed span.
                self.inbox.put(("input", P.Translation(req.unit_id, P.Failure("Hy-SimulMT span was not streamed"))))
                return
            audio = self.history_slice(span) if span is not None else None
            if audio is None or len(audio) != span.count:
                self.inbox.put(("input", P.Translation(req.unit_id, P.Failure("Hy-SimulMT source audio unavailable"))))
                return
            self.translation_audio[req.unit_id] = audio
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
        audio = self.translation_audio.pop(req.unit_id, None)
        ticket = self.translation_tickets.pop(req.unit_id, None)
        modeled = (self.translator_latency.nanoseconds(len(req.uncommitted_source))
                   if self.translator_latency else 0)

        def run() -> None:
            started = native.now_ns()
            try:
                if ticket is not None:
                    outcome = P.Success(self.hy_streaming.answer(ticket, req.unit_id))
                elif audio is not None:
                    outcome = P.Success(self.translator.translate(req.unit_id, audio))
                else:
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
        lane = 0
        if not self.virtual:
            if not self.can_start_synthesis:
                self.synthesis_queue.append(job)
                return
            lane = next(n for n in range(self.synthesis_lanes) if n not in self.active_synthesis.values())
            self.active_synthesis[job.unit] = lane
        synthesizer = self.synthesizer
        key = f"{job.speaker.raw_value}/{job.profile_version}"
        # Virtual clock nodes run every job inline on lane 0 and never touch a worker.
        lanes = None if self.virtual else self.prepared_lane_voices.get(key)
        voice = (lanes[lane] if lanes and lane < len(lanes) else None) or self.prepared_voices.get(key) or (
            PreparedVoice(job.speaker, job.profile_version, 0))
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
        spoken = self.pronunciation.apply(job.text)
        # The actor's own line conditions this take only; it remains in memory and dies with the job.
        delivery = None
        if job.delivery_allowed:
            pcm = self.history_slice(job.span)
            if pcm is not None:
                delivery = DeliveryClip(pcm, CANONICAL, self.pipeline.source_text(job.unit) or "")

        def run() -> None:
            try:
                if tone is not None:
                    time.sleep(tone.first_chunk.nanoseconds(0) / 1e9)
                    stream = tone.synthesize(job.unit.raw_value, spoken)
                elif getattr(synthesizer, "supports_fill", False):
                    stream = synthesizer.synthesize(job.unit.raw_value, spoken, voice, job.target_seconds,
                                                    allow_fill=job.allow_fill, lane=lane, delivery=delivery)
                else:
                    stream = synthesizer.synthesize(job.unit.raw_value, spoken, voice, job.target_seconds)
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
            self.active_synthesis.pop(job.unit, None)
            self.trace_synthesis_done(job)
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
            self.active_synthesis.pop(job.unit, None)
            self.send(P.SynthesisFailed(job.unit, message))
        elif kind == "prepared":
            voice, seconds, prepare_ns = event[1], event[2], event[3]
            voices = event[4] if len(event) > 4 else [voice]
            self.preparing_reference = False
            key = f"{voice.speaker_id.raw_value}/{voice.profile_version}"
            self.prepared_voices[key] = voice
            self.prepared_lane_voices[key] = voices
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
        elif kind == "remembered_ready":
            voice, seconds, prepare_ns = event[1], event[2], event[3]
            self.memory_preparing.discard(voice.speaker_id)
            self.memory_reused.add(voice.speaker_id)
            self.reference_audio.pop(voice.speaker_id, None)
            self.reference_policies.pop(voice.speaker_id, None)
            key = f"{voice.speaker_id.raw_value}/{voice.profile_version}"
            self.prepared_voices[key] = voice
            self.prepared_lane_voices[key] = event[4]
            self._incr("reference_memory_reused")
            self.profile_log.append({"speaker": voice.speaker_id.raw_value, "version": voice.profile_version,
                                     "reference_seconds": seconds, "prepare_seconds": prepare_ns / 1e9,
                                     "source_seconds": self.src_frames / CANONICAL, "reused": True})
            self.publish_voice_progress(voice.speaker_id, "ready", seconds, reused=True)
            self.send(P.ProfileReady(voice.speaker_id, voice.profile_version))
        elif kind == "remembered_failed":
            speaker = event[1]
            self.memory_preparing.discard(speaker)
            self.memory_failed.add(speaker)
            self._incr("reference_memory_failed")
            self.publish_voice_progress(speaker, "collecting")
        elif kind == "prepare_failed":
            speaker, version, message = event[1], event[2], event[3]
            self.preparing_reference = False
            if version == 1:
                self.reference_policies[speaker] = self.live_reference_policy(speaker)
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

    def trace_synthesis_done(self, job: P.SynthesisJob) -> None:
        """`tts.done`: the worker's pacing of a completed unit, numbers only."""
        take = getattr(self.synthesizer, "take_stats", None)
        done = take(job.unit.raw_value) if take else None
        if done is None:
            return
        if done.truncated:
            self._incr("tts_truncated_units")
        if done.retries > 0:
            self._incr("tts_silent_retries", done.retries)
        delivery = getattr(done, "delivery", "absent")
        if delivery != "absent":
            self._incr("delivery_used_units" if delivery == "line" else "delivery_refused_units")
            if delivery != "line":
                self._incr(f"delivery_{delivery}")
            self.stats["delivery_prep_ms_max"] = max(
                self.stats.get("delivery_prep_ms_max", 0), round_half_away(done.delivery_prep_seconds * 1000))

        def ms(seconds: float) -> int:
            return round_half_away(seconds * 1000)

        fields = [f_unit(job.unit), f_double("pace", done.pace), f_int("native_ms", ms(done.native_seconds)),
                  f_int("audio_ms", ms(done.audio_seconds)), f_int("quiet_removed_ms", ms(done.quiet_removed_seconds))]
        if done.budget_seconds > 0:
            fields.append(f_int("budget_ms", ms(done.budget_seconds)))
        # The character-count cap stopped generation: this line lost its tail.
        if done.truncated:
            fields.append(f_bool("truncated", True))
        # A silent first take was discarded and generated again with another seed.
        if done.retries > 0:
            fields.append(f_int("retries", done.retries))
        if delivery != "absent":
            fields.append(f_str("delivery", delivery))
            fields.append(f_int("delivery_ms", ms(done.delivery_seconds)))
            fields.append(f_int("delivery_prep_ms", ms(done.delivery_prep_seconds)))
        if job.target_seconds is not None:
            fields.append(f_int("target_ms", ms(job.target_seconds)))
        if not self.pronunciation.is_empty:
            fields.append(f_bool("respelled", self.pronunciation.apply(job.text) != job.text))
        gate = self.pipeline.gate
        self.trace.emit(TraceEvent(self.now, self.o.session, gate.epoch, gate.generation, TraceKind.TTS_DONE,
                                   tuple(fields)))

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

    def step(self, chunk: np.ndarray | None, frames: int, synthetic: bool = False) -> None:
        """`synthetic`: silence that is not source time (a pause flush). It renders no output
        span of its own."""
        self.now = self.clock()
        if chunk is not None:
            self.idle_since = None
            self.pause_flushed = False
            self.last_capture_now = self.now
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
        if self.live is not None:
            reason = self.live.take_discontinuity()
            if reason:
                log(f"{reason}; translation restarts from the next phrase")
                self.send(P.Discontinuity(reason))
                self.reset_audio_translation()
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
        segments = []
        if self.separator is not None:
            try:
                segments = self.process_separation(samples)
            except Exception:
                self.fail_background()
        elif self.o.asr == "audio":
            segments = self.audio_segments.process(vad_events, span.end)
        if self.o.asr == "audio":
            # The worker hears the span before the reducer commits it, hiding all but the last turn.
            self.feed_hy(segments)
            for segment in segments:
                self.send(P.AudioSegment(segment))
                self.send(P.FollowingSilence(segment, self.following_silence_seconds(segment)))
                self.recent_segments.append(segment)
                if len(self.recent_segments) > 32:
                    self.recent_segments.pop(0)
                if self.restores_voice and self.lid is not None:
                    voice = self.voice_history_slice(segment)
                    if voice is not None:
                        self.lid.identify(segment, voice)
                    else:
                        self._incr("lid_failed")
                if self.acquires_reference:
                    h = ASRHypothesis(segment.start, (ASRToken("", segment),), True, segment.end)
                    self.acquire_reference(h)
            for closed in self.recent_segments:
                self.send(P.FollowingSilence(closed, self.following_silence_seconds(closed)))
            if self.lid is not None:
                for source_span, matches in self.lid.drain():
                    self._incr("lid_same_language" if matches else "lid_translate")
                    self.send(P.SourceLanguage(source_span, matches))
        else:
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
                               (self.synthesizer, "voice worker"), (self.separator, "separator"),
                               (self.lid, "language identifier")):
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
        try:
            self.flush_bed()
        except AdapterError:
            self.fail_background()
        self.render(samples if chunk is not None else None, 0 if synthetic else frames)
        if self.now >= self.last_tick + 100_000_000:
            self.last_tick = self.now
            self.send(P.TranslatedBacklog(self.translated_ring.readable / self.out_rate))
            self.send(P.Tick())
        if self.now >= self.last_resource + 1_000_000_000:
            self.last_resource = self.now
            self.emit_resource()
        self.trace.flush()

    def idle_step(self, frames: int) -> None:
        """A period without capture. Model results, playout tracking, ticks and phrase slots still
        advance by the clock; nothing touches the source timeline, VAD, or tracker."""
        self.now = self.clock()
        # No frames is not silence: the phrase spoken just before a pause would stay open until
        # capture resumes and miss its slot. Once per pause, 0.6 s of silence closes it through
        # the usual VAD rules. It gets no capture anchor, so slots stay on captured time.
        since = self.idle_since if self.idle_since is not None else self.now
        self.idle_since = since
        if not self.pause_flushed and self.now - since >= 300_000_000:
            self.pause_flushed = True
            flush = CANONICAL * 3 // 5
            self.step(None, flush, synthetic=True)
            self.synthetic_frames += flush
            self.idle_since = since
            self.pause_flushed = True
            return
        if self.controls is not None:
            mode = self.controls.take_latest()
            if mode is not None:
                self.send(P.SetPlaybackMode(mode))
        while True:
            try:
                event = self.inbox.get_nowait()
            except queue.Empty:
                break
            self.deliver(event)
        self.deliveries.sort()
        while self.deliveries and self.deliveries[0].at <= self.now:
            self.deliver(self.deliveries.pop(0).event)
        # The device renders on its own; a file replay renders the same span of output.
        try:
            self.flush_bed()
        except AdapterError:
            self.fail_background()
        self.render(None, frames if self.live is None else 0)
        # Every input re-checks slots, so due phrases start within one idle step.
        self.send(P.TranslatedBacklog(self.translated_ring.readable / self.out_rate))
        if self.now >= self.last_tick + 100_000_000:
            self.last_tick = self.now
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
                if len(chunk):
                    self.step(chunk, len(chunk))
                else:
                    self.idle_step(0)
                if self.runtime_failed:
                    break
            log(f"stopping: finishing in-flight units over live audio (up to {o.tail_seconds} s)")
            self.recognizer.request_finalize()
            while not self.runtime_failed and source.failure is None and tail < tail_frames:
                chunk = source.next_live(until_stop=False)
                if chunk is None:
                    break
                if len(chunk):
                    self.step(chunk, len(chunk))
                else:
                    self.idle_step(0)
                tail += len(chunk)
                if tail >= CANONICAL and self.idle:
                    break
        else:
            realtime_file = not self.virtual and source.total_frames is not None and o.pace == "realtime"
            wall_start = native.now_ns()
            gap = o.capture_gap
            while (chunk := source.next()) is not None:
                if gap is not None and self.src_frames / CANONICAL >= gap[0]:
                    at, seconds = gap
                    gap = None
                    for _ in range(round_half_away(seconds * 50)):
                        self.idle_ns += 20_000_000
                        self.idle_step(CANONICAL // 50)
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
        try:
            self.flush_bed(final=True)
        except AdapterError:
            self.fail_background()
        self.send(P.Stop())
        for adapter in (self.synthesizer, self.translator, self.recognizer, self.separator, self.lid):
            if hasattr(adapter, "shutdown"):
                adapter.shutdown()
        if self.live is not None:
            self.live.settle_gains(self.gain_target, self.pipeline.config.health.ramp_ms + 50)
            self.live.stop()
        self.write_rendered()
        self.trace.close()
        if self.voice_memory is not None:
            self.voice_memory.flush()
        for w in (self.out_writer, self.stem_writer, self.source_writer, self.translated_writer,
                  self.rendered_writer, self.background_writer):
            if w:
                w.close()
        self.summary()

    def slot_summary(self) -> dict:
        """Slot timing: phrases scheduled, median and worst lateness, and how many started within
        250 ms of their slot (scheduling time, before the device buffer)."""
        if not self.slot_late_ms:
            return {"scheduled": 0}
        ordered = sorted(self.slot_late_ms)
        return {"scheduled": len(ordered), "median_late_ms": ordered[len(ordered) // 2], "max_late_ms": ordered[-1],
                "within_250ms": sum(1 for ms in ordered if abs(ms) <= 250)}

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
            "asr_dropped_finals": getattr(self.recognizer, "dropped_finals", 0),
            "asr_forced_splits": getattr(self.recognizer, "forced_splits", 0),
            "final_state": str(self.pipeline.state), "runtime_failed": self.runtime_failed,
            "captions_only": o.captions_only, "translation_only": o.translation_only,
            "output_sample_rate": self.out_rate, "playback": self.pipeline.playback_mode.value, "fluent": o.fluent,
            "presentation_delay_ms": o.presentation_delay_ms, "turn_fill": o.turn_fill, "slot": self.slot_summary(),
            "adaptive_prebuffer": o.adaptive_prebuffer, "quality_first": o.quality_first,
            "background": {"enabled": o.background, "failed": self.background_failed,
                           "frames": self.background_frames, "backend": "rnnoise" if self.separator else None},
            "rendered_frames_recorded": self.rendered_frames,
            "rendered_capture_overflow_frames": self.engine.rendered.overflow_frames if self.engine.rendered else 0,
            "silence_hypotheses_rejected": self.stats.get("silence_hypotheses_rejected", 0),
            "translated_starved_frames": self.engine.counter("translated_starved_frames"),
            "max_debt_seconds": self.pipeline.config.health.max_debt_seconds,
            "max_source_lag_seconds": self.pipeline.config.health.max_source_lag_seconds,
            "max_unit_keys": self.pipeline.config.commit.max_unit_keys,
            "max_unit_seconds": self.pipeline.config.commit.max_unit_source_seconds,
            # Cuts stay off Han words only where the system segmenter is available.
            "word_cuts": self.word_segmenter is not None or self.target_word_segmenter is not None,
            "speaker_carry_seconds": self.pipeline.config.unlabeled_speaker_carry_seconds,
            "speaker_carry_after_pause_seconds": self.pipeline.config.unlabeled_speaker_carry_after_pause_seconds,
            "speaker_carry_max_piece_seconds": self.pipeline.config.unlabeled_speaker_carry_max_piece_seconds,
            "speaker_carried_units": self.stats.get("speaker_carried_units", 0),
            "shorten_targets": self.pipeline.config.validator.shortens_over_budget_targets,
            "shortened_targets": self.stats.get("shortened_targets", 0),
            "filler_silence": self.pipeline.config.validator.silences_fillers,
            "silence_units": self.stats.get("silence_units", 0),
            "tts_truncated_units": self.stats.get("tts_truncated_units", 0),
            "tts_silent_retries": self.stats.get("tts_silent_retries", 0),
            "speakers": {"mode": o.speakers, "detected": 1 if self.tracker is None else len(self.known_speakers)},
            "audio_units": {"max_seconds": self.audio_segments.max_samples / CANONICAL,
                            "detector": "voice" if self.segment_vad is not None else "session",
                            "pause_closes": self.audio_segments.pause_closes,
                            "cap_closes": self.audio_segments.cap_closes},
            "passthrough": {"enabled": self.restores_voice, "identifier": self.lid is not None,
                            "hold_seconds": self.bed.delay_frames / CANONICAL if self.bed else 0,
                            "same_language": self.stats.get("lid_same_language", 0),
                            "translated": self.stats.get("lid_translate", 0),
                            "failed": self.stats.get("lid_failed", 0),
                            "voice_restored_units": self.stats.get("voice_restored_units", 0),
                            "voice_restored_late": self.stats.get("voice_restored_late", 0)},
            "runtime": {"implementation": "portable", "platform": sys.platform, "vad": o.vad,
                        "dropped_finals": getattr(self.recognizer, "dropped_finals", 0),
                        "forced_splits": getattr(self.recognizer, "forced_splits", 0),
                        "adapters": {k: getattr(a, "describe", lambda: getattr(a, "name", type(a).__name__))()
                                     for k, a in (("asr", self.recognizer), ("mt", self.translator),
                                                  ("tts", self.synthesizer))}},
        }
        if o.mt in ("hy", "hy-local"):
            info = getattr(self.translator, "info", None)
            s["speech_translation"] = {
                "backend": o.mt, "audio_leaves_machine": o.mt == "hy",
                "streaming": self.hy_streaming is not None,
                "model": info.model if info else "",
                "load_seconds": info.load_seconds if info else 0,
                "warmup_seconds": info.warmup_seconds if info else 0}
        if self.tracker is not None and o.tts != "clone":
            s["voices"] = self.profile_log
        info = getattr(self.synthesizer, "info", None)
        if o.tts == "clone":
            s["clone"] = {"model": info.model if info else "", "selected_model": o.clone_model,
                          "audio_output_streaming": bool(caps.audio_output_streaming) if caps else False,
                          "conditioning": o.clone_mode, "delivery": o.clone_delivery,
                          "reference": "fixed" if o.voice_ref else "acquired",
                          "profiles": self.profile_log, "load_seconds": info.load_seconds if info else 0,
                          "warmup_seconds": info.warmup_seconds if info else 0,
                          "workers": getattr(self.synthesizer, "worker_count", 1),
                          "session_voice_locked": self.pipeline.config.pin_speaker_profiles,
                          "speaker_attribution_required": (self.pipeline.config.require_speaker_attribution
                                                           and self.pipeline.config.multi_speaker),
                          "reference_stats": {k: v for k, v in self.stats.items() if k.startswith("reference_")},
                          "delivery_stats": {k: v for k, v in self.stats.items() if k.startswith("delivery_")}}
        if self.live is not None:
            s["mode"] = "tap"
            s["output_frames"] = self.engine.counter("output_frames")
            s["tap"] = self.live.summary()
        self._stdout(json.dumps(s, sort_keys=True))


def word_segmenter_for(language):
    """Windows' word segmenter for a Mandarin side, as the Swift host injects NaturalLanguage's:
    per-character keys hide Han words, so only a segmenter keeps a cut out of one. None for
    space-delimited languages (the commit policy's Latin rule) or where none is available."""
    if language is not TranslationLanguage.MANDARIN or sys.platform != "win32":
        return None
    from .models.word_segmenter import WindowsWordSegmenter

    try:
        return WindowsWordSegmenter(language.translation_identifier)
    except OSError as error:
        log(f"word segmenter unavailable ({error}); clause cuts follow the commit rules only")
        return None


def vad_for(o):
    # --vad silero is refused by option validation until the manifest pins a Silero revision.
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
