"""Ports of FluentPlayoutTests.swift (incl. StreamingClauseTests) and MultiSpeakerTests.swift."""

from __future__ import annotations

from a2a_runtime.core import pipeline as p
from a2a_runtime.core.commit_policy import Commit, CommitPolicy, CommitPolicyConfig, CommitReason
from a2a_runtime.core.pipeline import round_half_away
from a2a_runtime.core.state_machine import DEGRADED, translating
from a2a_runtime.core.trace import TraceKind
from a2a_runtime.core.types import (ASRHypothesis, ASRToken, PCMChunkHeader, PCMFormat, SourceSpan, SpeakerAssign,
                                    SpeakerID, SpeechStart, TranslationCandidate, UnitID)

S1, S2 = SpeakerID("S1"), SpeakerID("S2")
RATE = 48_000.0


def ns(seconds: float) -> int:
    return round_half_away(seconds * 1e9)


def span(start: float, end: float) -> SourceSpan:
    return SourceSpan(int(start * RATE), int(end * RATE))


def header(uid: int, speaker: SpeakerID = S1, generation: int = 1, unit_span: SourceSpan = SourceSpan(0, 0)):
    return PCMChunkHeader(UnitID(uid), speaker, 1, 1, generation, PCMFormat(24_000, 1), unit_span, False)


def has(effects, kind) -> bool:
    return any(isinstance(e, kind) for e in effects)


class LiveSession:
    """Drives the reducer on a live-like timeline: capture advances with event time."""

    def __init__(self, config: p.PipelineConfig, voices=(S1,)) -> None:
        self.p = p.SessionPipeline(config)
        self.effects: list = []
        self._captured = 0
        self.run(0, p.Arm())
        self.run(0, p.ArmSucceeded())
        for v in voices:
            self.run(0, p.ProfileReady(v, 1))
        self.run(0, p.Vad(SpeechStart(0)))

    def run(self, t: float, inp) -> list:
        frontier = int(t * RATE)
        if frontier > self._captured:
            self.effects += self.p.handle(p.Captured(SourceSpan(self._captured, frontier)), ns(t))
            self._captured = frontier
        out = self.p.handle(inp, ns(t))
        self.effects += out
        return out

    def commit(self, uid: int, start: float, end: float, t: float) -> list:
        s = span(start, end)
        out = self.run(t, p.Hypothesis(ASRHypothesis(uid, (ASRToken(f"测试第{uid}句。", s),), True, s.end)))
        return out + self.run(t + 0.1, p.Translation(UnitID(uid), p.Success(TranslationCandidate(UnitID(uid), f"Sentence {uid}."))))

    def pcm(self, uid: int, seconds: float, t: float, generation: int = 1, speaker: SpeakerID = S1) -> list:
        return self.run(t, p.PCM(header(uid, speaker, generation), int(seconds * 24_000)))

    def tick(self, t: float, backlog: float) -> list:
        return self.run(t, p.TranslatedBacklog(backlog)) + self.run(t, p.Tick())

    def traces(self, kind: TraceKind):
        return [e.event for e in self.effects if isinstance(e, p.TraceOut) and e.event.kind == kind]

    def canceled(self, reason: str) -> list[str]:
        return [e.value("unit") for e in self.traces(TraceKind.UNIT_CANCEL) if e.value("reason") == reason]

    def jobs(self, effects=None) -> list[p.SynthesisJob]:
        return [e.job for e in (self.effects if effects is None else effects) if isinstance(e, p.Synthesize)]

    def label(self, speaker: SpeakerID, start: float, end: float, t: float) -> None:
        self.run(t, p.Speaker(SpeakerAssign(speaker, span(start, end))))


def fluent(fluent=True, original_audio=False, prebuffer=0.08, max_debt=3.0) -> LiveSession:
    config = p.PipelineConfig(session="fluent")
    config.finish_phrase_before_catch_up = fluent
    config.original_audio_enabled = original_audio
    config.health.prebuffer_seconds = prebuffer
    config.health.max_debt_seconds = max_debt
    return LiveSession(config)


def dialogue(voices=(S1, S2), fluent_playout=True) -> LiveSession:
    config = p.PipelineConfig(session="dialogue")
    config.multi_speaker = True
    config.finish_phrase_before_catch_up = fluent_playout
    config.original_audio_enabled = False
    return LiveSession(config, voices)


# MARK: Fluent playout


def test_empty_bus_does_not_starve_by_canceling_every_in_flight_synthesis():
    s = fluent(max_debt=0.3)
    s.commit(1, 0, 1, 1)
    s.pcm(1, 0.2, 1.2)
    s.run(1.3, p.PlayStarted(UnitID(1), 0))
    s.run(1.4, p.SynthesisFinished(UnitID(1), 1))
    s.run(1.5, p.PlayEnded(UnitID(1), 9_600))
    s.commit(2, 1, 2, 2.2)
    s.commit(3, 2, 3, 3.2)
    s.commit(4, 3, 4, 4.2)
    effects = s.tick(4.4, 0)
    assert p.CancelSynthesis(UnitID(2)) not in effects
    assert p.CancelSynthesis(UnitID(3)) in effects
    assert p.CancelSynthesis(UnitID(4)) not in effects
    assert p.ReleaseHeld(UnitID(2)) in s.pcm(2, 0.3, 4.5)


def test_long_session_prunes_terminal_bookkeeping_and_still_rejects_late_pcm():
    s = fluent()
    for uid in range(1, 601):
        t = uid * 1.2
        s.commit(uid, uid - 1, uid, t)
        s.pcm(uid, 0.1, t + 0.2)
        s.run(t + 0.3, p.PlayStarted(UnitID(uid), (uid - 1) * 4_800))
        s.run(t + 0.4, p.SynthesisFinished(UnitID(uid), 1))
        s.run(t + 0.5, p.PlayEnded(UnitID(uid), uid * 4_800))
    hdr = lambda uid: header(uid, unit_span=SourceSpan(0, 4_800))  # noqa: E731
    retained = [uid for uid in range(1, 601) if s.p.gate.admit(hdr(uid)).reason == "already_finished"]
    assert len(retained) <= 256 and 600 in retained
    assert s.p.gate.admit(hdr(1)).reason == "retired_unit"
    late = s.run(721, p.PCM(hdr(1), 2_400))
    assert has(late, p.DropPCM) and not has(late, p.EnqueuePCM) and not has(late, p.HoldPCM)


def test_every_phrase_waits_for_its_prebuffer():
    s = fluent(prebuffer=0.24)
    s.commit(1, 0, 2, 2.5)
    s.pcm(1, 0.3, 2.7)
    s.run(2.8, p.PlayStarted(UnitID(1), 0))
    s.run(2.9, p.SynthesisFinished(UnitID(1), 1))
    s.run(3.0, p.PlayEnded(UnitID(1), 14_400))
    s.commit(2, 2, 3, 3.1)
    first = s.pcm(2, 0.1, 3.3)
    assert has(first, p.HoldPCM) and p.ReleaseHeld(UnitID(2)) not in first and not has(first, p.EnqueuePCM)
    assert p.ReleaseHeld(UnitID(2)) not in s.pcm(2, 0.1, 3.4)
    assert p.ReleaseHeld(UnitID(2)) in s.pcm(2, 0.1, 3.5)


def test_short_completed_phrase_does_not_wait_forever_for_prebuffer():
    s = fluent(prebuffer=0.24)
    s.commit(1, 0, 2, 2.5)
    s.pcm(1, 0.3, 2.7)
    s.run(2.8, p.PlayStarted(UnitID(1), 0))
    s.commit(2, 2, 3, 3.1)
    s.pcm(2, 0.05, 3.3)
    assert p.ReleaseHeld(UnitID(2)) not in s.run(3.4, p.PlayEnded(UnitID(1), 14_400))
    assert p.ReleaseHeld(UnitID(2)) in s.run(3.5, p.SynthesisFinished(UnitID(2), 1))


def test_later_units_wait_behind_the_phrase_being_heard():
    s = fluent()
    s.commit(1, 0, 2, 2.5)
    assert p.ReleaseHeld(UnitID(1)) in s.pcm(1, 1, 2.7)  # voice handoff
    s.run(2.8, p.PlayStarted(UnitID(1), 0))
    s.commit(2, 2, 3, 3.2)
    held = s.pcm(2, 1, 3.4)
    assert has(held, p.HoldPCM) and not has(held, p.EnqueuePCM)
    assert p.ReleaseHeld(UnitID(2)) in s.run(4.8, p.PlayEnded(UnitID(1), 96_000))
    assert has(s.pcm(2, 0.5, 4.9), p.EnqueuePCM)


def test_soft_pressure_skips_unheard_units_without_cutting_the_phrase():
    s = fluent()
    s.commit(1, 0, 2, 2.5)
    s.pcm(1, 4, 2.7)
    s.run(2.8, p.PlayStarted(UnitID(1), 0))
    s.run(2.9, p.SynthesisFinished(UnitID(1), 1))
    s.commit(2, 2, 4, 4.5)
    s.pcm(2, 4, 4.7)
    s.run(4.8, p.SynthesisFinished(UnitID(2), 1))
    s.commit(3, 4, 6, 6.5)
    caught_up = s.tick(6.6, 0.9)
    assert s.p.state == translating(S1)
    assert s.canceled("catch_up") == ["u2"]
    assert p.FlushTranslatedBus() not in caught_up
    assert p.CancelSynthesis(UnitID(1)) not in s.effects
    assert len(s.traces(TraceKind.CATCH_UP)) == 1 and not s.traces(TraceKind.FALLBACK_ENTER)
    s.pcm(3, 4, 6.8)
    s.tick(6.9, 0.7)
    assert s.canceled("catch_up") == ["u2"]
    assert p.ReleaseHeld(UnitID(3)) in s.run(6.9, p.PlayEnded(UnitID(1), 192_000))


def test_continuous_speech_never_reaches_the_queue_cap():
    s = fluent()
    t = 2.5
    for uid in range(1, 13):
        s.commit(uid, (uid - 1) * 2, uid * 2, t)
        s.pcm(uid, 3, t + 0.2)
        s.run(t + 0.25, p.SynthesisFinished(UnitID(uid), 1))
        if uid == 1:
            s.run(t + 0.3, p.PlayStarted(UnitID(1), 0))
        s.tick(t + 0.3, 1.5)
        t += 2
    assert s.p.state == translating(S1)
    assert s.canceled("queue_limit") == [] and s.canceled("catch_up")
    assert p.FlushTranslatedBus() not in s.effects


def test_english_only_playback_skips_a_missing_unit_instead_of_falling_back():
    s = fluent()
    s.commit(1, 0, 2, 2.5)
    s.pcm(1, 2, 2.7)
    s.run(2.8, p.PlayStarted(UnitID(1), 0))
    s.run(3.0, p.Hypothesis(ASRHypothesis(2, (ASRToken("第二句。"),), True, 144_000)))
    failed = s.run(3.1, p.Translation(UnitID(2), p.Failure("translation unavailable")))
    assert s.p.state == translating(S1) and p.FlushTranslatedBus() not in failed
    s.commit(3, 3, 4, 4.2)
    synth_failed = s.run(4.4, p.SynthesisFailed(UnitID(3), "worker hiccup"))
    assert s.p.state == translating(S1) and p.FlushTranslatedBus() not in synth_failed


def test_audible_original_still_falls_back_for_a_missing_unit():
    s = fluent(original_audio=True)
    s.commit(1, 0, 2, 2.5)
    s.pcm(1, 2, 2.7)
    s.run(3.0, p.Hypothesis(ASRHypothesis(2, (ASRToken("第二句。"),), True, 144_000)))
    failed = s.run(3.1, p.Translation(UnitID(2), p.Failure("translation unavailable")))
    assert s.p.state == DEGRADED and p.FlushTranslatedBus() in failed


def test_recovery_after_worker_loss_ignores_soft_pressure():
    s = fluent()
    s.commit(1, 0, 2, 2.5)
    s.pcm(1, 2, 2.7)
    s.run(2.8, p.WorkerHealthy(False))
    assert s.p.state == DEGRADED
    s.run(3.0, p.WorkerHealthy(True))
    s.run(3.5, p.Vad(SpeechStart(int(3.5 * RATE))))
    s.tick(3.6, 0)
    s.commit(2, 3.5, 9.5, 10.0)  # long unit: its onset is 6.5 s old
    s.tick(10.2, 0)
    s.pcm(2, 3, 10.3, generation=2)
    s.tick(10.4, 0)
    assert s.p.state == translating(S1) and s.canceled("source_expired") == []


def test_fluent_jobs_carry_the_source_duration_as_target():
    a = fluent()
    a.commit(1, 0.5, 2.5, 3)
    b = fluent(fluent=False)
    b.commit(1, 0.5, 2.5, 3)
    assert [j.target_seconds for j in a.jobs()] == [2.0]
    assert [j.target_seconds for j in b.jobs()] == [None]


# MARK: Streaming clauses


def _streaming() -> CommitPolicy:
    return CommitPolicy(CommitPolicyConfig(streaming_clauses=True))


def _units(outputs):
    return [o.unit for o in outputs if isinstance(o, Commit)]


def test_stable_conjunction_closes_a_clause_without_waiting_for_the_window():
    tokens = tuple(ASRToken(c) for c in "我们今天开会但是他们不来")
    h = ASRHypothesis(1, tokens, False, 480_000)
    q = _streaming()
    q.process(h, 0)
    out = _units(q.process(h, 100_000_000))
    assert [u.text for u in out] == ["我们今天开会"] and out[0].reason == CommitReason.CONJUNCTION
    plain = CommitPolicy()
    plain.process(h, 0)
    assert _units(plain.process(h, 100_000_000)) == []


def test_timed_pause_between_tokens_closes_a_clause():
    tokens, cursor = [], 0
    for i, ch in enumerate("我们今天开会他们不来"):
        if i == 6:
            cursor += 14_400
        tokens.append(ASRToken(ch, SourceSpan(cursor, cursor + 9_600)))
        cursor += 9_600
    h = ASRHypothesis(1, tuple(tokens), False, 480_000)
    q = _streaming()
    q.process(h, 0)
    out = _units(q.process(h, 100_000_000))
    assert [u.text for u in out] == ["我们今天开会"] and out[0].reason == CommitReason.PROSODIC_PAUSE
    assert out[0].span == SourceSpan(0, 57_600)


def test_contiguous_token_timing_is_not_a_pause():
    tokens = tuple(ASRToken(c, SourceSpan(i * 9_600, (i + 1) * 9_600)) for i, c in enumerate("我们今天开会他们不来"))
    h = ASRHypothesis(1, tokens, False, 480_000)
    q = _streaming()
    q.process(h, 0)
    assert _units(q.process(h, 100_000_000)) == []


# MARK: Multi-speaker


def test_each_unit_is_voiced_by_the_speaker_who_said_it():
    d = dialogue()
    d.label(S1, 0.2, 2.0, 2.3)
    d.commit(1, 0.2, 2.0, 2.6)
    d.label(S2, 2.4, 4.0, 4.3)
    d.commit(2, 2.4, 4.0, 4.6)
    assert [j.speaker for j in d.jobs()] == [S1, S2]
    committed = [e.json_line() for e in d.traces(TraceKind.COMMIT_SOURCE)]
    assert len(committed) == 2 and '"spk":"S1"' in committed[0] and '"spk":"S2"' in committed[1]


def test_a_unit_committed_before_its_label_takes_the_label_before_synthesis():
    d = dialogue()
    d.label(S1, 0.2, 2.0, 2.3)
    d.run(3.5, p.Hypothesis(ASRHypothesis(2, (ASRToken("好。", span(2.4, 3.4)),), True, span(2.4, 3.4).end)))
    d.label(S2, 2.4, 3.4, 3.55)
    d.run(3.6, p.Translation(UnitID(1), p.Success(TranslationCandidate(UnitID(1), "Fine."))))
    assert [j.speaker for j in d.jobs()] == [S2]


def test_speaker_turns_never_fall_back_or_cut_the_phrase():
    d = dialogue()
    d.label(S1, 0.2, 2.0, 2.3)
    d.commit(1, 0.2, 2.0, 2.6)
    d.pcm(1, 1, 2.8, speaker=S1)
    d.run(2.9, p.PlayStarted(UnitID(1), 0))
    before = len(d.effects)
    d.label(S2, 2.4, 4.0, 4.3)
    d.commit(2, 2.4, 4.0, 4.6)
    turn = d.effects[before:]
    assert p.FlushTranslatedBus() not in turn and not has(turn, p.CancelSynthesis) and not has(turn, p.RampGains)
    assert not d.traces(TraceKind.FALLBACK_ENTER) and len(d.traces(TraceKind.SPK_CHANGE)) == 1
    assert d.p.state.is_translating
    d.pcm(2, 1, 4.8, speaker=S2)
    assert p.ReleaseHeld(UnitID(2)) in d.run(5.0, p.PlayEnded(UnitID(1), 48_000))


def test_a_new_speakers_unit_waits_for_their_voice():
    d = dialogue(voices=(S1,))
    d.label(S1, 0.2, 2.0, 2.3)
    d.commit(1, 0.2, 2.0, 2.6)
    d.label(S2, 2.4, 4.0, 4.3)
    waiting = d.commit(2, 2.4, 4.0, 4.6)
    assert d.jobs(waiting) == []
    assert not any("no_profile" in e.json_line() for e in d.traces(TraceKind.SOURCE_DISPOSITION))
    ready = d.run(5.0, p.ProfileReady(S2, 1))
    assert [j.unit for j in d.jobs(ready)] == [UnitID(2)] and d.jobs(ready)[0].speaker == S2


def test_a_voice_that_never_arrives_expires_the_waiting_unit():
    d = dialogue(voices=(S1,))
    d.label(S2, 0.2, 2.0, 2.3)
    d.commit(1, 0.2, 2.0, 2.6)
    d.run(5.9, p.Tick())
    assert not any("no_profile" in e.json_line() for e in d.traces(TraceKind.SOURCE_DISPOSITION))
    d.run(6.1, p.Tick())
    assert len([e for e in d.traces(TraceKind.SOURCE_DISPOSITION) if e.value("reason") == "no_profile"]) == 1
    assert d.jobs(d.run(6.2, p.ProfileReady(S2, 1))) == []


def test_the_first_ready_unit_of_any_speaker_starts_translation():
    d = dialogue(voices=(S2,))
    d.label(S2, 0.2, 2.0, 2.3)
    d.commit(1, 0.2, 2.0, 2.6)
    assert p.ReleaseHeld(UnitID(1)) in d.pcm(1, 0.5, 2.8, speaker=S2)
    assert d.p.state == translating(S2)


def test_coverage_reports_the_dominant_speaker_and_labeled_share():
    d = dialogue()
    d.label(S1, 0, 1, 1.1)
    d.label(S2, 1.2, 4, 4.1)
    spk, share, labeled = d.p.speaker_coverage(span(0, 4))
    assert spk == S2 and abs(share - 0.7) < 1e-6 and abs(labeled - 0.95) < 1e-6
    assert d.p.speaker_for(span(4.5, 5)) == S2
    assert d.p.speaker_coverage(span(4.5, 5)) is None


def test_speech_the_tracker_never_labels_still_plays_in_the_default_voice():
    d = dialogue(voices=(S1,))
    d.commit(1, 0.2, 2.0, 2.6)
    d.commit(2, 2.4, 4.0, 4.6)
    assert [j.speaker for j in d.jobs()] == [S1, S1]
    assert p.ReleaseHeld(UnitID(1)) in d.pcm(1, 0.5, 2.8, speaker=S1)


def test_a_speakers_cast_voice_replaces_the_default_for_later_units():
    d = dialogue(voices=(S1,))
    d.commit(1, 0.2, 2.0, 2.6)
    d.label(S1, 2.4, 4.0, 4.3)
    d.run(4.4, p.ProfileReady(S1, 2))
    d.commit(2, 2.4, 4.0, 4.6)
    assert [j.profile_version for j in d.jobs()] == [1, 2]
