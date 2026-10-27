"""Session state machine. Port of apple/Sources/A2ACore/SessionStateMachine.swift.

Idle ─arm→ Arming ─ok→ Listening ─speech→ AcquiringVoice(spk)
AcquiringVoice ─profile ready + fresh unit PCM buffered + safe handoff→ Translating(spk)
Translating ─debt / source lag / underrun / service lost→ Degraded
Degraded ─healthy worker + stable interval + fresh unit PCM + safe boundary→ Translating
Translating ─speaker change→ AcquiringVoice(new)
any ─stop / error→ Idle

Multi-speaker sessions (ADR-0005) never report speaker changes: each unit carries its
speaker and waits for that speaker's profile, so dialogue turns do not fall back.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import SpeakerID, UnitID, wrapping_sub_u64


@dataclass(frozen=True, slots=True)
class SessionState:
    name: str
    speaker: SpeakerID | None = None

    def __str__(self) -> str:
        if self.speaker is not None:
            return f"{self.name}({self.speaker.raw_value})"
        return self.name

    @property
    def is_translating(self) -> bool:
        return self.name == "translating"

    @property
    def gains(self) -> BusGains:
        """Bus gains the mixer should hold in this state."""
        return BusGains.TRANSLATED if self.name == "translating" else BusGains.ORIGINAL


IDLE = SessionState("idle")
ARMING = SessionState("arming")
LISTENING = SessionState("listening")
DEGRADED = SessionState("degraded")


def acquiring_voice(spk: SpeakerID) -> SessionState:
    return SessionState("acquiring_voice", spk)


def translating(spk: SpeakerID) -> SessionState:
    return SessionState("translating", spk)


@dataclass(frozen=True, slots=True)
class BusGains:
    original: float
    translated: float


BusGains.ORIGINAL = BusGains(1.0, 0.0)
BusGains.TRANSLATED = BusGains(0.0, 1.0)


@dataclass
class HealthThresholds:
    max_debt_seconds: float = 3.0
    max_source_lag_seconds: float = 4.0
    # Healthy interval required in Degraded before retrying translation.
    stable_retry_ns: int = 2_000_000_000
    # Playable PCM needed before the original bus can be muted (80–120 ms).
    prebuffer_seconds: float = 0.08
    ramp_ms: int = 30


@dataclass(frozen=True, slots=True)
class HealthSample:
    debt_seconds: float
    source_lag_seconds: float
    underrun: bool = False
    service_lost: bool = False


@dataclass(frozen=True, slots=True)
class UnitReadiness:
    unit: UnitID
    speaker: SpeakerID
    generation: int
    buffered_seconds: float
    # Synthesis finished, so a short unit is ready even below the prebuffer.
    complete: bool
    # The unit starts at a closed-unit boundary (always true for v0 closed units).
    at_safe_boundary: bool = True


# MARK: Inputs


@dataclass(frozen=True, slots=True)
class SArm:
    pass


@dataclass(frozen=True, slots=True)
class SArmSucceeded:
    pass


@dataclass(frozen=True, slots=True)
class SArmFailed:
    message: str


@dataclass(frozen=True, slots=True)
class SSpeech:
    speaker: SpeakerID


@dataclass(frozen=True, slots=True)
class SProfileReady:
    speaker: SpeakerID
    version: int


@dataclass(frozen=True, slots=True)
class SUnitReady:
    readiness: UnitReadiness


@dataclass(frozen=True, slots=True)
class SHealth:
    sample: HealthSample


@dataclass(frozen=True, slots=True)
class SWorkerHealthy:
    ok: bool


@dataclass(frozen=True, slots=True)
class SSpeakerChange:
    speaker: SpeakerID


@dataclass(frozen=True, slots=True)
class SStop:
    pass


@dataclass(frozen=True, slots=True)
class SError:
    message: str


# MARK: Effects


@dataclass(frozen=True, slots=True)
class Transition:
    frm: SessionState
    to: SessionState
    reason: str


@dataclass(frozen=True, slots=True)
class SRampGains:
    gains: BusGains
    duration_ms: int


@dataclass(frozen=True, slots=True)
class AdvanceGeneration:
    """Fallback or speaker handoff: invalidate pending playout."""


@dataclass(frozen=True, slots=True)
class CancelPendingSynthesis:
    pass


@dataclass(frozen=True, slots=True)
class FallbackEnter:
    reason: str


@dataclass(frozen=True, slots=True)
class FallbackExit:
    unit: UnitID


@dataclass(frozen=True, slots=True)
class Handoff:
    """Start translated playback with this unit's buffered PCM."""

    unit: UnitID


@dataclass(frozen=True, slots=True)
class STeardown:
    pass


@dataclass
class SessionStateMachine:
    thresholds: HealthThresholds = field(default_factory=HealthThresholds)
    generation: int = 1
    multi_speaker: bool = False
    state: SessionState = IDLE
    _profiles: dict = field(default_factory=dict)
    _worker_healthy: bool = True
    _healthy_since: int | None = None

    def has_profile(self, speaker: SpeakerID) -> bool:
        return speaker in self._profiles

    def sync_generation(self, g: int) -> None:
        """Keep the machine's view of the playout generation aligned with the host gate."""
        self.generation = g

    def handle(self, inp, t_ns: int) -> list:
        state = self.state
        ramp = self.thresholds.ramp_ms
        if isinstance(inp, SStop):
            if state == IDLE:
                return []
            return self._move(IDLE, "stop") + [SRampGains(BusGains.ORIGINAL, ramp), STeardown()]
        if isinstance(inp, SError):
            if state == IDLE:
                return []
            return self._move(IDLE, f"error: {inp.message}") + [SRampGains(BusGains.ORIGINAL, ramp), STeardown()]

        if state == IDLE and isinstance(inp, SArm):
            return self._move(ARMING, "arm")
        if state == ARMING and isinstance(inp, SArmSucceeded):
            return self._move(LISTENING, "armed") + [SRampGains(BusGains.ORIGINAL, 0)]
        if state == ARMING and isinstance(inp, SArmFailed):
            return self._move(IDLE, f"arm failed: {inp.message}") + [STeardown()]

        if isinstance(inp, SProfileReady):
            self._profiles[inp.speaker] = inp.version
            return []

        if state == LISTENING and isinstance(inp, SSpeech):
            return self._move(acquiring_voice(inp.speaker), "speech")

        if state.name == "acquiring_voice" and isinstance(inp, SUnitReady):
            r = inp.readiness
            if not ((r.speaker == state.speaker or self.multi_speaker) and self._handoff_allowed(r)):
                return []
            return self._move(translating(r.speaker), f"handoff {r.unit}") + [
                Handoff(r.unit), SRampGains(BusGains.TRANSLATED, ramp)]

        if state.name == "acquiring_voice" and isinstance(inp, SSpeakerChange) and inp.speaker != state.speaker:
            return self._move(acquiring_voice(inp.speaker), "speaker change")
        if state.name == "translating" and isinstance(inp, SSpeakerChange) and inp.speaker != state.speaker:
            return self._move(acquiring_voice(inp.speaker), "speaker change") + [
                SRampGains(BusGains.ORIGINAL, ramp), AdvanceGeneration(), CancelPendingSynthesis()]

        if state.name == "translating" and isinstance(inp, SHealth):
            reason = self._unhealthy_reason(inp.sample)
            if reason is None:
                return []
            return self._enter_degraded(reason)
        if state.name == "translating" and isinstance(inp, SWorkerHealthy) and not inp.ok:
            self._worker_healthy = False
            return self._enter_degraded("service_lost")

        if state == DEGRADED and isinstance(inp, SHealth):
            if self._unhealthy_reason(inp.sample) is None and self._worker_healthy:
                if self._healthy_since is None:
                    self._healthy_since = t_ns
            else:
                self._healthy_since = None
            return []
        if state == DEGRADED and isinstance(inp, SUnitReady):
            r = inp.readiness
            since = self._healthy_since
            if (not self._worker_healthy or since is None
                    or wrapping_sub_u64(t_ns, since) < self.thresholds.stable_retry_ns
                    or not self._handoff_allowed(r)):
                return []
            return self._move(translating(r.speaker), f"recovered {r.unit}") + [
                Handoff(r.unit), FallbackExit(r.unit), SRampGains(BusGains.TRANSLATED, ramp)]

        if isinstance(inp, SWorkerHealthy):
            self._worker_healthy = inp.ok
            if not inp.ok:
                self._healthy_since = None
            return []
        return []

    def _handoff_allowed(self, r: UnitReadiness) -> bool:
        return (self.has_profile(r.speaker) and r.generation == self.generation and r.at_safe_boundary
                and (r.buffered_seconds >= self.thresholds.prebuffer_seconds or r.complete))

    def _unhealthy_reason(self, h: HealthSample) -> str | None:
        if h.service_lost:
            return "service_lost"
        if h.underrun:
            return "underrun"
        if h.debt_seconds > self.thresholds.max_debt_seconds:
            return "debt"
        if h.source_lag_seconds > self.thresholds.max_source_lag_seconds:
            return "source_lag"
        return None

    def _enter_degraded(self, reason: str) -> list:
        self._healthy_since = None
        ramp = self.thresholds.ramp_ms
        return self._move(DEGRADED, reason) + [
            SRampGains(BusGains.ORIGINAL, ramp), AdvanceGeneration(), CancelPendingSynthesis(), FallbackEnter(reason)]

    def _move(self, nxt: SessionState, reason: str) -> list:
        frm = self.state
        self.state = nxt
        if nxt == IDLE:
            self._healthy_since = None
            self._worker_healthy = True
        return [Transition(frm, nxt, reason)]
