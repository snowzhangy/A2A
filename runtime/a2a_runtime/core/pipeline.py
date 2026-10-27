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
from .text import char_count, trim_whitespace
from .trace import TraceEvent, TraceKind, f_bool, f_double, f_int, f_span, f_speaker, f_str, f_unit
from .types import (CANONICAL_SAMPLE_RATE, ASRHypothesis, CommittedPair, PCMChunkHeader, SourceSpan, SpeakerAssign,
                    SpeakerChange, SpeakerID, SpeechEnd, SpeechStart, TranslationCandidate, TranslationRequest, UnitID)
from .validator import TranslationValidator


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
    # Dialogue (ADR-0005): each unit takes the speaker who covers most of its span.
    multi_speaker: bool = False

    def __post_init__(self) -> None:
        self.validator = TranslationValidator(target_language=self.direction.target)
        if self.direction.source is TranslationLanguage.ENGLISH:
            self.commit.prefer_word_boundaries = True
            self.estimated_seconds_per_source_char = 1.0 / 15.0
            self.estimated_seconds_per_target_char = 0.25


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
class RampGains:
    gains: BusGains
    duration_ms: int


@dataclass(frozen=True, slots=True)
class FlushTranslatedBus:
    pass


@dataclass(frozen=True, slots=True)
class Teardown:
    pass


class SourceDisposition:
    TRANSLATED = "translated"
    ORIGINAL_ONLY = "original_only"
    ABANDONED = "abandoned"
    NEVER_COMMITTED = "never_committed"


class Stage:
    TRANSLATING = "translating"
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
    LIVE = frozenset([TRANSLATING, AWAITING_VOICE]) | SYNTHESIS_STARTED
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
    synthesis_complete: bool = False
    scheduled: bool = False
    handoff: bool = False
    playback_suppressed: bool = False


class SessionPipeline:
    def __init__(self, config: PipelineConfig) -> None:
        self.config = config
        self.gate = PlayoutGate()
        self.machine = SessionStateMachine(thresholds=config.health, generation=1, multi_speaker=config.multi_speaker)
        self.playback_mode = config.playback_mode
        self._policy = CommitPolicy(config.commit)
        self._units: dict[UnitID, UnitRecord] = {}
        self._order: list[UnitID] = []
        self._context: list[CommittedPair] = []
        self._last_target: str | None = None
        self._speaker = config.default_speaker
        self._speaker_assigned = False
        # Multi-speaker: tracker labels in source order, for attributing units by span.
        self._speaker_timeline: list[tuple[SourceSpan, SpeakerID]] = []
        self._backlog_seconds = 0.0
        self._underrun_pending = False
        self._worker_healthy = True
        self._speech_onset: int | None = None
        self._in_speech = False
        self._anchors: list[tuple[int, int]] = []
        self._now = 0
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
            self._backlog_seconds = 0.0
            self._worker_healthy = True
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
                out.append(self._trace(TraceKind.VAD_END, [f_int("src_sample", e.sample)]))
                out += self._commit_outputs(self._policy.speech_ended(e.sample))
        elif isinstance(inp, Speaker):
            e = inp.event
            if isinstance(e, SpeakerAssign):
                changed = self._speaker_assigned and e.speaker != self._speaker
                self._speaker = e.speaker
                self._speaker_assigned = True
                out.append(self._trace(TraceKind.SPK_ASSIGN, [f_speaker(e.speaker), f_span(e.span)]))
                if cfg.multi_speaker:
                    self._speaker_timeline.append((e.span, e.speaker))
                    if len(self._speaker_timeline) > 512:
                        del self._speaker_timeline[: len(self._speaker_timeline) - 256]
                    if changed:
                        out.append(self._trace(TraceKind.SPK_CHANGE, [f_speaker(e.speaker), f_span(e.span)]))
                elif changed:
                    out.append(self._trace(TraceKind.SPK_CHANGE, [f_speaker(e.speaker), f_span(e.span)]))
                    out += self._session(sm.SSpeakerChange(e.speaker))
            else:
                out.append(self._trace(TraceKind.SPK_CHANGE, [f_span(e.span)]))
        elif isinstance(inp, Hypothesis):
            h = inp.h
            if cfg.emit_captions:
                out.append(Caption("source.final" if h.is_final else "source.partial", f"utt{h.utterance}", h.text))
            onset = self._speech_onset if self._speech_onset is not None else 0
            fields = [f_span(SourceSpan(onset, max(onset, h.audio_frontier))), f_int("utt", h.utterance)]
            if cfg.trace_text:
                fields.append(f_str("text", h.text))
            out.append(self._trace(TraceKind.ASR_FINAL if h.is_final else TraceKind.ASR_PARTIAL, fields))
            out += self._commit_outputs(self._policy.process(h, t_ns))
        elif isinstance(inp, ProfileReady):
            out.append(self._trace(TraceKind.PROFILE_READY, [f_speaker(inp.speaker), f_int("prof", inp.version)]))
            out += self._session(sm.SProfileReady(inp.speaker, inp.version))
            for uid in list(self._order):
                r = self._units.get(uid)
                if r is not None and r.stage == Stage.AWAITING_VOICE and r.speaker == inp.speaker:
                    out += self._synthesize(uid, inp.version)
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
            self._last_target = None
            self._backlog_seconds = 0.0
            self._in_speech = False
            out += [FlushTranslatedBus(), RampGains(self._output_gains(BusGains.ORIGINAL), cfg.health.ramp_ms)]
            out += self._session(sm.SArm())
            out += self._session(sm.SArmSucceeded())
            self._anchors.clear()
            self._speech_onset = None
        else:
            raise TypeError(f"unknown pipeline input {inp!r}")
        out += self._promote_waiting()
        return out

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
                limit = max(1, cfg.max_pending_units)
                if len(pending) > limit:
                    for uid in pending[: len(pending) - limit]:
                        out += self._cancel(uid, SourceDisposition.ORIGINAL_ONLY, "queue_limit")
                    out += self._session(sm.SHealth(HealthSample(cfg.health.max_debt_seconds + 1, 0)))
                if self._stage(unit.id) != Stage.TRANSLATING:
                    continue
                request = TranslationRequest(
                    unit_id=unit.id, context=tuple(self._context[-cfg.context_pairs:] if cfg.context_pairs > 0 else ()),
                    glossary=tuple(sorted(cfg.glossary.items())), uncommitted_source=unit.text)
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
        rejection = cfg.validator.validate(candidate, r.source.text, self._last_target)
        if rejection is not None:
            return [self._trace(TraceKind.MT_RESULT, [f_unit(uid), f_bool("ok", False), f_str("reason", rejection)])
                    ] + self._reject_translation(uid, rejection)
        text = trim_whitespace(candidate.text)
        out: list = [self._trace(TraceKind.MT_RESULT, [f_unit(uid), f_bool("ok", True)])]
        if cfg.emit_captions:
            out.append(Caption("target.final", str(uid), text))
        # The tracker may have labeled more of the unit's span since it was committed.
        if cfg.multi_speaker:
            r.speaker = self.speaker_for(r.source.span)
        fields = [f_unit(uid), f_speaker(r.speaker), f_span(r.source.span)]
        r.target = text
        self._last_target = text
        self._context.append(CommittedPair(r.source.text, text))
        if len(self._context) > cfg.context_pairs:
            del self._context[: len(self._context) - cfg.context_pairs]
        version = self._profile_versions.get(r.speaker)
        if not self.machine.has_profile(r.speaker) or version is None:
            if cfg.multi_speaker:
                r.stage = Stage.AWAITING_VOICE
            if cfg.trace_text:
                fields.append(f_str("text", text))
            out.append(self._trace(TraceKind.COMMIT_TARGET, fields))
            return out if cfg.multi_speaker else out + self._finish(uid, SourceDisposition.ORIGINAL_ONLY, "no_profile")
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
        target_seconds = None
        if self.config.finish_phrase_before_catch_up and not r.source.timing_estimated:
            target_seconds = max(0.5, r.source.span.duration_seconds)
        return [self._trace(TraceKind.TTS_REQUEST, [f_unit(uid), f_int("prof", version), f_int("chars", char_count(text))]),
                Synthesize(SynthesisJob(uid, text, r.speaker, version, self.gate.epoch, self.gate.generation,
                                        r.source.span, target_seconds))]

    def profile_version(self, spk: SpeakerID) -> int | None:
        return self._profile_versions.get(spk)

    def speaker_for(self, span: SourceSpan) -> SpeakerID:
        """The speaker who covers most of `span` in the tracker's labels; without overlap, the
        latest speaker labeled before the span ends (multi-speaker sessions)."""
        overlaps = self._overlaps(span)
        if overlaps:
            return overlaps[0][0]
        for entry_span, spk in reversed(self._speaker_timeline):
            if entry_span.start < span.end:
                return spk
        return self._speaker

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
        return self._finish(uid, SourceDisposition.ORIGINAL_ONLY, reason) + self._missing_unit_fallback()

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

    @staticmethod
    def _readiness(r: UnitRecord, uid: UnitID) -> UnitReadiness:
        return UnitReadiness(uid, r.speaker, r.generation, r.buffered_seconds, r.synthesis_complete)

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
            if self._stage(older) in (Stage.HELD, Stage.SYNTHESIZING, Stage.TRANSLATING, Stage.AWAITING_VOICE):
                out += self._cancel(older, SourceDisposition.ORIGINAL_ONLY, "superseded_by_handoff")
        r = self._units.get(uid)
        if r is None:
            return out
        r.stage = Stage.QUEUED
        r.scheduled = True
        r.handoff = True
        out.append(ReleaseHeld(uid))
        out.append(self._trace(TraceKind.PLAY_SCHEDULED, [f_unit(uid), f_bool("handoff", True)]))
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
        r.stage = Stage.CANCELED
        self.gate.cancel(uid)
        out: list = [self._trace(TraceKind.UNIT_CANCEL, [f_unit(uid), f_str("reason", reason)])]
        if was_synth:
            out += [CancelSynthesis(uid), DiscardHeld(uid)]
        else:
            out.append(CancelTranslation(uid))
        out.append(self._trace(TraceKind.SOURCE_DISPOSITION, [f_unit(uid), f_span(r.source.span),
                                                              f_str("disposition", disposition), f_str("reason", reason)]))
        self._prune()
        return out

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
                if r is None or self._source_age_seconds(r.source.span.start) <= health.max_source_lag_seconds:
                    continue
                out += self._cancel(uid, SourceDisposition.ORIGINAL_ONLY, "source_expired")
        # A unit waits for its speaker's profile only while its source is fresh.
        for uid in list(self._order):
            r = self._units.get(uid)
            if (r is None or r.stage != Stage.AWAITING_VOICE
                    or self._source_age_seconds(r.source.span.end) <= health.max_source_lag_seconds):
                continue
            out += self._finish(uid, SourceDisposition.ORIGINAL_ONLY, "no_profile")
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
        age = self._source_age_seconds(oldest) if oldest is not None else 0.0
        return (self._backlog_seconds + pending_estimate, age), pending

    def _unscheduled_seconds(self, r: UnitRecord) -> float:
        """Debt a unit adds beyond the bus backlog: estimated audio still to generate, plus
        generated audio held off the bus while it waits."""
        cfg = self.config
        total = (char_count(r.target) if r.target is not None else 0) * cfg.estimated_seconds_per_target_char
        if r.stage == Stage.TRANSLATING:
            return char_count(r.source.text) * cfg.estimated_seconds_per_source_char
        if r.stage in (Stage.AWAITING_VOICE, Stage.SYNTHESIZING, Stage.HELD):
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
            Stage.TRANSLATING, Stage.AWAITING_VOICE, Stage.SYNTHESIZING, Stage.WAITING)]
        debt = debt_now
        skipped: list[UnitID] = []
        # An empty bus must not continually cancel the generation about to refill it.
        on_bus = any(self._stage(uid) in Stage.ON_BUS for uid in self._order)
        refilling = None if on_bus else next((uid for uid in unheard if self._stage(uid) == Stage.SYNTHESIZING), None)
        for uid in unheard[:-1]:
            if uid == refilling:
                continue
            r = self._units.get(uid)
            if r is None:
                continue
            stale = self._source_age_seconds(r.source.span.end) > health.max_source_lag_seconds
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
        """Fluent playout: the oldest waiting unit goes on the bus once nothing is scheduled ahead."""
        if not self.config.finish_phrase_before_catch_up or not self.machine.state.is_translating:
            return []
        if any(self._stage(uid) in Stage.ON_BUS for uid in self._order):
            return []
        uid = next((u for u in self._order if self._stage(u) == Stage.WAITING), None)
        if uid is None:
            return []
        r = self._units[uid]
        if not (r.synthesis_complete or r.buffered_seconds >= self.config.health.prebuffer_seconds):
            return []
        r.stage = Stage.QUEUED
        r.scheduled = True
        return [ReleaseHeld(uid), self._trace(TraceKind.PLAY_SCHEDULED, [f_unit(uid)])]

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
