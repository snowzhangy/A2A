"""Ports of PictureDelayTests.swift (ADR-0012) and TurnFillTests.swift (ADR-0013).

Picture delay D: the host shows the source picture D seconds late, so each unit plays in its
own slot (source start + D), lag limits count from that slot, and early audio is not debt.
Turn fill: with a picture delay, a phrase still ahead of its slot may be slowed so that it ends
where the actor stops; a late phrase, a phrase with no slot, and estimated timing never are.
"""

from __future__ import annotations

from a2a_runtime.core import pipeline as p
from a2a_runtime.core.pipeline import round_half_away
from a2a_runtime.core.state_machine import translating
from a2a_runtime.core.trace import TraceKind
from a2a_runtime.core.types import (ASRHypothesis, ASRToken, PCMChunkHeader, PCMFormat, SourceSpan, SpeakerID,
                                    SpeechEnd, SpeechStart, TranslationCandidate, UnitID)

SPEAKER = SpeakerID("S1")
RATE = 48_000.0


def ns(seconds: float) -> int:
    return round_half_away(seconds * 1e9)


class DelayedSession:
    """Live-like timeline: capture advances with event time, as in the fluent playout tests."""

    def __init__(self, delay: float, max_debt: float = 3, max_lag: float = 4, max_pending: int = 8,
                 adaptive: bool = False) -> None:
        config = p.PipelineConfig(session="picture-delay")
        config.finish_phrase_before_catch_up = True
        config.original_audio_enabled = False
        config.health.max_debt_seconds = max_debt
        config.health.max_source_lag_seconds = max_lag
        config.max_pending_units = max_pending
        config.presentation_delay_seconds = delay
        config.adaptive_streaming_prebuffer = adaptive
        if adaptive:
            config.health.prebuffer_seconds = 0.24
        self.p = p.SessionPipeline(config)
        self.effects: list = []
        self._captured = 0
        self.run(0, p.Arm())
        self.run(0, p.ArmSucceeded())
        self.run(0, p.ProfileReady(SPEAKER, 1))
        self.run(0, p.Vad(SpeechStart(0)))

    def run(self, t: float, inp) -> list:
        frontier = int(t * RATE)
        if frontier > self._captured:
            self.effects += self.p.handle(p.Captured(SourceSpan(self._captured, frontier)), ns(t))
            self._captured = frontier
        out = self.p.handle(inp, ns(t))
        self.effects += out
        return out

    def commit(self, uid: int, start: float, end: float, t: float) -> None:
        s = SourceSpan(int(start * RATE), int(end * RATE))
        self.run(t, p.Hypothesis(ASRHypothesis(uid, (ASRToken(f"测试第{uid}句。", s),), True, s.end)))
        self.run(t + 0.1, p.Translation(UnitID(uid), p.Success(TranslationCandidate(UnitID(uid), f"Sentence {uid}."))))

    def synthesize(self, uid: int, seconds: float, t: float) -> None:
        """One complete synthesized phrase of `seconds`."""
        header = PCMChunkHeader(UnitID(uid), SPEAKER, 1, 1, self.p.gate.generation, PCMFormat(24_000, 1),
                                SourceSpan(0, 0), False)
        self.run(t, p.PCM(header, int(seconds * 24_000)))
        self.run(t, p.SynthesisFinished(UnitID(uid), self.p.gate.generation))

    def tick(self, t: float, backlog: float = 0) -> list:
        return self.run(t, p.TranslatedBacklog(backlog)) + self.run(t, p.Tick())

    def release_time(self, uid: int, start: float, end: float) -> float | None:
        """Event time at which `uid` was released to the bus, scanning 20 ms driver steps."""
        t = start
        while t <= end:
            # Capture advancing to `t` is itself an input, and can release the unit.
            before = len(self.effects)
            self.tick(t)
            if p.ReleaseHeld(UnitID(uid)) in self.effects[before:]:
                return t
            t += 0.02
        return None

    def traces(self, kind: TraceKind):
        return [e.event for e in self.effects if isinstance(e, p.TraceOut) and e.event.kind == kind]

    def canceled(self, reason: str) -> list[str]:
        return [e.value("unit") for e in self.traces(TraceKind.UNIT_CANCEL) if e.value("reason") == reason]

    def late_ms(self, uid: int) -> int | None:
        for e in self.traces(TraceKind.PLAY_SCHEDULED):
            if e.value("unit") == f"u{uid}" and e.value("late_ms") is not None:
                return e.value("late_ms")
        return None


# MARK: Picture delay


def test_the_first_phrase_waits_for_its_slot_although_it_was_ready_early():
    s = DelayedSession(delay=8)
    s.commit(1, 0, 2, 2.1)
    s.synthesize(1, 2, 3.0)
    # Ready 5 s before its slot: a live session would start now, ahead of the picture.
    assert p.ReleaseHeld(UnitID(1)) not in s.effects
    released = s.release_time(1, 3.02, 9)
    assert released is not None and abs(released - 8.0) <= 0.021
    assert s.p.state == translating(SPEAKER)
    assert 0 <= (s.late_ms(1) if s.late_ms(1) is not None else 999) <= 20


def test_stalled_chunks_are_not_mistaken_for_a_healthy_stream_at_the_slot():
    session = DelayedSession(delay=8, adaptive=True)
    session.commit(1, 0, 3, 3.1)
    header = PCMChunkHeader(UnitID(1), SPEAKER, 1, 1, 1, PCMFormat(24_000, 1), SourceSpan(0, 0), False)
    session.run(4.0, p.PCM(header, 7_200))
    session.run(4.1, p.PCM(header, 7_200))
    assert p.ReleaseHeld(UnitID(1)) not in session.tick(8.0)
    assert p.ReleaseHeld(UnitID(1)) in session.run(8.1, p.SynthesisFinished(UnitID(1), 1))


def test_a_later_phrase_waits_for_its_slot_even_when_the_bus_is_free():
    s = DelayedSession(delay=8)
    s.commit(1, 0, 1, 1.1)
    s.synthesize(1, 1, 1.5)
    s.commit(2, 3, 4, 4.1)
    s.synthesize(2, 1, 4.5)
    assert s.release_time(1, 1.52, 8.5) is not None
    s.run(8.05, p.PlayStarted(UnitID(1), 0))
    s.run(9.05, p.PlayEnded(UnitID(1), 48_000))
    # The bus is empty from 9.05 s, but unit 2's source started at 3 s: slot 11 s.
    released = s.release_time(2, 9.06, 12)
    assert released is not None and abs(released - 11.0) <= 0.021


def test_audio_waiting_for_its_slot_is_not_debt_and_nothing_expires_early():
    # Live limits would skip all of this: 6 s of ready speech is twice the debt limit, and every
    # source is older than the 4 s lag limit long before its 10 s slot.
    s = DelayedSession(delay=10, max_debt=3, max_lag=4)
    s.commit(1, 0, 2, 2.1)
    s.synthesize(1, 2, 2.5)
    s.commit(2, 2, 4, 4.1)
    s.synthesize(2, 2, 4.5)
    s.commit(3, 4, 6, 6.1)
    s.synthesize(3, 2, 6.5)
    t = 6.6
    while t < 9.9:
        s.tick(t)
        t += 0.1
    assert s.canceled("catch_up") == []
    assert s.canceled("source_expired") == []
    assert s.canceled("queue_limit") == []
    assert s.release_time(1, 9.9, 10.1) is not None


def test_a_phrase_missing_its_slot_by_more_than_the_lag_limit_is_skipped():
    s = DelayedSession(delay=8, max_lag=1)
    s.commit(1, 0, 1, 1.1)
    s.synthesize(1, 1, 1.5)
    assert s.release_time(1, 1.52, 8.5) is not None
    s.run(8.05, p.PlayStarted(UnitID(1), 0))
    s.run(9.05, p.PlayEnded(UnitID(1), 48_000))
    # Unit 2 never gets audio; unit 3 behind it keeps the newest slot alive.
    s.commit(2, 1, 2, 2.1)
    s.commit(3, 2, 3, 3.1)
    t = 9.1
    while t < 10.9:
        s.tick(t)
        t += 0.1
    # Slot of unit 2's end is 10 s; 1 s of lateness is tolerated.
    assert s.canceled("catch_up") == []
    while t < 11.3:
        s.tick(t)
        t += 0.1
    assert s.canceled("catch_up") == ["u2"]


def test_the_queue_holds_the_delayed_seconds_of_dialogue():
    s = DelayedSession(delay=8, max_pending=2)
    for uid in range(1, 7):
        s.commit(uid, uid - 1, uid, uid + 0.1)
    assert s.canceled("queue_limit") == []


def test_no_picture_delay_keeps_live_playback():
    s = DelayedSession(delay=0)
    s.commit(1, 0, 2, 2.1)
    s.synthesize(1, 2, 3.0)
    assert p.ReleaseHeld(UnitID(1)) in s.effects
    assert s.late_ms(1) is None


# MARK: Turn fill


class FillSession:
    def __init__(self, delay: float, fill_enabled: bool = True) -> None:
        config = p.PipelineConfig(session="turn-fill")
        config.finish_phrase_before_catch_up = True
        config.original_audio_enabled = False
        config.health.max_debt_seconds = 30
        config.health.max_source_lag_seconds = 30
        config.presentation_delay_seconds = delay
        config.turn_fill_enabled = fill_enabled
        self.p = p.SessionPipeline(config)
        self.effects: list = []
        self._captured = 0
        self.run(0, p.Arm())
        self.run(0, p.ArmSucceeded())
        self.run(0, p.ProfileReady(SPEAKER, 1))

    def run(self, t: float, inp) -> list:
        frontier = int(t * RATE)
        if frontier > self._captured:
            self.effects += self.p.handle(p.Captured(SourceSpan(self._captured, frontier)), int(t * 1e9))
            self._captured = frontier
        out = self.p.handle(inp, int(t * 1e9))
        self.effects += out
        return out

    def speak(self, uid: int, start: float, end: float, commit_at: float, timed: bool = True) -> p.SynthesisJob | None:
        """Speech from `start` to `end`, recognized at `commit_at` and answered 100 ms later.
        `timed` False stands for a recognizer without token timing (spans only estimated)."""
        span = SourceSpan(int(start * RATE), int(end * RATE))
        self.run(commit_at, p.Vad(SpeechStart(span.start)))
        self.run(commit_at, p.Vad(SpeechEnd(span.end)))
        self.run(commit_at, p.Hypothesis(ASRHypothesis(uid, (ASRToken(f"测试第{uid}句。", span if timed else None),),
                                                      True, span.end)))
        before = len(self.effects)
        self.run(commit_at + 0.1, p.Translation(UnitID(uid), p.Success(TranslationCandidate(UnitID(uid),
                                                                                           f"Sentence {uid}."))))
        return next((e.job for e in self.effects[before:] if isinstance(e, p.Synthesize)), None)

    def requested_fill(self, uid: int) -> bool:
        return any(isinstance(e, p.TraceOut) and e.event.kind == TraceKind.TTS_REQUEST
                   and e.event.value("unit") == f"u{uid}" and e.event.value("fill") is True for e in self.effects)


def test_a_phrase_ahead_of_its_slot_asks_to_be_filled_to_the_actor():
    s = FillSession(delay=8)
    job = s.speak(1, 0, 2.5, 2.8)
    assert job is not None and job.target_seconds == 2.5 and job.allow_fill
    assert s.requested_fill(1)


def test_live_playback_without_a_slot_never_slows_a_line():
    s = FillSession(delay=0)
    job = s.speak(1, 0, 2.5, 2.8)
    assert job is not None and job.target_seconds == 2.5 and not job.allow_fill
    assert not s.requested_fill(1)


def test_a_phrase_already_late_for_its_slot_keeps_its_own_pace():
    # D = 1 s: the slot of speech that started at 0 passed long before the answer landed.
    s = FillSession(delay=1)
    job = s.speak(1, 0, 2.5, 3.0)
    assert job is not None and job.target_seconds == 2.5 and not job.allow_fill
    assert not s.requested_fill(1)


def test_estimated_timing_paces_against_nothing():
    s = FillSession(delay=8)
    job = s.speak(1, 0, 2.5, 2.8, timed=False)
    assert job is not None and job.target_seconds is None and not job.allow_fill
    assert not s.requested_fill(1)


def test_the_escape_hatch_leaves_every_take_at_its_own_pace():
    # `--no-turn-fill`: the same slotted session, for a listener to compare.
    s = FillSession(delay=8, fill_enabled=False)
    job = s.speak(1, 0, 2.5, 2.8)
    assert job is not None and job.target_seconds == 2.5 and not job.allow_fill
    assert not s.requested_fill(1)


def test_each_clause_is_filled_to_its_own_speech_end():
    s = FillSession(delay=8)
    job = s.speak(1, 0, 1.2, 1.5)
    assert job is not None and job.target_seconds == 1.2 and job.allow_fill
