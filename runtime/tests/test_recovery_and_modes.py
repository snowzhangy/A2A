"""Ports of PlaybackModeTests, RecoveryTests (incl. SpeechEvidenceTests), SessionStateMachineTests,
and TranslationDirectionTests."""

from __future__ import annotations

import pytest

from a2a_runtime.core import pipeline as p
from a2a_runtime.core import state_machine as sm
from a2a_runtime.core.commit_policy import Commit, CommitPolicy
from a2a_runtime.core.direction import TranslationDirection, TranslationLanguage
from a2a_runtime.core.pipeline import PlaybackMode
from a2a_runtime.core.speech_evidence import SpeechEvidence
from a2a_runtime.core.state_machine import BusGains, HealthSample, SessionStateMachine, UnitReadiness
from a2a_runtime.core.trace import TraceKind
from a2a_runtime.core.types import (ASRHypothesis, ASRToken, PCMChunkHeader, PCMFormat, SourceSpan, SpeakerID,
                                    SpeechEnd, SpeechStart, TranslationCandidate, UnitID)
from a2a_runtime.core.validator import TranslationValidator

SPK = SpeakerID("S1")
S2 = SpeakerID("S2")
SEC = 1_000_000_000


def hypothesis(text: str, utterance: int = 1) -> ASRHypothesis:
    return ASRHypothesis(utterance, (ASRToken(text),), True, 48_000)


def hdr(uid: int, generation: int = 1) -> PCMChunkHeader:
    return PCMChunkHeader(UnitID(uid), SPK, 1, 1, generation, PCMFormat(24_000, 1), SourceSpan(0, 48_000), False)


def ok(uid: int, text: str) -> p.Translation:
    return p.Translation(UnitID(uid), p.Success(TranslationCandidate(UnitID(uid), text)))


def traces(effects):
    return [e.event for e in effects if isinstance(e, p.TraceOut)]


# MARK: Playback mode


def mode_session(mode=PlaybackMode.TRANSLATED) -> p.SessionPipeline:
    config = p.PipelineConfig(session="playback-switch")
    config.original_audio_enabled = False
    config.playback_mode = mode
    pl = p.SessionPipeline(config)
    for inp in (p.Arm(), p.ArmSucceeded(), p.ProfileReady(SPK, 1), p.Vad(SpeechStart(0))):
        pl.handle(inp, 0)
    return pl


def ready_unit(pl: p.SessionPipeline) -> list:
    pl.handle(p.Hypothesis(ASRHypothesis(1, (ASRToken("我们开会。"),), True, 48_000)), 1)
    pl.handle(ok(1, "We will meet."), 2)
    return pl.handle(p.PCM(hdr(1), 4_800), 3)


def silent_ramp(effects) -> bool:
    return any(isinstance(e, p.RampGains) and e.duration_ms == 30 and e.gains.original == 0 and e.gains.translated == 0
               for e in effects)


def test_original_can_be_selected_before_starting():
    pl = p.SessionPipeline(p.PipelineConfig(session="idle-switch"))
    assert pl.handle(p.SetPlaybackMode(PlaybackMode.ORIGINAL), 0) == []
    pl.handle(p.Arm(), 1)
    assert p.RampGains(BusGains.ORIGINAL, 0) in pl.handle(p.ArmSucceeded(), 2)
    assert pl.playback_mode is PlaybackMode.ORIGINAL


def test_switching_back_and_forth_keeps_the_session_and_voice_warm():
    pl = mode_session()
    ready_unit(pl)
    epoch, generation = pl.gate.epoch, pl.gate.generation
    original = pl.handle(p.SetPlaybackMode(PlaybackMode.ORIGINAL), 4)
    assert p.RampGains(BusGains.ORIGINAL, 30) in original and pl.state == sm.translating(SPK)
    translated = pl.handle(p.SetPlaybackMode(PlaybackMode.TRANSLATED), 5)
    assert p.RampGains(BusGains.TRANSLATED, 30) in translated
    assert p.FlushTranslatedBus() not in original and p.Teardown() not in translated
    assert (pl.gate.epoch, pl.gate.generation) == (epoch, generation) and pl.machine.has_profile(SPK)


def test_background_handoffs_and_recovery_cannot_override_original_selection():
    pl = mode_session(PlaybackMode.ORIGINAL)
    assert p.RampGains(BusGains.ORIGINAL, 30) in ready_unit(pl)
    assert p.RampGains(BusGains.ORIGINAL, 30) in pl.handle(p.WorkerHealthy(False), 4)
    assert p.RampGains(BusGains.ORIGINAL, 30) in pl.handle(p.Discontinuity("device changed"), 5)
    assert pl.playback_mode is PlaybackMode.ORIGINAL


def test_translated_selection_stays_silent_until_english_is_available():
    pl = mode_session(PlaybackMode.ORIGINAL)
    assert silent_ramp(pl.handle(p.SetPlaybackMode(PlaybackMode.TRANSLATED), 1))
    assert p.RampGains(BusGains.TRANSLATED, 30) in ready_unit(pl)
    assert silent_ramp(pl.handle(p.WorkerHealthy(False), 4))


def test_muted_english_does_not_count_as_played_translation():
    pl = mode_session(PlaybackMode.ORIGINAL)
    ready_unit(pl)
    start = pl.handle(p.PlayStarted(UnitID(1), 0), 4)
    assert not any(e.kind == TraceKind.PLAY_START for e in traces(start))
    end = pl.handle(p.PlayEnded(UnitID(1), 9_600), 5)
    assert any(e.kind == TraceKind.SOURCE_DISPOSITION and "user_original_playback" in e.json_line() for e in traces(end))
    assert not any(e.kind == TraceKind.PLAY_END for e in traces(end))


def test_switching_mid_unit_does_not_count_partial_english_as_complete():
    pl = mode_session()
    ready_unit(pl)
    pl.handle(p.PlayStarted(UnitID(1), 0), 4)
    pl.handle(p.SetPlaybackMode(PlaybackMode.ORIGINAL), 5)
    pl.handle(p.SetPlaybackMode(PlaybackMode.TRANSLATED), 6)
    end = pl.handle(p.PlayEnded(UnitID(1), 9_600), 7)
    assert any("user_original_playback" in e.json_line() for e in traces(end))


# MARK: Recovery


def armed(config: p.PipelineConfig | None = None) -> p.SessionPipeline:
    pl = p.SessionPipeline(config or p.PipelineConfig(session="recovery"))
    for inp in (p.Arm(), p.ArmSucceeded(), p.ProfileReady(SPK, 1), p.Vad(SpeechStart(0))):
        pl.handle(inp, 0)
    return pl


def start_playing(pl: p.SessionPipeline) -> None:
    pl.handle(p.Hypothesis(hypothesis("我们开会。")), 1)
    pl.handle(ok(1, "We will meet."), 2)
    pl.handle(p.PCM(hdr(1), 4_800), 3)


def test_device_change_flushes_and_restores_original():
    pl = armed()
    start_playing(pl)
    assert pl.state == sm.translating(SPK)
    effects = pl.handle(p.Discontinuity("headphones removed"), 4)
    assert p.FlushTranslatedBus() in effects and p.RampGains(BusGains.ORIGINAL, 30) in effects
    assert pl.state == sm.LISTENING and pl.gate.epoch == 2 and not pl.machine.has_profile(SPK)


def test_active_synthesis_failure_restores_original_immediately():
    pl = armed()
    start_playing(pl)
    effects = pl.handle(p.SynthesisFailed(UnitID(1), "worker stopped"), 4)
    assert pl.state == sm.DEGRADED
    assert p.RampGains(BusGains.ORIGINAL, 30) in effects and p.FlushTranslatedBus() in effects


def test_late_failure_cannot_interrupt_a_new_generation():
    pl = armed()
    start_playing(pl)
    pl.handle(p.Discontinuity("restart"), 4)
    assert pl.handle(p.SynthesisFailed(UnitID(1), "late cancellation"), 5) == []


def test_translation_failure_while_playing_restores_original():
    pl = armed()
    start_playing(pl)
    pl.handle(p.Hypothesis(hypothesis("第二句。", 2)), 4)
    effects = pl.handle(p.Translation(UnitID(2), p.Failure("translation unavailable")), 5)
    assert pl.state == sm.DEGRADED
    assert p.RampGains(BusGains.ORIGINAL, 30) in effects and p.FlushTranslatedBus() in effects


def test_voice_acquisition_bounds_pending_translations_and_cancels_work():
    config = p.PipelineConfig(session="bounded")
    config.max_pending_units = 2
    pl = p.SessionPipeline(config)
    pl.handle(p.Arm(), 0)
    pl.handle(p.ArmSucceeded(), 0)
    for uid in (1, 2):
        pl.handle(p.Hypothesis(hypothesis("测试。", uid)), uid)
    effects = pl.handle(p.Hypothesis(hypothesis("测试。", 3)), 3)
    assert p.CancelTranslation(UnitID(1)) in effects
    stale = pl.handle(ok(1, "Stale."), 4)
    assert not any(isinstance(e, p.Synthesize) for e in stale)


def test_captions_do_not_put_text_in_diagnostics():
    config = p.PipelineConfig(session="private")
    config.emit_captions = True
    pl = p.SessionPipeline(config)
    pl.handle(p.Arm(), 0)
    pl.handle(p.ArmSucceeded(), 0)
    effects = pl.handle(p.Hypothesis(hypothesis("私密内容。")), 1)
    assert p.Caption("source.final", "utt1", "私密内容。") in effects
    assert all("私密" not in e.json_line() for e in traces(effects))


def test_stopped_session_ignores_late_recognition_and_profiles():
    pl = armed()
    pl.handle(p.Stop(), 1)
    assert pl.handle(p.Hypothesis(hypothesis("迟到的结果。")), 2) == []
    assert pl.handle(p.ProfileReady(SPK, 2), 3) == []
    assert pl.state == sm.IDLE


def test_framework_error_descriptions_cannot_leak_text_to_default_diagnostics():
    pl = armed()
    pl.handle(p.Hypothesis(hypothesis("私密内容。")), 1)
    effects = pl.handle(p.Translation(UnitID(1), p.Failure("error processing 私密内容。")), 2)
    assert all("私密" not in e.json_line() for e in traces(effects))


def test_stale_held_audio_cannot_prevent_recovery_with_fresh_speech():
    config = p.PipelineConfig(session="recovery")
    config.health.stable_retry_ns = 500_000_000
    pl = p.SessionPipeline(config)

    def s(x: float) -> int:
        return int(x * 1e9)

    def timed(uid: int, start: int, end: int) -> ASRHypothesis:
        return ASRHypothesis(uid, (ASRToken("测试。", SourceSpan(start, end)),), True, end)

    for inp in (p.Arm(), p.ArmSucceeded(), p.ProfileReady(SPK, 1), p.Vad(SpeechStart(0))):
        pl.handle(inp, 0)
    pl.handle(p.Captured(SourceSpan(0, 48_000)), s(1))
    pl.handle(p.Hypothesis(timed(1, 0, 48_000)), s(1))
    pl.handle(ok(1, "First."), s(1.1))
    pl.handle(p.PCM(hdr(1, 1), 4_800), s(1.2))
    pl.handle(p.WorkerHealthy(False), s(1.3))
    assert pl.state == sm.DEGRADED
    pl.handle(p.Hypothesis(timed(2, 48_000, 96_000)), s(2))
    pl.handle(ok(2, "Second."), s(2.1))
    pl.handle(p.PCM(hdr(2, 2), 4_800), s(2.2))
    pl.handle(p.Captured(SourceSpan(48_000, 384_000)), s(8))
    pl.handle(p.WorkerHealthy(True), s(8))
    assert p.DiscardHeld(UnitID(2)) in pl.handle(p.Tick(), s(8))
    pl.handle(p.Hypothesis(timed(3, 336_000, 384_000)), s(8))
    pl.handle(ok(3, "Fresh."), s(8.01))
    pl.handle(p.PCM(hdr(3, 2), 4_800), s(8.02))
    pl.handle(p.Tick(), s(8.1))
    recovered = pl.handle(p.Tick(), s(8.7))
    assert pl.state == sm.translating(SPK)
    assert p.ReleaseHeld(UnitID(3)) in recovered and p.ReleaseHeld(UnitID(2)) not in recovered


def test_translation_only_never_enables_original_on_any_recovery_path():
    config = p.PipelineConfig(session="english-only")
    config.original_audio_enabled = False
    pl = p.SessionPipeline(config)
    effects = []
    for inp in (p.Arm(), p.ArmSucceeded(), p.ProfileReady(SPK, 1), p.Vad(SpeechStart(0))):
        effects += pl.handle(inp, 0)
    start_playing(pl)
    assert pl.state == sm.translating(SPK)
    effects += pl.handle(p.WorkerHealthy(False), 4)
    effects += pl.handle(p.Discontinuity("device change"), 5)
    effects += pl.handle(p.Stop(), 6)
    ramps = [e.gains for e in effects if isinstance(e, p.RampGains)]
    assert ramps and all(g.original == 0 for g in ramps)


def test_speech_evidence_rejects_silence_but_accepts_delayed_final():
    ev = SpeechEvidence()
    ev.advance(96_000)
    h = ASRHypothesis(1, (ASRToken("好的", SourceSpan(0, 48_000)),), True, 96_000)
    assert not ev.accepts(h)
    ev.process(SpeechStart(0))
    ev.process(SpeechEnd(48_000))
    assert ev.accepts(h)
    silence = ASRHypothesis(2, (ASRToken("you", SourceSpan(72_000, 90_000)),), True, 96_000)
    assert not ev.accepts(silence)


def test_old_speech_does_not_qualify_new_untimed_hallucinations():
    ev = SpeechEvidence(history_seconds=1)
    ev.process(SpeechStart(0))
    ev.process(SpeechEnd(48_000))
    ev.advance(192_000)
    assert not ev.accepts(hypothesis("you"))


# MARK: State machine


def ready(unit, spk=SPK, gen=1, buffered=0.1, complete=False):
    return sm.SUnitReady(UnitReadiness(UnitID(unit), spk, gen, buffered, complete))


def listening() -> SessionStateMachine:
    m = SessionStateMachine()
    m.handle(sm.SArm(), 0)
    m.handle(sm.SArmSucceeded(), 0)
    return m


def translating_machine() -> SessionStateMachine:
    m = listening()
    m.handle(sm.SProfileReady(SPK, 1), 0)
    m.handle(sm.SSpeech(SPK), 0)
    m.handle(ready(1), 0)
    return m


def test_arm_reaches_listening_with_original_bus():
    m = SessionStateMachine()
    m.handle(sm.SArm(), 0)
    assert m.state == sm.ARMING
    fx = m.handle(sm.SArmSucceeded(), 1)
    assert m.state == sm.LISTENING and sm.SRampGains(BusGains.ORIGINAL, 0) in fx


def test_profile_ready_alone_does_not_start_translation():
    m = listening()
    m.handle(sm.SSpeech(SPK), 0)
    assert m.handle(sm.SProfileReady(SPK, 1), 1) == [] and m.state == sm.acquiring_voice(SPK)


def test_handoff_needs_profile_prebuffer_and_current_generation():
    m = listening()
    m.handle(sm.SSpeech(SPK), 0)
    m.handle(ready(1), 1)
    assert m.state == sm.acquiring_voice(SPK)
    m.handle(sm.SProfileReady(SPK, 1), 2)
    m.handle(ready(2, buffered=0.02), 3)
    assert m.state == sm.acquiring_voice(SPK)
    m.handle(ready(3, gen=0), 4)
    assert m.state == sm.acquiring_voice(SPK)
    fx = m.handle(ready(4), 5)
    assert m.state == sm.translating(SPK)
    assert sm.Handoff(UnitID(4)) in fx and sm.SRampGains(BusGains.TRANSLATED, 30) in fx


def test_short_complete_unit_is_ready_below_prebuffer():
    m = listening()
    m.handle(sm.SProfileReady(SPK, 1), 0)
    m.handle(sm.SSpeech(SPK), 0)
    m.handle(ready(1, buffered=0.03, complete=True), 1)
    assert m.state == sm.translating(SPK)


def test_debt_above_threshold_degrades_and_advances_generation():
    m = translating_machine()
    assert m.handle(sm.SHealth(HealthSample(2.9, 1)), SEC) == []
    fx = m.handle(sm.SHealth(HealthSample(3.1, 1)), 2 * SEC)
    assert m.state == sm.DEGRADED
    for e in (sm.AdvanceGeneration(), sm.CancelPendingSynthesis(), sm.FallbackEnter("debt"),
              sm.SRampGains(BusGains.ORIGINAL, 30)):
        assert e in fx


@pytest.mark.parametrize("sample", [HealthSample(0, 4.5), HealthSample(0, 0, underrun=True),
                                    HealthSample(0, 0, service_lost=True)])
def test_source_lag_underrun_and_service_loss_degrade(sample):
    m = translating_machine()
    m.handle(sm.SHealth(sample), SEC)
    assert m.state == sm.DEGRADED


def test_recovery_requires_stable_interval_healthy_worker_and_fresh_unit():
    m = translating_machine()
    m.handle(sm.SHealth(HealthSample(5, 0)), SEC)
    m.sync_generation(2)
    healthy = HealthSample(0, 0)
    m.handle(sm.SHealth(healthy), 2 * SEC)
    m.handle(ready(5, gen=2), 3 * SEC)
    assert m.state == sm.DEGRADED
    m.handle(sm.SHealth(HealthSample(4, 0)), 3 * SEC + 1)
    m.handle(sm.SHealth(healthy), 4 * SEC)
    m.handle(ready(6, gen=2), 5 * SEC)
    assert m.state == sm.DEGRADED
    m.handle(ready(7, gen=1), 6 * SEC + 1)
    assert m.state == sm.DEGRADED
    fx = m.handle(ready(8, gen=2), 6 * SEC + 1)
    assert m.state == sm.translating(SPK) and sm.FallbackExit(UnitID(8)) in fx


def test_worker_loss_blocks_recovery():
    m = translating_machine()
    m.handle(sm.SWorkerHealthy(False), SEC)
    assert m.state == sm.DEGRADED
    m.sync_generation(2)
    m.handle(sm.SHealth(HealthSample(0, 0)), 2 * SEC)
    m.handle(ready(2, gen=2), 5 * SEC)
    assert m.state == sm.DEGRADED
    m.handle(sm.SWorkerHealthy(True), 5 * SEC)
    m.handle(sm.SHealth(HealthSample(0, 0)), 5 * SEC)
    m.handle(ready(3, gen=2), 8 * SEC)
    assert m.state == sm.translating(SPK)


def test_speaker_change_without_profile_returns_to_original():
    m = translating_machine()
    fx = m.handle(sm.SSpeakerChange(S2), SEC)
    assert m.state == sm.acquiring_voice(S2)
    assert sm.AdvanceGeneration() in fx and sm.SRampGains(BusGains.ORIGINAL, 30) in fx
    m.sync_generation(2)
    m.handle(ready(4, spk=SPK, gen=2), 2 * SEC)
    assert m.state == sm.acquiring_voice(S2)


def test_stop_from_any_state_tears_down():
    m = translating_machine()
    assert sm.STeardown() in m.handle(sm.SStop(), SEC) and m.state == sm.IDLE
    assert m.handle(sm.SStop(), SEC) == []


# MARK: Direction


def test_directions_select_all_stage_languages():
    fwd, rev = TranslationDirection.CHINESE_TO_ENGLISH, TranslationDirection.ENGLISH_TO_CHINESE
    assert fwd.source == rev.target and fwd.target == rev.source
    assert rev.source.speech_locale_identifier == "en-US" and rev.source.translation_identifier == "en"
    assert rev.target.speech_locale_identifier == "zh-CN" and rev.target.translation_identifier == "zh-Hans"
    assert rev.target.clone_language == "chinese"


def test_validator_checks_the_selected_target():
    english = TranslationValidator(TranslationLanguage.ENGLISH)
    chinese = TranslationValidator(TranslationLanguage.MANDARIN)
    src = "We will meet next week."

    def c(text, complete=True):
        return TranslationCandidate(UnitID(1), text, complete)

    assert chinese.validate(c("我们下周开会。"), src, None) is None
    assert chinese.validate(c("请在 Zoom 开会。"), src, None) is None
    assert chinese.validate(c(src), src, None) == "wrong_language"
    assert english.validate(c("我们下周开会。"), src, None) == "wrong_language"
    assert english.validate(c(src), "我们下周开会。", None) is None
    assert chinese.validate(c(" "), src, None) == "empty"
    assert chinese.validate(c("我们下周开会。", False), src, None) == "incomplete"
    assert chinese.validate(c("我们下周开会。"), src, "我们下周开会。") == "duplicate"
    assert chinese.validate(c("会" * 200), src, None) == "too_long"


@pytest.mark.parametrize("direction", list(TranslationDirection))
def test_selected_pair_is_traced_and_target_is_synthesized(direction):
    config = p.PipelineConfig(session="direction", direction=direction)
    config.emit_captions = True
    pl = p.SessionPipeline(config)
    start = pl.handle(p.Arm(), 0)
    assert any(e.kind == TraceKind.SESSION_START and e.value("pair") == direction.trace_pair for e in traces(start))
    pl.handle(p.ArmSucceeded(), 0)
    pl.handle(p.ProfileReady(SPK, 1), 0)
    source = "We will meet next week." if direction.source is TranslationLanguage.ENGLISH else "我们下周开会。"
    target = "我们下周开会。" if direction.target is TranslationLanguage.MANDARIN else "We will meet next week."
    committed = pl.handle(p.Hypothesis(ASRHypothesis(1, (ASRToken(source),), True, 48_000)), SEC)
    assert any(isinstance(e, p.Translate) and e.request.uncommitted_source == source for e in committed)
    effects = pl.handle(ok(1, target), 1_100_000_000)
    assert any(isinstance(e, p.Synthesize) and e.job.text == target for e in effects)
    assert p.Caption("target.final", "u1", target) in effects


def test_english_length_cap_keeps_words_whole():
    for text, cap in [("We will discuss commercial quality tomorrow", 15), ("Please don't stop testing tomorrow", 10)]:
        config = p.PipelineConfig(session="english", direction=TranslationDirection.ENGLISH_TO_CHINESE).commit
        config.max_unit_keys = cap
        policy = CommitPolicy(config)
        partial = ASRHypothesis(1, (ASRToken(text),), False, 48_000)
        policy.process(partial, 0)
        units = [o.unit for o in policy.process(partial, 100_000_000) if isinstance(o, Commit)]
        words = set(text.split(" "))
        assert units and all(all(w in words for w in u.text.split(" ")) for u in units)
