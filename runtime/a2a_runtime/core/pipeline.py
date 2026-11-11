"""Session pipeline reducer. Port of apple/Sources/A2ACore/SessionPipeline.swift.

Commit policy + translation validation + synthesis scheduling + playout gate + state machine
+ debt accounting. Pure: inputs (with their event time) in, effects out. Drivers execute the
effects with real adapters and feed results back, so the same decisions run in golden tests,
replay, and live sessions on every platform. Keep this file in lockstep with the Swift
source; tests/test_golden.py replays the Swift golden traces byte for byte.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from . import state_machine as sm
from .commit_policy import (Abandoned, Commit, CommitPolicy, CommitPolicyConfig, Hold, RequestFinalizeOut,
                            RevisionBehindFrontier, SourceUnit)
from .direction import TranslationDirection, TranslationLanguage
from .playout_gate import PlayoutGate
from .state_machine import BusGains, HealthSample, HealthThresholds, SessionStateMachine, UnitReadiness
from .commit_policy import CommitReason
from .text import char_count, trim_whitespace
from .trace import TraceEvent, TraceKind, f_bool, f_double, f_int, f_span, f_speaker, f_str, f_unit
from .types import (CANONICAL_SAMPLE_RATE, ASRHypothesis, CommittedPair, PCMChunkHeader, SourceSpan, SpeakerAssign,
                    SpeakerChange, SpeakerID, SpeechEnd, SpeechStart, TranslationCandidate, TranslationRequest, UnitID)
from .validator import Accepted, Rejected, Silent, TranslationValidator


class PlaybackMode(str, Enum):
    ORIGINAL = "original"
    TRANSLATED = "translated"


def round_half_away(x: float) -> int:
    """Swift `Double.rounded()` (schoolbook rounding), not Python's banker's rounding."""
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


@dataclass
class PipelineConfig:
    session: str
    direction: TranslationDirection = TranslationDirection.CHINESE_TO_ENGLISH
    commit: CommitPolicyConfig = field(default_factory=CommitPolicyConfig)
    health: HealthThresholds = field(default_factory=HealthThresholds)
    validator: TranslationValidator | None = None
    # Preceding committed source/target pairs sent with each request.
    context_pairs: int = 3
    glossary: dict = field(default_factory=dict)
    default_speaker: SpeakerID = SpeakerID("S1")
    # Debt estimate for committed text that has no PCM yet.
    estimated_seconds_per_source_char: float = 0.25
    estimated_seconds_per_target_char: float = 1.0 / 15.0
    # Research mode includes text in trace records; product diagnostics omit it (D9).
    trace_text: bool = False
    # Bounds model work even while acquiring a voice or recovering from a failure.
    max_pending_units: int = 8
    # Captions are delivered in memory, separately from persisted diagnostics.
    emit_captions: bool = False
    # Translation-only sessions stay silent while preparing or recovering.
    original_audio_enabled: bool = True
    # Explicit listening choice. Translation continues when the original bus is selected.
    playback_mode: PlaybackMode = PlaybackMode.TRANSLATED
    # Fluent playout: soft pressure never cuts the phrase being heard and never falls back.
    finish_phrase_before_catch_up: bool = False
    # Experimental: observe at least two chunks and reserve enough audio for measured
    # generation slower than playout. Completed phrases can always start immediately.
    adaptive_streaming_prebuffer: bool = False
    # Quality-first live playback admits a whole synthesized phrase at once. Capture,
    # recognition and translation continue while it waits; the queue stays bounded.
    complete_phrases_before_playback: bool = False
    # Separated music/effects, never the unprocessed source track.
    background_audio_enabled: bool = False
    # Dialogue (ADR-0005): each unit takes the speaker who covers most of its span.
    multi_speaker: bool = False
    # Keep each speaker's first prepared voice until stop/discontinuity.
    pin_speaker_profiles: bool = False
    # Clone dialogue must have a majority source label before choosing a voice.
    require_speaker_attribution: bool = False
    # How far unlabeled speech may reach back to the previous label, in seconds, when no pause
    # separated them: inside one uninterrupted stretch the same actor keeps talking. 0 disables.
    unlabeled_speaker_carry_seconds: float = 6.0
    # How far a *brief* unlabeled piece may reach back across a pause: a short reply or the
    # tail of a sentence (reports/week2/live-session-recovery.md).
    unlabeled_speaker_carry_after_pause_seconds: float = 3.0
    # A piece this long or longer is a turn of its own: after a pause it waits for its own label.
    unlabeled_speaker_carry_max_piece_seconds: float = 1.0
    # Direct speech translation receives bounded source audio, not recognized text.
    direct_audio_translation: bool = False
    # Provider emits translated PCM directly, without locally committed units.
    provider_streaming: bool = False
    # Picture delay D in seconds (ADR-0012). The host shows the source picture D seconds late
    # and delays the original and separated-background buses to match, so each unit plays in
    # its own slot: never before its source start + D. Lag limits then count from the slot,
    # and audio still early for its slot is not debt. 0 keeps live playback unchanged. Needs
    # fluent playout (`finish_phrase_before_catch_up`), which holds PCM.
    presentation_delay_seconds: float = 0.0
    # Turn fill (ADR-0013): slow a take that comes out shorter than the speech it translates so
    # the line ends with its actor. Applies only with a presentation delay.
    turn_fill_enabled: bool = True
    # Offer each unit's own source audio for per-line delivery conditioning (ADR-0015).
    clone_delivery_line: bool = False
    # Let the host restore a span's isolated voice for passthrough or a failed translation (ADR-0018).
    restores_original_voice: bool = False
    # Latest source age at which restoration is still covered by the host's held background bed.
    original_voice_decision_deadline_seconds: float = 0.0
    # Only a stalled request is given up at that deadline: a unit as long as the deadline is still
    # being answered when it passes (Hy answered within 1.6 s of close at p99 live; labels 2.7 s).
    translation_stall_seconds: float = 2.0
    speaker_label_stall_seconds: float = 3.0

    @staticmethod
    def original_voice_hold_seconds(presentation_delay_seconds: float) -> float:
        return min(6.0, presentation_delay_seconds - 1.5)

    @staticmethod
    def original_voice_decision_deadline(presentation_delay_seconds: float) -> float:
        return max(0.1, PipelineConfig.original_voice_hold_seconds(presentation_delay_seconds) - 0.7)

    def __post_init__(self) -> None:
        self.validator = TranslationValidator(target_language=self.direction.target)
        if self.direction.source is TranslationLanguage.ENGLISH:
            self.commit.prefer_word_boundaries = True
            self.estimated_seconds_per_source_char = 1.0 / 15.0
            # Measured cloned Mandarin pace (ADR-0017); 0.25 over-shortened replayed lines.
            self.estimated_seconds_per_target_char = 0.2
        # The spoken budget and the debt estimate must agree about how fast the target talks.
        self.validator.spoken_characters_per_second = 1 / self.estimated_seconds_per_target_char


# MARK: Inputs


@dataclass(frozen=True, slots=True)
class Arm:
    pass


@dataclass(frozen=True, slots=True)
class ArmSucceeded:
    pass


@dataclass(frozen=True, slots=True)
class ArmFailed:
    message: str


@dataclass(frozen=True, slots=True)
class Stop:
    pass


@dataclass(frozen=True, slots=True)
class SetPlaybackMode:
    mode: PlaybackMode


@dataclass(frozen=True, slots=True)
class ProviderPCM:
    """A cloud provider has emitted a PCM chunk (the payload stays in the driver)."""


@dataclass(frozen=True, slots=True)
class Captured:
    """A capture buffer covering `span` became available to the pipeline."""

    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Vad:
    event: SpeechStart | SpeechEnd


@dataclass(frozen=True, slots=True)
class Speaker:
    event: SpeakerAssign | SpeakerChange


@dataclass(frozen=True, slots=True)
class Hypothesis:
    h: ASRHypothesis


@dataclass(frozen=True, slots=True)
class AudioSegment:
    """A bounded span of source speech for a direct audio translator."""

    span: SourceSpan


@dataclass(frozen=True, slots=True)
class FollowingSilence:
    """Seconds of silence known to follow a direct-audio span when translation arrives."""

    span: SourceSpan
    seconds: float


@dataclass(frozen=True, slots=True)
class SourceLanguage:
    """Whether a direct-audio span is already spoken in the target language."""

    span: SourceSpan
    matches_target: bool


@dataclass(frozen=True, slots=True)
class ProfileReady:
    speaker: SpeakerID
    version: int


@dataclass(frozen=True, slots=True)
class Success:
    candidate: TranslationCandidate


@dataclass(frozen=True, slots=True)
class Failure:
    message: str


@dataclass(frozen=True, slots=True)
class Translation:
    unit: UnitID
    outcome: Success | Failure


@dataclass(frozen=True, slots=True)
class PCM:
    header: PCMChunkHeader
    frames: int


@dataclass(frozen=True, slots=True)
class SynthesisFinished:
    unit: UnitID
    generation: int


@dataclass(frozen=True, slots=True)
class SynthesisFailed:
    unit: UnitID
    message: str


@dataclass(frozen=True, slots=True)
class PlayStarted:
    unit: UnitID
    out_sample: int


@dataclass(frozen=True, slots=True)
class PlayEnded:
    unit: UnitID
    out_sample: int


@dataclass(frozen=True, slots=True)
class TranslatedBacklog:
    """Scheduled-but-unplayed translated audio reported by the mixer."""

    seconds: float


@dataclass(frozen=True, slots=True)
class Underrun:
    pass


@dataclass(frozen=True, slots=True)
class WorkerHealthy:
    ok: bool


@dataclass(frozen=True, slots=True)
class Tick:
    """Periodic (100 ms) health and queue sampling."""


@dataclass(frozen=True, slots=True)
class Discontinuity:
    """Source/device discontinuity or restart."""

    reason: str


# MARK: Effects


@dataclass(frozen=True, slots=True)
class SynthesisJob:
    unit: UnitID
    text: str
    speaker: SpeakerID
    profile_version: int
    epoch: int
    generation: int
    span: SourceSpan
    target_seconds: float | None = None
    # The host may slow a take that comes out shorter than `target_seconds`, so the line ends
    # where the actor stops (ADR-0013). Set only for a phrase that plays in a slot and is not
    # yet late for it; see `SessionPipeline._synthesize`.
    allow_fill: bool = False
    # Permission to offer this unit's own audio to a reference-conditioned synthesizer (ADR-0015).
    delivery_allowed: bool = False


@dataclass(frozen=True, slots=True)
class TraceOut:
    event: TraceEvent


@dataclass(frozen=True, slots=True)
class Caption:
    kind: str
    id: str
    text: str


@dataclass(frozen=True, slots=True)
class RequestFinalize:
    pass


@dataclass(frozen=True, slots=True)
class Translate:
    request: TranslationRequest


@dataclass(frozen=True, slots=True)
class Synthesize:
    job: SynthesisJob


@dataclass(frozen=True, slots=True)
class CancelTranslation:
    unit: UnitID


@dataclass(frozen=True, slots=True)
class CancelSynthesis:
    unit: UnitID


@dataclass(frozen=True, slots=True)
class HoldPCM:
    """Admitted PCM waits for a handoff decision."""

    header: PCMChunkHeader


@dataclass(frozen=True, slots=True)
class EnqueuePCM:
    """Admitted PCM goes straight to the translated bus."""

    header: PCMChunkHeader


@dataclass(frozen=True, slots=True)
class ReleaseHeld:
    """Move a held unit's PCM to the translated bus."""

    unit: UnitID


@dataclass(frozen=True, slots=True)
class DiscardHeld:
    unit: UnitID


@dataclass(frozen=True, slots=True)
class DropPCM:
    header: PCMChunkHeader
    reason: str


@dataclass(frozen=True, slots=True)
class EnqueueProviderPCM:
    pass


@dataclass(frozen=True, slots=True)
class RampGains:
    gains: BusGains
    duration_ms: int


@dataclass(frozen=True, slots=True)
class BackgroundGain:
    gain: float
    duration_ms: int


@dataclass(frozen=True, slots=True)
class FlushTranslatedBus:
    pass


@dataclass(frozen=True, slots=True)
class RestoreOriginalVoice:
    """Put the span's own isolated voice back under its delayed picture (ADR-0018)."""

    span: SourceSpan


@dataclass(frozen=True, slots=True)
class Teardown:
    pass


class SourceDisposition:
    TRANSLATED = "translated"
    ORIGINAL_ONLY = "original_only"
    ABANDONED = "abandoned"
    NEVER_COMMITTED = "never_committed"
    # The source held no speech to translate (empty or filler-only answer): silence by design.
    SILENCE = "silence"


class Stage:
    TRANSLATING = "translating"
    # Translated; the source has insufficient speaker labels.
    AWAITING_SPEAKER = "awaitingSpeaker"
    # Translated; its speaker's profile is not ready yet (multi-speaker).
    AWAITING_VOICE = "awaitingVoice"
    SYNTHESIZING = "synthesizing"
    HELD = "held"
    # Fluent playout holds the unit's PCM until the phrase ahead of it ends.
    WAITING = "waiting"
    QUEUED = "queued"
    PLAYING = "playing"
    FINISHED = "finished"
    CANCELED = "canceled"
    REJECTED = "rejected"

    SYNTHESIS_STARTED = frozenset([SYNTHESIZING, HELD, WAITING, QUEUED, PLAYING])
    LIVE = frozenset([TRANSLATING, AWAITING_SPEAKER, AWAITING_VOICE]) | SYNTHESIS_STARTED
    ON_BUS = frozenset([QUEUED, PLAYING])
    TERMINAL = frozenset([FINISHED, CANCELED, REJECTED])


@dataclass
class UnitRecord:
    source: SourceUnit
    speaker: SpeakerID
    stage: str = Stage.TRANSLATING
    target: str | None = None
    generation: int = 0
    buffered_seconds: float = 0.0
    first_pcm_at: int | None = None
    first_pcm_seconds: float = 0.0
    last_pcm_at: int | None = None
    pcm_chunks: int = 0
    synthesis_complete: bool = False
    scheduled: bool = False
    handoff: bool = False
    playback_suppressed: bool = False
    # Silence known to follow the span when its translation arrived (ADR-0018).
    following_silence: float | None = None


class SessionPipeline:
    def __init__(self, config: PipelineConfig, word_segmenter=None, target_word_segmenter=None) -> None:
        """`word_segmenter` lets a host keep clause cuts off Han words; golden traces and tests
        run without one. `target_word_segmenter` is the same service for the target language."""
        self.config = config
        self.gate = PlayoutGate()
        self.machine = SessionStateMachine(thresholds=config.health, generation=1, multi_speaker=config.multi_speaker)
        self.playback_mode = config.playback_mode
        self._word_segmenter = word_segmenter
        self._target_word_segmenter = target_word_segmenter
        self._policy = CommitPolicy(config.commit, word_segmenter=word_segmenter)
        self._units: dict[UnitID, UnitRecord] = {}
        self._order: list[UnitID] = []
        self._context: list[CommittedPair] = []
        self._last_target: str | None = None
        self._speaker = config.default_speaker
        self._speaker_assigned = False
        # Multi-speaker: tracker labels in source order, for attributing units by span.
        self._speaker_timeline: list[tuple[SourceSpan, SpeakerID]] = []
        # Most recent tracker label and where it ended, for the unlabeled-piece carry.
        self._last_label: tuple[SpeakerID, int] | None = None
        # Sample of the most recent VAD pause: a label is only carried across stretches with none.
        self._last_pause: int | None = None
        self._backlog_seconds = 0.0
        self._underrun_pending = False
        self._worker_healthy = True
        self._speech_onset: int | None = None
        self._in_speech = False
        self._anchors: list[tuple[int, int]] = []
        self._next_audio_unit = 1
        self._last_audio_end = 0
        self._now = 0
        self._provider_armed = False
        self._profile_versions: dict[SpeakerID, int] = {}

    @property
    def state(self) -> sm.SessionState:
        return self.machine.state

    def _stage(self, uid: UnitID) -> str | None:
        r = self._units.get(uid)
        return r.stage if r is not None else None

    def _new_machine(self) -> SessionStateMachine:
        return SessionStateMachine(thresholds=self.config.health, generation=self.gate.generation,
                                   multi_speaker=self.config.multi_speaker)

    def handle(self, inp, t_ns: int) -> list:
        self._now = t_ns
        cfg = self.config
        if cfg.provider_streaming:
            return self._provider_input(inp)
        if self.machine.state == sm.IDLE and not isinstance(inp, (Arm, SetPlaybackMode)):
            return []
        out: list = []
        if isinstance(inp, Arm):
            out.append(self._trace(TraceKind.SESSION_START, [f_str("pair", cfg.direction.trace_pair), f_int("schema", 0)]))
            out += self._session(sm.SArm())
        elif isinstance(inp, ArmSucceeded):
            out += self._session(sm.SArmSucceeded())
        elif isinstance(inp, ArmFailed):
            message = inp.message if cfg.trace_text else "preparation failed"
            out.append(self._trace(TraceKind.ERROR, [f_str("where", "arm"), f_str("message", message)]))
            out += self._session(sm.SArmFailed(message))
        elif isinstance(inp, SetPlaybackMode):
            if inp.mode != self.playback_mode:
                self.playback_mode = inp.mode
                if inp.mode is PlaybackMode.ORIGINAL:
                    for uid in self._order:
                        r = self._units.get(uid)
                        if r is not None and r.stage == Stage.PLAYING:
                            r.playback_suppressed = True
                if self.machine.state != sm.IDLE:
                    gains = self._output_gains(self.machine.state.gains)
                    out.append(self._trace(TraceKind.GAIN_RAMP, [
                        f_double("original", gains.original), f_double("translated", gains.translated),
                        f_int("ms", cfg.health.ramp_ms), f_str("playback", inp.mode.value)]))
                    out.append(RampGains(gains, cfg.health.ramp_ms))
                    out += self._background_gain_effect(cfg.health.ramp_ms)
        elif isinstance(inp, ProviderPCM):
            pass
        elif isinstance(inp, Stop):
            out += self._cancel_all(SourceDisposition.ABANDONED, "stop")
            out += self._commit_outputs(self._policy.reset())
            out += self._session(sm.SStop())
            self.gate.advance_epoch()
            self.machine = self._new_machine()
            self._profile_versions.clear()
            self._context.clear()
            self._units.clear()
            self._order.clear()
            self._anchors.clear()
            self._last_target = None
            self._speech_onset = None
            self._in_speech = False
            self._speaker_assigned = False
            self._speaker_timeline.clear()
            self._last_label = None
            self._last_pause = None
            self._backlog_seconds = 0.0
            self._worker_healthy = True
            self._next_audio_unit = 1
            self._last_audio_end = 0
        elif isinstance(inp, Captured):
            span = inp.span
            self._anchors.append((span.end, t_ns))
            if len(self._anchors) > 4096:
                del self._anchors[: len(self._anchors) - 2048]
            out.append(self._trace(TraceKind.BUF_CAPTURE, [f_span(span), f_int("frames", span.count)]))
        elif isinstance(inp, Vad):
            e = inp.event
            if isinstance(e, SpeechStart):
                self._speech_onset = e.sample
                self._in_speech = True
                self._policy.speech_started(e.sample)
                out.append(self._trace(TraceKind.VAD_START, [f_int("src_sample", e.sample)]))
                out += self._session(sm.SSpeech(self._speaker))
            else:
                self._in_speech = False
                self._last_pause = max(self._last_pause or 0, e.sample)
                out.append(self._trace(TraceKind.VAD_END, [f_int("src_sample", e.sample)]))
                out += self._commit_outputs(self._policy.speech_ended(e.sample))
        elif isinstance(inp, Speaker):
            e = inp.event
            if isinstance(e, SpeakerAssign):
                changed = self._speaker_assigned and e.speaker != self._speaker
                self._speaker = e.speaker
                self._speaker_assigned = True
                if self._last_label is None or e.span.end >= self._last_label[1]:
                    self._last_label = (e.speaker, e.span.end)
                out.append(self._trace(TraceKind.SPK_ASSIGN, [f_speaker(e.speaker), f_span(e.span)]))
                if cfg.multi_speaker:
                    self._speaker_timeline.append((e.span, e.speaker))
                    if len(self._speaker_timeline) > 512:
                        del self._speaker_timeline[: len(self._speaker_timeline) - 256]
                    if changed:
                        out.append(self._trace(TraceKind.SPK_CHANGE, [f_speaker(e.speaker), f_span(e.span)]))
                    out += self._schedule_waiting_voices()
                elif changed:
                    out.append(self._trace(TraceKind.SPK_CHANGE, [f_speaker(e.speaker), f_span(e.span)]))
                    out += self._session(sm.SSpeakerChange(e.speaker))
            else:
                out.append(self._trace(TraceKind.SPK_CHANGE, [f_span(e.span)]))
        elif isinstance(inp, Hypothesis):
            h = inp.h
            if cfg.direct_audio_translation:
                return out + self._promote_waiting()
            if cfg.emit_captions:
                out.append(Caption("source.final" if h.is_final else "source.partial", f"utt{h.utterance}", h.text))
            onset = self._speech_onset if self._speech_onset is not None else 0
            fields = [f_span(SourceSpan(onset, max(onset, h.audio_frontier))), f_int("utt", h.utterance)]
            if cfg.trace_text:
                fields.append(f_str("text", h.text))
            out.append(self._trace(TraceKind.ASR_FINAL if h.is_final else TraceKind.ASR_PARTIAL, fields))
            out += self._commit_outputs(self._policy.process(h, t_ns))
        elif isinstance(inp, AudioSegment):
            span = inp.span
            if (cfg.direct_audio_translation and not span.is_empty and span.start >= self._last_audio_end
                    and span.duration_seconds <= 30):
                self._last_audio_end = span.end
                unit = SourceUnit(id=UnitID(self._next_audio_unit), utterance=self._next_audio_unit, span=span,
                                  text="", reason=CommitReason.AUDIO_SEGMENT, timing_estimated=False,
                                  semantic_wait_ns=0)
                self._next_audio_unit += 1
                out += self._commit_outputs([Commit(unit)])
        elif isinstance(inp, FollowingSilence):
            uid = self._unit_spanning(inp.span)
            if uid is not None and math.isfinite(inp.seconds):
                self._units[uid].following_silence = max(0.0, inp.seconds)
        elif isinstance(inp, SourceLanguage):
            uid = self._unit_spanning(inp.span)
            r = self._units.get(uid) if uid is not None else None
            # Once synthesis has started the translation plays; never voice the line twice.
            if inp.matches_target and r is not None and r.stage in (
                    Stage.TRANSLATING, Stage.AWAITING_SPEAKER, Stage.AWAITING_VOICE):
                if r.stage == Stage.TRANSLATING:
                    out.append(CancelTranslation(uid))
                out += self._finish(uid, SourceDisposition.ORIGINAL_ONLY, "same_language")
                if cfg.restores_original_voice:
                    out.append(RestoreOriginalVoice(inp.span))
        elif isinstance(inp, ProfileReady):
            if not (cfg.pin_speaker_profiles and inp.speaker in self._profile_versions):
                out.append(self._trace(TraceKind.PROFILE_READY, [f_speaker(inp.speaker), f_int("prof", inp.version)]))
                out += self._session(sm.SProfileReady(inp.speaker, inp.version))
                out += self._schedule_waiting_voices()
        elif isinstance(inp, Translation):
            out += self._translation_result(inp.unit, inp.outcome)
        elif isinstance(inp, PCM):
            out += self._pcm(inp.header, inp.frames)
        elif isinstance(inp, SynthesisFinished):
            r = self._units.get(inp.unit)
            if r is not None and r.generation == inp.generation and r.stage in Stage.SYNTHESIS_STARTED:
                r.synthesis_complete = True
                if r.stage == Stage.HELD:
                    out += self._session(sm.SUnitReady(self._readiness(r, inp.unit)))
        elif isinstance(inp, SynthesisFailed):
            r = self._units.get(inp.unit)
            if r is not None and r.stage in Stage.SYNTHESIS_STARTED:
                message = inp.message if cfg.trace_text else "synthesis failed"
                out.append(self._trace(TraceKind.ERROR, [f_unit(inp.unit), f_str("where", "tts"), f_str("message", message)]))
                self.gate.cancel(inp.unit)
                out += [CancelSynthesis(inp.unit), DiscardHeld(inp.unit)]
                out += self._finish(inp.unit, SourceDisposition.ORIGINAL_ONLY, "tts_failed")
                if not r.scheduled and self._can_restore_before_deadline(r.source.span):
                    out.append(RestoreOriginalVoice(r.source.span))
                out += self._missing_unit_fallback()
        elif isinstance(inp, PlayStarted):
            r = self._units.get(inp.unit)
            if r is not None and r.stage == Stage.QUEUED:
                r.stage = Stage.PLAYING
                r.playback_suppressed = self.playback_mode is PlaybackMode.ORIGINAL
                if not r.playback_suppressed:
                    out.append(self._trace(TraceKind.PLAY_START, [f_unit(inp.unit), f_int("out_sample", inp.out_sample)]))
        elif isinstance(inp, PlayEnded):
            r = self._units.get(inp.unit)
            if r is not None and r.stage in (Stage.PLAYING, Stage.QUEUED):
                self.gate.mark_finished(inp.unit)
                if r.playback_suppressed:
                    out += self._finish(inp.unit, SourceDisposition.ORIGINAL_ONLY, "user_original_playback")
                else:
                    out.append(self._trace(TraceKind.PLAY_END, [f_unit(inp.unit), f_int("out_sample", inp.out_sample)]))
                    out += self._finish(inp.unit, SourceDisposition.TRANSLATED, "handoff" if r.handoff else "played")
        elif isinstance(inp, TranslatedBacklog):
            self._backlog_seconds = inp.seconds
        elif isinstance(inp, Underrun):
            self._underrun_pending = True
        elif isinstance(inp, WorkerHealthy):
            self._worker_healthy = inp.ok
            if not inp.ok:
                out.append(self._trace(TraceKind.ERROR, [f_str("where", "worker"), f_str("message", "service lost")]))
            out += self._session(sm.SWorkerHealthy(inp.ok))
        elif isinstance(inp, Tick):
            out += self._tick()
        elif isinstance(inp, Discontinuity):
            out.append(self._trace(TraceKind.ERROR, [f_str("where", "source"), f_str("message", f"discontinuity: {inp.reason}")]))
            out += self._commit_outputs(self._policy.reset())
            out += self._cancel_all(SourceDisposition.ABANDONED, "discontinuity")
            self.gate.advance_epoch()
            self.machine = self._new_machine()
            self._profile_versions.clear()
            self._context.clear()
            self._speaker_timeline.clear()
            self._last_label = None
            self._last_pause = None
            self._last_target = None
            self._backlog_seconds = 0.0
            self._in_speech = False
            out += [FlushTranslatedBus(), RampGains(self._output_gains(BusGains.ORIGINAL), cfg.health.ramp_ms)]
            out += self._session(sm.SArm())
            out += self._session(sm.SArmSucceeded())
            self._anchors.clear()
            self._speech_onset = None
            self._last_audio_end = 0
        else:
            raise TypeError(f"unknown pipeline input {inp!r}")
        out += self._promote_waiting()
        return out

    def _provider_input(self, inp) -> list:
        """A cloud provider emits translated PCM directly; the reducer only gates the bus."""
        cfg = self.config
        if isinstance(inp, Arm):
            if self._provider_armed:
                return []
            self._provider_armed = True
            mode = BusGains.ORIGINAL if self.playback_mode is PlaybackMode.ORIGINAL else BusGains.TRANSLATED
            return [RampGains(mode, 0)]
        if isinstance(inp, SetPlaybackMode):
            if inp.mode == self.playback_mode:
                return []
            self.playback_mode = inp.mode
            if not self._provider_armed:
                return []
            mode = BusGains.ORIGINAL if inp.mode is PlaybackMode.ORIGINAL else BusGains.TRANSLATED
            return [FlushTranslatedBus(), RampGains(mode, cfg.health.ramp_ms)]
        if isinstance(inp, ProviderPCM):
            return [EnqueueProviderPCM()] if self._provider_armed and self.playback_mode is PlaybackMode.TRANSLATED else []
        if isinstance(inp, Stop):
            if not self._provider_armed:
                return []
            self._provider_armed = False
            return [FlushTranslatedBus(), Teardown()]
        return []

    # MARK: Commit → translate

    def _commit_outputs(self, outputs: list) -> list:
        cfg = self.config
        out: list = []
        for o in outputs:
            if isinstance(o, Commit):
                unit = o.unit
                unit_speaker = self.speaker_for(unit.span) if cfg.multi_speaker else self._speaker
                fields = [f_unit(unit.id), f_speaker(unit_speaker), f_span(unit.span), f_str("reason", unit.reason),
                          f_bool("timing_estimated", unit.timing_estimated),
                          f_int("wait_ms", unit.semantic_wait_ns // 1_000_000)]
                if cfg.trace_text:
                    fields.append(f_str("text", unit.text))
                out.append(self._trace(TraceKind.COMMIT_SOURCE, fields))
                self._units[unit.id] = UnitRecord(source=unit, speaker=unit_speaker)
                self._order.append(unit.id)
                pending = [uid for uid in self._order if self._stage(uid) in Stage.LIVE]
                limit = self._max_pending_units
                if len(pending) > limit:
                    candidates = ([uid for uid in pending if self._stage(uid) not in Stage.ON_BUS]
                                  if cfg.complete_phrases_before_playback else pending)
                    for uid in candidates[: len(pending) - limit]:
                        out += self._cancel(uid, SourceDisposition.ORIGINAL_ONLY, "queue_limit")
                    if not cfg.complete_phrases_before_playback:
                        out += self._session(sm.SHealth(HealthSample(cfg.health.max_debt_seconds + 1, 0)))
                if self._stage(unit.id) != Stage.TRANSLATING:
                    continue
                request = TranslationRequest(
                    unit_id=unit.id, context=tuple(self._context[-cfg.context_pairs:] if cfg.context_pairs > 0 else ()),
                    glossary=tuple(sorted(cfg.glossary.items())), uncommitted_source=unit.text, source_span=unit.span)
                out.append(self._trace(TraceKind.MT_REQUEST, [f_unit(unit.id), f_int("context_pairs", len(request.context))]))
                out.append(Translate(request))
            elif isinstance(o, RequestFinalizeOut):
                out.append(self._trace(TraceKind.FINALIZE_REQUEST, [f_int("utt", o.utterance)]))
                out.append(RequestFinalize())
            elif isinstance(o, Hold):
                out.append(self._trace(TraceKind.COMMIT_HOLD, [
                    f_int("utt", o.utterance), f_str("reason", o.reason), f_int("pending_keys", o.pending_keys),
                    f_int("wait_ms", o.waited_ns // 1_000_000)]))
            elif isinstance(o, RevisionBehindFrontier):
                out.append(self._trace(TraceKind.COMMIT_REVISION, [f_int("utt", o.utterance), f_int("committed_keys", o.committed_keys)]))
            elif isinstance(o, Abandoned):
                fields = [f_int("utt", o.utterance), f_str("disposition", SourceDisposition.NEVER_COMMITTED)]
                if cfg.trace_text:
                    fields.append(f_str("text", o.text))
                out.append(self._trace(TraceKind.SOURCE_DISPOSITION, fields))
        return out

    def _translation_result(self, uid: UnitID, outcome) -> list:
        cfg = self.config
        r = self._units.get(uid)
        if r is None or r.stage != Stage.TRANSLATING:
            return [self._trace(TraceKind.MT_RESULT, [f_unit(uid), f_bool("ok", False), f_str("reason", "stale")])]
        if isinstance(outcome, Failure):
            message = outcome.message if cfg.trace_text else "translation failed"
            return [self._trace(TraceKind.MT_RESULT, [f_unit(uid), f_bool("ok", False), f_str("reason", "error")]),
                    self._trace(TraceKind.ERROR, [f_unit(uid), f_str("where", "mt"), f_str("message", message)])
                    ] + self._reject_translation(uid, "mt_error")
        candidate = outcome.candidate
        if candidate.unit_id != uid:
            return self._reject_translation(uid, "wrong_unit")
        verdict = cfg.validator.evaluate(
            candidate, r.source.text, self._last_target,
            source_duration_seconds=r.source.span.duration_seconds if cfg.direct_audio_translation else None,
            word_segmenter=self._target_word_segmenter,
            grace_seconds=(min(cfg.validator.spoken_budget_grace_seconds, r.following_silence + 0.2)
                           if r.following_silence is not None else None))
        if isinstance(verdict, Rejected):
            return [self._trace(TraceKind.MT_RESULT, [f_unit(uid), f_bool("ok", False), f_str("reason", verdict.rejection)])
                    ] + self._reject_translation(uid, verdict.rejection)
        if isinstance(verdict, Silent):
            # Music, laughter, or a pause the translator answered with nothing at all. The honest
            # output is silence: no error, no fallback over audio that held no speech, and the
            # unit stops holding debt the moment it is dismissed.
            return [self._trace(TraceKind.MT_RESULT, [f_unit(uid), f_bool("ok", True), f_str("silence", verdict.silence)]
                                + self._worker_fields(candidate))
                    ] + self._finish(uid, SourceDisposition.SILENCE, verdict.silence) + (
                        [RestoreOriginalVoice(r.source.span)] if cfg.restores_original_voice else [])
        text = verdict.text
        out: list = [self._trace(TraceKind.MT_RESULT, [f_unit(uid), f_bool("ok", True)] + self._worker_fields(candidate))]
        # Captions retain the complete validated translation even when speech uses a shorter prefix.
        if cfg.emit_captions:
            out.append(Caption("target.final", str(uid), trim_whitespace(candidate.text)))
        # The tracker may have labeled more of the unit's span since it was committed.
        if cfg.multi_speaker:
            r.speaker = self.speaker_for(r.source.span)
        fields = [f_unit(uid), f_speaker(r.speaker), f_span(r.source.span)]
        returned = trim_whitespace(candidate.text)
        if text != returned:
            fields.append(f_bool("shortened", True))
            fields.append(f_int("mt_chars", char_count(returned)))
        if cfg.multi_speaker and cfg.require_speaker_attribution:
            coverage = self.speaker_coverage(r.source.span)
            if (coverage[1] if coverage else 0.0) <= 0.5 and self._carried_speaker(r.source.span) is not None:
                fields.append(f_bool("speaker_carried", True))
        r.target = text
        self._last_target = text
        # Context carries what the listener is actually told, which after a deletion
        # is shorter than what the model returned.
        self._context.append(CommittedPair(r.source.text, text))
        if len(self._context) > cfg.context_pairs:
            del self._context[: len(self._context) - cfg.context_pairs]
        if not self._has_speaker_attribution(r.source.span):
            r.stage = Stage.AWAITING_SPEAKER
            # The last turn's speaker is only a guess; never report it as confirmed.
            fields = [f_unit(uid), f_span(r.source.span), f_bool("speaker_pending", True)]
            if cfg.trace_text:
                fields.append(f_str("text", text))
            out.append(self._trace(TraceKind.COMMIT_TARGET, fields))
            return out
        version = self._profile_versions.get(r.speaker)
        if not self.machine.has_profile(r.speaker) or version is None:
            # The initial reference is acquired concurrently with translation in single-
            # speaker sessions too. Keep the line within the same bounded voice wait.
            r.stage = Stage.AWAITING_VOICE
            if cfg.trace_text:
                fields.append(f_str("text", text))
            out.append(self._trace(TraceKind.COMMIT_TARGET, fields))
            return out
        fields.insert(2, f_int("prof", version))
        if cfg.trace_text:
            fields.append(f_str("text", text))
        out.append(self._trace(TraceKind.COMMIT_TARGET, fields))
        return out + self._synthesize(uid, version)

    def _synthesize(self, uid: UnitID, version: int) -> list:
        r = self._units.get(uid)
        if r is None or r.target is None:
            return []
        text = r.target
        r.stage = Stage.SYNTHESIZING
        r.generation = self.gate.generation
        # A unit whose timing is estimated has no trustworthy speech end to pace against.
        target_seconds = None
        if self.config.finish_phrase_before_catch_up and not r.source.timing_estimated:
            target_seconds = max(0.5, r.source.span.duration_seconds)
        fill = target_seconds is not None and self._allow_fill(r)
        fields = [f_unit(uid), f_int("prof", version), f_int("chars", char_count(text))]
        if fill:
            fields.append(f_bool("fill", True))
        delivery = self._allow_delivery(r)
        if delivery:
            fields.append(f_bool("delivery", True))
        return [self._trace(TraceKind.TTS_REQUEST, fields),
                Synthesize(SynthesisJob(uid, text, r.speaker, version, self.gate.epoch, self.gate.generation,
                                        r.source.span, target_seconds, allow_fill=fill,
                                        delivery_allowed=delivery))]

    def _allow_delivery(self, r: UnitRecord) -> bool:
        # Real timing and speaker attribution keep one actor's clip out of another actor's reference.
        return (self.config.clone_delivery_line and not r.source.timing_estimated
                and self._has_speaker_attribution(r.source.span))

    def _allow_fill(self, r: UnitRecord) -> bool:
        """Fill (ADR-0013): slow a short take so it ends with the actor's own last sound rather
        than leaving the tail of their turn silent. Only a phrase that plays in a slot gains
        this, and only while it is still ahead of its slot: a filled line ends at slot + source
        speech, where the actor stops and never later, so it cannot delay the next phrase."""
        return self.config.turn_fill_enabled and self._slotted and self._lateness(r.source.span.start) <= 0

    def profile_version(self, spk: SpeakerID) -> int | None:
        return self._profile_versions.get(spk)

    def _has_speaker_attribution(self, span: SourceSpan) -> bool:
        cfg = self.config
        if not (cfg.multi_speaker and cfg.require_speaker_attribution):
            return True
        coverage = self.speaker_coverage(span)
        if coverage is not None and coverage[1] > 0.5:
            return True
        return self._carried_speaker(span) is not None

    def _carried_speaker(self, span: SourceSpan) -> SpeakerID | None:
        """The speaker of the previous label, when `span` continues their turn: with no pause in
        between, or as a brief piece just after a pause. Never carried backwards: a later label
        may belong to an actor who started without a detectable pause."""
        cfg = self.config
        last = self._last_label
        if not cfg.multi_speaker or cfg.unlabeled_speaker_carry_seconds <= 0 or last is None:
            return None
        speaker, end = last
        if span.start < end:
            return None
        gap = (span.start - end) / CANONICAL_SAMPLE_RATE
        if self._last_pause is None or self._last_pause < end:
            return speaker if gap <= cfg.unlabeled_speaker_carry_seconds else None
        if span.duration_seconds > cfg.unlabeled_speaker_carry_max_piece_seconds:
            return None
        return speaker if gap <= cfg.unlabeled_speaker_carry_after_pause_seconds else None

    def _schedule_waiting_voices(self) -> list:
        """Labels and profiles can arrive in either order. Resolve again before synthesis,
        including units that were initially waiting for a different speaker's profile."""
        out: list = []
        for uid in list(self._order):
            r = self._units.get(uid)
            if r is None or r.stage not in (Stage.AWAITING_SPEAKER, Stage.AWAITING_VOICE):
                continue
            if not self._has_speaker_attribution(r.source.span):
                continue
            if self.config.multi_speaker:
                r.speaker = self.speaker_for(r.source.span)
            r.stage = Stage.AWAITING_VOICE
            version = self._profile_versions.get(r.speaker)
            if version is not None:
                out += self._synthesize(uid, version)
        return out

    def speaker_for(self, span: SourceSpan) -> SpeakerID:
        """The speaker who covers most of `span` in the tracker's labels; without overlap, the
        speaker whose turn it continues, else the latest labeled before the span ends."""
        overlaps = self._overlaps(span)
        if overlaps:
            return overlaps[0][0]
        carried = self._carried_speaker(span)
        if carried is not None:
            return carried
        for entry_span, spk in reversed(self._speaker_timeline):
            if entry_span.start < span.end:
                return spk
        return self._speaker

    def source_text(self, uid: UnitID) -> str | None:
        """Recognized source words for local delivery conditioning; never traced by this accessor."""
        r = self._units.get(uid)
        return r.source.text if r is not None else None

    def speaker_coverage(self, span: SourceSpan) -> tuple[SpeakerID, float, float] | None:
        """The speaker labeling most of `span`, their share of it, and the share labeled at all."""
        overlaps = self._overlaps(span)
        if not overlaps or span.count <= 0:
            return None
        labeled = sum(samples for _, samples in overlaps)
        best = overlaps[0]
        return best[0], best[1] / span.count, labeled / span.count

    def _overlaps(self, span: SourceSpan) -> list[tuple[SpeakerID, int]]:
        """Labeled samples of `span` per speaker, largest first; ties go to the most recent speaker."""
        overlaps: list[list] = []
        for entry_span, spk in reversed(self._speaker_timeline):
            if entry_span.end <= span.start:
                break
            o = min(entry_span.end, span.end) - max(entry_span.start, span.start)
            if o <= 0:
                continue
            for item in overlaps:
                if item[0] == spk:
                    item[1] += o
                    break
            else:
                overlaps.append([spk, o])
        ranked = sorted(enumerate(overlaps), key=lambda e: (-e[1][1], e[0]))
        return [(item[0], item[1]) for _, item in ranked]

    def _reject_translation(self, uid: UnitID, reason: str) -> list:
        r = self._units.get(uid)
        restore = [RestoreOriginalVoice(r.source.span)] if self.config.restores_original_voice and r is not None else []
        return self._finish(uid, SourceDisposition.ORIGINAL_ONLY, reason) + restore + self._missing_unit_fallback()

    def _missing_unit_fallback(self) -> list:
        """A unit that will never play. When the original bus can be heard, fall back so the
        listener hears what it covered; English-only playback skips the unit."""
        if not self.config.original_audio_enabled:
            return []
        return self._session(sm.SHealth(HealthSample(0, 0, underrun=True)))

    # MARK: PCM admission and handoff

    def _pcm(self, header: PCMChunkHeader, frames: int) -> list:
        admission = self.gate.admit(header)
        r = self._units.get(header.unit_id)
        if not admission.is_accepted or r is None or r.stage not in Stage.SYNTHESIS_STARTED:
            reason = "unknown_unit" if admission.is_accepted else admission.reason
            return [self._trace(TraceKind.PCM_REJECT, [f_unit(header.unit_id), f_int("chunk_gen", header.playout_generation),
                                                       f_str("reason", reason)]),
                    DropPCM(header, reason)]
        seconds = frames / header.format.sample_rate
        out: list = [self._trace(TraceKind.TTS_FIRST_CHUNK if r.buffered_seconds == 0 else TraceKind.TTS_CHUNK,
                                 [f_unit(header.unit_id), f_int("frames", frames), f_int("rate", header.format.sample_rate)])]
        r.buffered_seconds += seconds
        if r.first_pcm_at is None:
            r.first_pcm_at = self._now
            r.first_pcm_seconds = seconds
        r.last_pcm_at = self._now
        r.pcm_chunks += 1
        if r.stage in Stage.ON_BUS:
            out.append(EnqueuePCM(header))
            return out
        if r.stage == Stage.WAITING:
            out.append(HoldPCM(header))
            return out
        state = self.machine.state
        if state.is_translating and (state.speaker == r.speaker or self.config.multi_speaker):
            if self.config.finish_phrase_before_catch_up:
                # Every phrase needs a jitter reserve, including after an empty bus.
                r.stage = Stage.WAITING
                out.append(HoldPCM(header))
                return out
            r.stage = Stage.QUEUED
            r.scheduled = True
            out.append(EnqueuePCM(header))
            out.append(self._trace(TraceKind.PLAY_SCHEDULED, [f_unit(header.unit_id)]))
            return out
        r.stage = Stage.HELD
        out.append(HoldPCM(header))
        out += self._session(sm.SUnitReady(self._readiness(r, header.unit_id)))
        return out

    def _readiness(self, r: UnitRecord, uid: UnitID) -> UnitReadiness:
        # Not ready before its slot: a handoff would put the phrase ahead of its picture.
        if not self._is_due(r):
            return UnitReadiness(uid, r.speaker, r.generation, 0.0, False)
        return UnitReadiness(uid, r.speaker, r.generation,
                             r.buffered_seconds if self._has_sufficient_prebuffer(r) else 0.0, r.synthesis_complete)

    def _has_sufficient_prebuffer(self, r: UnitRecord) -> bool:
        cfg = self.config
        if r.synthesis_complete:
            return True
        if cfg.complete_phrases_before_playback:
            return False
        reserve = cfg.health.prebuffer_seconds
        if not cfg.adaptive_streaming_prebuffer:
            return r.buffered_seconds >= reserve
        # One chunk gives no evidence about sustained generation speed. Both native
        # whole-phrase adapters and short streams release on synthesis_finished.
        if r.pcm_chunks < 2 or r.first_pcm_at is None or r.last_pcm_at is None:
            return False
        # Include idle time since the last chunk; delayed streams may pause before their slot.
        elapsed = (max(self._now, r.last_pcm_at) - r.first_pcm_at) / 1e9
        if elapsed <= 0:
            return r.buffered_seconds >= reserve
        rate = (r.buffered_seconds - r.first_pcm_seconds) / elapsed
        if rate <= 0:
            return False
        expected = max(r.buffered_seconds, max(r.source.span.duration_seconds,
                                               (char_count(r.target) if r.target else 0) * cfg.estimated_seconds_per_target_char))
        remaining = max(0.0, expected - r.buffered_seconds)
        # Wait for a reserve covering the predicted generation deficit. A source/text duration
        # estimate is imperfect, so this reduces starvation rather than guaranteeing it.
        required = reserve + max(0.0, remaining / rate - remaining)
        return r.buffered_seconds >= required

    # MARK: State machine effects

    def _session(self, inp) -> list:
        if isinstance(inp, sm.SProfileReady):
            self._profile_versions[inp.speaker] = inp.version
        out: list = []
        for effect in self.machine.handle(inp, self._now):
            if isinstance(effect, sm.Transition):
                out.append(self._trace(TraceKind.STATE_CHANGE, [f_str("from", str(effect.frm)), f_str("to", str(effect.to)),
                                                                f_str("reason", effect.reason)]))
            elif isinstance(effect, sm.SRampGains):
                audible = self._output_gains(effect.gains)
                out.append(self._trace(TraceKind.GAIN_RAMP, [f_double("original", audible.original),
                                                             f_double("translated", audible.translated),
                                                             f_int("ms", effect.duration_ms)]))
                out.append(RampGains(audible, effect.duration_ms))
                out += self._background_gain_effect(effect.duration_ms)
            elif isinstance(effect, sm.AdvanceGeneration):
                self.gate.advance_generation()
                self.machine.sync_generation(self.gate.generation)
            elif isinstance(effect, sm.CancelPendingSynthesis):
                out.append(FlushTranslatedBus())
                out += self._cancel_all(SourceDisposition.ABANDONED, "fallback")
            elif isinstance(effect, sm.FallbackEnter):
                out.append(self._trace(TraceKind.FALLBACK_ENTER, [f_str("reason", effect.reason)]))
            elif isinstance(effect, sm.FallbackExit):
                out.append(self._trace(TraceKind.FALLBACK_EXIT, [f_unit(effect.unit)]))
            elif isinstance(effect, sm.Handoff):
                out += self._handoff(effect.unit)
            elif isinstance(effect, sm.STeardown):
                out.append(Teardown())
        return out

    def _handoff(self, uid: UnitID) -> list:
        out: list = []
        # Earlier units were already heard as original; their PCM is stale.
        for older in list(self._order):
            if not older < uid:
                continue
            if self._stage(older) in (Stage.HELD, Stage.SYNTHESIZING, Stage.TRANSLATING, Stage.AWAITING_SPEAKER,
                                      Stage.AWAITING_VOICE):
                out += self._cancel(older, SourceDisposition.ORIGINAL_ONLY, "superseded_by_handoff")
        r = self._units.get(uid)
        if r is None:
            return out
        r.stage = Stage.QUEUED
        r.scheduled = True
        r.handoff = True
        out.append(ReleaseHeld(uid))
        out.append(self._trace(TraceKind.PLAY_SCHEDULED, [f_unit(uid), f_bool("handoff", True)] + self._slot_fields(r)))
        # Picture delay hands off the newest *due* unit, so later phrases may already be held.
        # They queue behind it for their own slots instead of waiting for another handoff.
        if self._slotted:
            for newer in self._order:
                nr = self._units.get(newer)
                if newer > uid and nr is not None and nr.stage == Stage.HELD:
                    nr.stage = Stage.WAITING
        return out

    # MARK: Cancellation and disposition

    def _cancel_all(self, disposition: str, reason: str) -> list:
        out: list = []
        for uid in list(self._order):
            if self._stage(uid) in Stage.LIVE:
                out += self._cancel(uid, disposition, reason)
        return out

    def _cancel(self, uid: UnitID, disposition: str, reason: str) -> list:
        r = self._units.get(uid)
        if r is None:
            return []
        was_synth = r.stage in Stage.SYNTHESIS_STARTED
        restore = (reason not in ("stop", "discontinuity") and not r.scheduled
                   and self._can_restore_before_deadline(r.source.span))
        r.stage = Stage.CANCELED
        self.gate.cancel(uid)
        out: list = [self._trace(TraceKind.UNIT_CANCEL, [f_unit(uid), f_str("reason", reason)])]
        if was_synth:
            out += [CancelSynthesis(uid), DiscardHeld(uid)]
        else:
            out.append(CancelTranslation(uid))
        out.append(self._trace(TraceKind.SOURCE_DISPOSITION, [f_unit(uid), f_span(r.source.span),
                                                              f_str("disposition", disposition), f_str("reason", reason)]))
        if restore:
            out.append(RestoreOriginalVoice(r.source.span))
        self._prune()
        return out

    def _can_restore_before_deadline(self, span: SourceSpan) -> bool:
        return (self.config.restores_original_voice
                and self.config.original_voice_decision_deadline_seconds > 0 and bool(self._anchors)
                and self._source_age_seconds(span.start) < self.config.original_voice_decision_deadline_seconds)

    def _unit_spanning(self, span: SourceSpan) -> UnitID | None:
        return next((uid for uid in reversed(self._order)
                     if self._units.get(uid) is not None and self._units[uid].source.span == span), None)

    def _finish(self, uid: UnitID, disposition: str, reason: str) -> list:
        r = self._units.get(uid)
        if r is None:
            return []
        r.stage = Stage.FINISHED if disposition == SourceDisposition.TRANSLATED else Stage.REJECTED
        self._prune()
        return [self._trace(TraceKind.SOURCE_DISPOSITION, [f_unit(uid), f_span(r.source.span),
                                                           f_str("disposition", disposition), f_str("reason", reason)])]

    def _prune(self) -> None:
        """Drop terminal records beyond a bounded history."""
        if len(self._order) <= 256:
            return
        drop = [uid for uid in self._order[: len(self._order) - 128]
                if self._stage(uid) is None or self._stage(uid) in Stage.TERMINAL]
        for uid in drop:
            self._units.pop(uid, None)
            self.gate.forget_terminal_unit(uid)
        dropped = set(drop)
        self._order = [uid for uid in self._order if uid not in dropped]

    # MARK: Health

    def _tick(self) -> list:
        cfg = self.config
        health = cfg.health
        out: list = []
        if self.machine.state.name in ("acquiring_voice", "degraded"):
            # Held audio is not a recovery candidate once its source is stale. Fluent playout
            # keeps the newest candidate: long units are older than the lag limit by commit time.
            candidates = []
            for uid in self._order:
                r = self._units.get(uid)
                if r is not None and self.machine.has_profile(r.speaker) and r.stage in (
                        Stage.TRANSLATING, Stage.SYNTHESIZING, Stage.HELD):
                    candidates.append(uid)
            for uid in (candidates[:-1] if cfg.finish_phrase_before_catch_up else candidates):
                r = self._units.get(uid)
                if r is None or self._lateness(r.source.span.start) <= health.max_source_lag_seconds:
                    continue
                out += self._cancel(uid, SourceDisposition.ORIGINAL_ONLY, "source_expired")
        # Restore a stalled translation: past the bed deadline and past the translator's normal
        # answer time. A long unit still being answered at the deadline is restored late only if
        # it then stalls. Late answers become stale.
        if cfg.restores_original_voice and cfg.original_voice_decision_deadline_seconds > 0 and self._anchors:
            for uid in list(self._order):
                r = self._units.get(uid)
                if (r is not None and r.stage == Stage.TRANSLATING
                        and self._source_age_seconds(r.source.span.start) >= cfg.original_voice_decision_deadline_seconds
                        and self._source_age_seconds(r.source.span.end) >= cfg.translation_stall_seconds):
                    out.append(CancelTranslation(uid))
                    out += self._finish(uid, SourceDisposition.ORIGINAL_ONLY, "mt_timeout_restore")
                    out.append(RestoreOriginalVoice(r.source.span))
        # Attribution and voice acquisition are bounded by source-lag and held-bed deadlines.
        for uid in list(self._order):
            r = self._units.get(uid)
            if r is None or r.stage not in (Stage.AWAITING_VOICE, Stage.AWAITING_SPEAKER):
                continue
            # A missing voice profile will not arrive within a line; a label normally does.
            restore_deadline = (cfg.restores_original_voice and cfg.original_voice_decision_deadline_seconds > 0
                                and bool(self._anchors)
                                and self._source_age_seconds(r.source.span.start) >= cfg.original_voice_decision_deadline_seconds
                                and (r.stage == Stage.AWAITING_VOICE
                                     or self._source_age_seconds(r.source.span.end) >= cfg.speaker_label_stall_seconds))
            if not restore_deadline and self._lateness(r.source.span.end) <= health.max_source_lag_seconds:
                continue
            out += self._finish(uid, SourceDisposition.ORIGINAL_ONLY,
                                "no_speaker" if r.stage == Stage.AWAITING_SPEAKER else "no_profile")
            if restore_deadline:
                out.append(RestoreOriginalVoice(r.source.span))
        (debt, age), pending = self._queue_pressure()
        out.append(self._trace(TraceKind.QUEUE_DEPTH, [f_int("debt_ms", round_half_away(debt * 1000)),
                                                       f_int("oldest_source_age_ms", round_half_away(age * 1000)),
                                                       f_int("units_pending", pending)]))
        underrun = self._underrun_pending
        self._underrun_pending = False
        if cfg.finish_phrase_before_catch_up:
            # Soft pressure skips units nobody has heard yet; it never falls back.
            if self.machine.state.is_translating:
                out += self._catch_up(debt, age)
            debt, age = 0.0, 0.0
        out += self._session(sm.SHealth(HealthSample(debt, age, underrun=underrun, service_lost=not self._worker_healthy)))
        # Retry handoff with the freshest held unit; older backlog is never replayed.
        if self.machine.state.name in ("acquiring_voice", "degraded"):
            held = [uid for uid in self._order if self._stage(uid) == Stage.HELD]
            if held:
                r = self._units.get(held[-1])
                if r is not None:
                    out += self._session(sm.SUnitReady(self._readiness(r, held[-1])))
        return out

    def _queue_pressure(self) -> tuple[tuple[float, float], int]:
        """Translated audio the listener has yet to hear (bus backlog plus estimates for pending
        work), and the age of the oldest source not yet covered by scheduled audio."""
        pending_estimate = 0.0
        oldest: int | None = None
        pending = 0
        for uid in self._order:
            r = self._units.get(uid)
            if r is None or r.stage not in Stage.LIVE:
                continue
            pending += 1
            # Audio still early for its slot is owed to no one yet.
            if self._is_due(r):
                pending_estimate += self._unscheduled_seconds(r)
            # Scheduled audio is covered source; the mixer reports it as backlog.
            if r.stage in Stage.ON_BUS or r.stage == Stage.WAITING:
                continue
            start = r.source.span.start
            oldest = start if oldest is None else min(oldest, start)
        # Speech not yet committed is uncovered source too.
        if self._speech_onset is not None and (self._in_speech or self._policy.uncommitted_keys > 0):
            start = self._policy.uncommitted_start
            oldest = start if oldest is None else min(oldest, start)
        if oldest is None:
            age = 0.0
        else:
            age = max(0.0, self._lateness(oldest)) if self._slotted else self._source_age_seconds(oldest)
        return (self._backlog_seconds + pending_estimate, age), pending

    def _unscheduled_seconds(self, r: UnitRecord) -> float:
        """Debt a unit adds beyond the bus backlog: estimated audio still to generate, plus
        generated audio held off the bus while it waits."""
        cfg = self.config
        total = (char_count(r.target) if r.target is not None else 0) * cfg.estimated_seconds_per_target_char
        if r.stage == Stage.TRANSLATING:
            if cfg.direct_audio_translation:
                return r.source.span.duration_seconds
            return char_count(r.source.text) * cfg.estimated_seconds_per_source_char
        if r.stage in (Stage.AWAITING_SPEAKER, Stage.AWAITING_VOICE, Stage.SYNTHESIZING, Stage.HELD):
            return max(0.0, total - r.buffered_seconds)
        if r.stage == Stage.WAITING:
            return max(total, r.buffered_seconds)
        return 0.0

    def _catch_up(self, debt_now: float, age_now: float) -> list:
        """Fluent catch-up: skip unheard units, oldest first, while the listener is more than the
        debt limit behind or a unit's source ended longer ago than the lag limit."""
        health = self.config.health
        if not (debt_now > health.max_debt_seconds or age_now > health.max_source_lag_seconds):
            return []
        unheard = [uid for uid in self._order if self._stage(uid) in (
            Stage.TRANSLATING, Stage.AWAITING_SPEAKER, Stage.AWAITING_VOICE, Stage.SYNTHESIZING, Stage.WAITING)]
        debt = debt_now
        skipped: list[UnitID] = []
        # An empty bus must not continually cancel the generation about to refill it.
        on_bus = any(self._stage(uid) in Stage.ON_BUS for uid in self._order)
        refilling = None if on_bus else next((uid for uid in unheard if self._stage(uid) == Stage.SYNTHESIZING), None)
        for uid in unheard[:-1]:
            r = self._units.get(uid)
            if r is None:
                continue
            stale = self._lateness(r.source.span.end) > health.max_source_lag_seconds
            # With a picture delay a refill already past its slot would push every later phrase
            # off its picture too; the newest unit is still kept, so the bus does not stay empty.
            if uid == refilling and not (self._slotted and stale):
                continue
            if not (debt > health.max_debt_seconds or stale):
                break
            debt -= self._unscheduled_seconds(r)
            skipped.append(uid)
        if not skipped:
            return []
        out: list = [self._trace(TraceKind.CATCH_UP, [f_int("units", len(skipped)),
                                                      f_int("debt_ms", round_half_away(debt_now * 1000)),
                                                      f_int("oldest_source_age_ms", round_half_away(age_now * 1000))])]
        for uid in skipped:
            out += self._cancel(uid, SourceDisposition.ORIGINAL_ONLY, "catch_up")
        return out

    def _promote_waiting(self) -> list:
        """Fluent playout: the oldest waiting unit goes on the bus once nothing is scheduled
        ahead and, with a picture delay, once its slot has come."""
        if self._slotted:
            return self._offer_due_held() + self._promote_due()
        return self._promote_due()

    def _promote_due(self) -> list:
        if not self.config.finish_phrase_before_catch_up or not self.machine.state.is_translating:
            return []
        if any(self._stage(uid) in Stage.ON_BUS for uid in self._order):
            return []
        uid = next((u for u in self._order if self._stage(u) == Stage.WAITING), None)
        if uid is None:
            return []
        r = self._units[uid]
        if not (self._is_due(r) and self._has_sufficient_prebuffer(r)):
            return []
        r.stage = Stage.QUEUED
        r.scheduled = True
        return [ReleaseHeld(uid), self._trace(TraceKind.PLAY_SCHEDULED, [f_unit(uid)] + self._slot_fields(r))]

    def _offer_due_held(self) -> list:
        """Picture delay: a held unit reaches its slot between ticks. Offer it to the session as
        soon as it is due rather than on the next 100 ms tick."""
        if self.machine.state.name not in ("acquiring_voice", "degraded"):
            return []
        # The newest held unit whose slot has come; older backlog is never replayed.
        due = [uid for uid in self._order
               if self._stage(uid) == Stage.HELD and self._is_due(self._units[uid])]
        if not due:
            return []
        r = self._units[due[-1]]
        return self._session(sm.SUnitReady(self._readiness(r, due[-1])))

    # MARK: Picture delay (ADR-0012)

    @property
    def _slotted(self) -> bool:
        return self.config.presentation_delay_seconds > 0

    @property
    def _max_pending_units(self) -> int:
        """Units held at once: the delay keeps up to D more seconds of dialogue in flight."""
        return max(1, self.config.max_pending_units) + math.ceil(self.config.presentation_delay_seconds)

    def _lateness(self, sample: int) -> float:
        """Seconds past the slot of a source sample (negative while early). Source age when D = 0."""
        return self._source_age_seconds(sample) - self.config.presentation_delay_seconds

    def _is_due(self, r: UnitRecord) -> bool:
        return not self._slotted or self._lateness(r.source.span.start) >= 0

    def _slot_fields(self, r: UnitRecord) -> list:
        """How far from its slot a unit was scheduled; positive is late. Only with a picture delay."""
        if not self._slotted:
            return []
        return [f_int("late_ms", round_half_away(self._lateness(r.source.span.start) * 1000))]

    def _background_gain_effect(self, duration_ms: int) -> list:
        if not self.config.background_audio_enabled:
            return []
        active = (self.machine.state != sm.IDLE and self.machine.state.name != "arming"
                  and self.playback_mode is PlaybackMode.TRANSLATED)
        return [BackgroundGain(0.8 if active else 0.0, duration_ms)]

    @staticmethod
    def _worker_fields(candidate: TranslationCandidate) -> list:
        s = candidate.worker_stats
        if s is None:
            return []
        return [f_int("worker_ms", s.after_close_ms), f_int("worker_turns", s.turns),
                f_bool("worker_st", s.sentence_fallback)]

    def _source_age_seconds(self, sample: int) -> float:
        """Age of a source sample, mapped to event time through capture anchors."""
        anchors = self._anchors
        lo, hi = 0, len(anchors)
        while lo < hi:
            mid = (lo + hi) // 2
            if anchors[mid][0] < sample:
                lo = mid + 1
            else:
                hi = mid
        if not anchors:
            return 0.0
        a_sample, a_ns = anchors[lo] if lo < len(anchors) else anchors[-1]
        sample_ns = float(a_ns) - float(a_sample - sample) * 1e9 / CANONICAL_SAMPLE_RATE
        return max(0.0, (float(self._now) - sample_ns) / 1e9)

    def _trace(self, kind: TraceKind, fields: list) -> TraceOut:
        cfg = self.config
        return TraceOut(TraceEvent(self._now, cfg.session, self.gate.epoch, self.gate.generation, kind, tuple(fields)))

    def _output_gains(self, gains: BusGains) -> BusGains:
        if self.playback_mode is PlaybackMode.ORIGINAL:
            return BusGains.ORIGINAL
        original = gains.original if self.config.original_audio_enabled else 0.0
        return BusGains(original, gains.translated)
