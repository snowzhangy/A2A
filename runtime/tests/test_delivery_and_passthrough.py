"""Ports of DeliveryConditioningTests.swift and PassthroughTests.swift (ADR-0015/0018)."""

from a2a_runtime.core import pipeline as p
from a2a_runtime.core.direction import TranslationDirection
from a2a_runtime.core.trace import TraceKind
from a2a_runtime.core.types import (
    ASRHypothesis, ASRToken, PCMChunkHeader, PCMFormat, SourceSpan, SpeakerAssign, SpeakerID,
    SpeechStart, TranslationCandidate, UnitID,
)

SPEAKER = SpeakerID("S1")
RATE = 48_000
SPAN = SourceSpan(0, 144_000)


def has(effects, kind):
    return any(isinstance(effect, kind) for effect in effects)


def synthesized(effects):
    return next((effect.job.text for effect in effects if isinstance(effect, p.Synthesize)), None)


class DeliverySession:
    def __init__(self, delivery):
        config = p.PipelineConfig(session="delivery")
        config.finish_phrase_before_catch_up = True
        config.original_audio_enabled = False
        config.health.max_debt_seconds = 30
        config.health.max_source_lag_seconds = 30
        config.clone_delivery_line = delivery
        self.p = p.SessionPipeline(config)
        self.effects = []
        self.captured = 0
        self.run(0, p.Arm())
        self.run(0, p.ArmSucceeded())
        self.run(0, p.ProfileReady(SPEAKER, 1))

    def run(self, t, inp):
        frontier = int(t * RATE)
        if frontier > self.captured:
            self.effects += self.p.handle(p.Captured(SourceSpan(self.captured, frontier)), int(t * 1e9))
            self.captured = frontier
        out = self.p.handle(inp, int(t * 1e9))
        self.effects += out
        return out

    def speak(self, uid, timed=True):
        span = SourceSpan(0, int(2.5 * RATE))
        self.run(2.8, p.Vad(SpeechStart(span.start)))
        self.run(2.8, p.Vad(p.SpeechEnd(span.end)))
        self.run(2.8, p.Hypothesis(ASRHypothesis(
            uid, (ASRToken(f"测试第{uid}句。", span if timed else None),), True, span.end)))
        before = len(self.effects)
        self.run(2.9, p.Translation(UnitID(uid), p.Success(
            TranslationCandidate(UnitID(uid), f"Sentence {uid}."))))
        return next((e.job for e in self.effects[before:] if isinstance(e, p.Synthesize)), None)

    def requested_delivery(self, uid):
        return any(isinstance(e, p.TraceOut) and e.event.kind == TraceKind.TTS_REQUEST
                   and e.event.value("unit") == f"u{uid}" and e.event.value("delivery") is True
                   for e in self.effects)


def test_delivery_requires_explicit_opt_in_and_real_timing():
    offered = DeliverySession(True).speak(1)
    assert offered.delivery_allowed
    pinned = DeliverySession(False)
    assert not pinned.speak(1).delivery_allowed
    assert not pinned.requested_delivery(1)
    estimated = DeliverySession(True)
    assert not estimated.speak(1, timed=False).delivery_allowed
    assert not estimated.requested_delivery(1)


def test_delivery_trace_and_source_words_are_available_to_the_driver():
    session = DeliverySession(True)
    session.speak(1)
    assert session.requested_delivery(1)
    assert session.p.source_text(UnitID(1)) == "测试第1句。"


class Direct:
    def __init__(self, restores=True, direction=TranslationDirection.CHINESE_TO_ENGLISH):
        config = p.PipelineConfig(session="passthrough", direction=direction)
        config.direct_audio_translation = True
        config.restores_original_voice = restores
        self.p = p.SessionPipeline(config)
        self.effects = []
        self.run(0, p.Arm())
        self.run(0, p.ArmSucceeded())
        self.run(0, p.ProfileReady(SPEAKER, 1))

    def run(self, t, inp):
        out = self.p.handle(inp, int(t * 1e9))
        self.effects += out
        return out

    def translate(self, uid, text, t):
        return self.run(t, p.Translation(UnitID(uid), p.Success(TranslationCandidate(UnitID(uid), text))))

    def reason(self, uid):
        return next((e.event.value("reason") for e in self.effects
                     if isinstance(e, p.TraceOut) and e.event.kind == TraceKind.SOURCE_DISPOSITION
                     and e.event.value("unit") == f"u{uid}"), None)


def test_target_language_line_restores_actor_and_late_translation_is_stale():
    session = Direct()
    session.run(3, p.AudioSegment(SPAN))
    out = session.run(3.1, p.SourceLanguage(SPAN, True))
    assert p.CancelTranslation(UnitID(1)) in out and p.RestoreOriginalVoice(SPAN) in out
    assert session.reason(1) == "same_language"
    assert synthesized(session.translate(1, "Hello there.", 3.4)) is None


def test_source_language_line_translates_and_late_lid_does_not_double_voice():
    session = Direct()
    session.run(3, p.AudioSegment(SPAN))
    assert session.run(3.1, p.SourceLanguage(SPAN, False)) == []
    assert synthesized(session.translate(1, "Hello there.", 3.2)) == "Hello there."
    late = session.run(3.4, p.SourceLanguage(SPAN, True))
    assert not has(late, p.RestoreOriginalVoice) and not has(late, p.CancelTranslation)


def test_unanswered_line_restores_only_when_host_supports_it():
    session = Direct()
    session.run(3, p.AudioSegment(SPAN))
    assert p.RestoreOriginalVoice(SPAN) in session.translate(1, "", 3.3)
    off = Direct(restores=False)
    off.run(3, p.AudioSegment(SPAN))
    assert p.RestoreOriginalVoice(SPAN) not in off.translate(1, "", 3.3)
    assert p.RestoreOriginalVoice(SPAN) not in off.run(3.4, p.SourceLanguage(SPAN, True))


def test_single_speaker_keeps_translation_while_its_first_voice_is_prepared():
    config = p.PipelineConfig(session="initial-voice")
    config.direct_audio_translation = True
    config.presentation_delay_seconds = 8
    config.restores_original_voice = True
    config.original_voice_decision_deadline_seconds = 5.3
    reducer = p.SessionPipeline(config)
    reducer.handle(p.Arm(), 0)
    reducer.handle(p.ArmSucceeded(), 0)
    reducer.handle(p.Captured(SPAN), 3_000_000_000)
    reducer.handle(p.AudioSegment(SPAN), 3_000_000_000)
    translated = reducer.handle(p.Translation(UnitID(1), p.Success(
        TranslationCandidate(UnitID(1), "Please send the report."))), 3_100_000_000)
    assert not has(translated, p.RestoreOriginalVoice) and not has(translated, p.Synthesize)
    assert not has(reducer.handle(p.Tick(), 3_200_000_000), p.RestoreOriginalVoice)
    ready = reducer.handle(p.ProfileReady(SPEAKER, 1), 3_300_000_000)
    jobs = [e.job for e in ready if isinstance(e, p.Synthesize)]
    assert len(jobs) == 1
    assert jobs[0].text == "Please send the report." and jobs[0].speaker == SPEAKER
    assert not has(reducer.handle(p.Tick(), 5_400_000_000), p.RestoreOriginalVoice)
    assert not has(reducer.handle(p.ProfileReady(SPEAKER, 2), 5_500_000_000), p.Synthesize)


def test_single_speaker_voice_wait_expires_and_never_speaks_over_restored_audio():
    config = p.PipelineConfig(session="initial-voice-expired")
    config.direct_audio_translation = True
    config.presentation_delay_seconds = 8
    config.restores_original_voice = True
    config.original_voice_decision_deadline_seconds = 5.3
    reducer = p.SessionPipeline(config)
    reducer.handle(p.Arm(), 0)
    reducer.handle(p.ArmSucceeded(), 0)
    reducer.handle(p.Captured(SPAN), 3_000_000_000)
    reducer.handle(p.AudioSegment(SPAN), 3_000_000_000)
    reducer.handle(p.Translation(UnitID(1), p.Success(
        TranslationCandidate(UnitID(1), "Hello."))), 3_100_000_000)
    assert has(reducer.handle(p.Tick(), 5_400_000_000), p.RestoreOriginalVoice)
    assert not has(reducer.handle(p.ProfileReady(SPEAKER, 1), 5_500_000_000), p.Synthesize)


def restoration_pipeline(session, line, attributed=True, profile=True):
    config = p.PipelineConfig(session=session)
    config.direct_audio_translation = True
    config.multi_speaker = True
    config.require_speaker_attribution = True
    config.presentation_delay_seconds = 8
    config.restores_original_voice = True
    config.original_voice_decision_deadline_seconds = 5.3
    reducer = p.SessionPipeline(config)
    reducer.handle(p.Arm(), 0)
    reducer.handle(p.ArmSucceeded(), 0)
    if profile:
        reducer.handle(p.ProfileReady(SPEAKER, 1), 0)
    reducer.handle(p.Captured(SourceSpan(0, 48_000)), 1_000_000_000)
    if attributed:
        reducer.handle(p.Speaker(SpeakerAssign(SPEAKER, line)), 1_000_000_000)
    reducer.handle(p.AudioSegment(line), 1_000_000_000)
    reducer.handle(p.Translation(UnitID(1), p.Success(TranslationCandidate(UnitID(1), "Hello."))), 1_100_000_000)
    return reducer


def test_unattributed_line_and_missing_profile_restore_before_bed_leaves_hold():
    line = SourceSpan(0, 24_000)
    reducer = restoration_pipeline("missing-speaker", line, attributed=False)
    assert p.RestoreOriginalVoice(line) not in reducer.handle(p.Tick(), 5_200_000_000)
    assert p.RestoreOriginalVoice(line) in reducer.handle(p.Tick(), 5_400_000_000)
    assert not has(reducer.handle(p.Speaker(SpeakerAssign(SPEAKER, line)), 5_500_000_000), p.Synthesize)

    line = SourceSpan(0, 48_000)
    reducer = restoration_pipeline("missing-profile", line, profile=False)
    assert p.RestoreOriginalVoice(line) in reducer.handle(p.Tick(), 5_400_000_000)
    assert not has(reducer.handle(p.ProfileReady(SPEAKER, 1), 5_500_000_000), p.Synthesize)


def test_stalled_translator_restores_and_ignores_late_result():
    config = p.PipelineConfig(session="stalled-mt")
    config.direct_audio_translation = True
    config.restores_original_voice = True
    config.original_voice_decision_deadline_seconds = 5.3
    reducer = p.SessionPipeline(config)
    line = SourceSpan(0, 48_000)
    for inp, at in [(p.Arm(), 0), (p.ArmSucceeded(), 0), (p.ProfileReady(SPEAKER, 1), 0),
                    (p.Captured(line), 1_000_000_000), (p.AudioSegment(line), 1_000_000_000)]:
        reducer.handle(inp, at)
    timeout = reducer.handle(p.Tick(), 5_400_000_000)
    assert p.CancelTranslation(UnitID(1)) in timeout and p.RestoreOriginalVoice(line) in timeout
    late = reducer.handle(p.Translation(UnitID(1), p.Success(TranslationCandidate(UnitID(1), "Hello."))), 5_500_000_000)
    assert not has(late, p.Synthesize)


def test_translation_and_synthesis_failures_restore_original_voice():
    session = Direct()
    session.run(3, p.AudioSegment(SPAN))
    assert p.RestoreOriginalVoice(SPAN) in session.run(3.1, p.Translation(UnitID(1), p.Failure("unavailable")))
    off = Direct(restores=False)
    off.run(3, p.AudioSegment(SPAN))
    assert p.RestoreOriginalVoice(SPAN) not in off.run(3.1, p.Translation(UnitID(1), p.Failure("unavailable")))

    config = p.PipelineConfig(session="tts-failure")
    config.direct_audio_translation = True
    config.restores_original_voice = True
    config.original_voice_decision_deadline_seconds = 5.3
    reducer = p.SessionPipeline(config)
    line = SourceSpan(0, 48_000)
    for inp, at in [(p.Arm(), 0), (p.ArmSucceeded(), 0), (p.ProfileReady(SPEAKER, 1), 0),
                    (p.Captured(line), 1_000_000_000), (p.AudioSegment(line), 1_000_000_000),
                    (p.Translation(UnitID(1), p.Success(TranslationCandidate(UnitID(1), "Hello."))), 1_100_000_000)]:
        reducer.handle(inp, at)
    assert p.RestoreOriginalVoice(line) in reducer.handle(p.SynthesisFailed(UnitID(1), "failed"), 1_200_000_000)


def test_fallback_restores_only_unheard_canceled_units():
    config = p.PipelineConfig(session="fallback-restore")
    config.direct_audio_translation = True
    config.restores_original_voice = True
    config.original_voice_decision_deadline_seconds = 5.3
    reducer = p.SessionPipeline(config)
    first, second = SourceSpan(0, 48_000), SourceSpan(48_000, 96_000)
    for inp, at in [(p.Arm(), 0), (p.ArmSucceeded(), 0), (p.ProfileReady(SPEAKER, 1), 0),
                    (p.Vad(SpeechStart(0)), 0), (p.Captured(first), 1_000_000_000),
                    (p.AudioSegment(first), 1_000_000_000),
                    (p.Translation(UnitID(1), p.Success(TranslationCandidate(UnitID(1), "Hello."))), 1_100_000_000)]:
        reducer.handle(inp, at)
    header = PCMChunkHeader(UnitID(1), SPEAKER, 1, 1, 1, PCMFormat(24_000, 1), first, False)
    reducer.handle(p.PCM(header, 4_800), 1_200_000_000)
    reducer.handle(p.Captured(second), 2_000_000_000)
    reducer.handle(p.AudioSegment(second), 2_000_000_000)
    reducer.handle(p.Underrun(), 2_100_000_000)
    fallback = reducer.handle(p.Tick(), 2_200_000_000)
    assert p.RestoreOriginalVoice(second) in fallback and p.RestoreOriginalVoice(first) not in fallback


def test_line_is_budgeted_into_following_silence_not_next_line():
    line = "我们下周开会讨论这个方案的预算价格是三点五万元"
    roomy = Direct(direction=TranslationDirection.ENGLISH_TO_CHINESE)
    roomy.run(3, p.AudioSegment(SPAN))
    roomy.run(3.3, p.FollowingSilence(SPAN, 2))
    assert len(synthesized(roomy.translate(1, line, 3.4))) == 20
    tight = Direct(direction=TranslationDirection.ENGLISH_TO_CHINESE)
    tight.run(3, p.AudioSegment(SPAN))
    tight.run(3.3, p.FollowingSilence(SPAN, 0))
    assert len(synthesized(tight.translate(1, line, 3.4))) == 16


def delayed_pipeline(d, speakers=False):
    config = p.PipelineConfig(session=f"deadline-{d}")
    config.direct_audio_translation = True
    config.presentation_delay_seconds = d
    config.restores_original_voice = True
    config.original_voice_decision_deadline_seconds = p.PipelineConfig.original_voice_decision_deadline(d)
    if speakers:
        config.multi_speaker = True
        config.require_speaker_attribution = True
    reducer = p.SessionPipeline(config)
    for inp in (p.Arm(), p.ArmSucceeded(), p.ProfileReady(SPEAKER, 1)):
        reducer.handle(inp, 0)
    return reducer


def test_unit_as_long_as_the_deadline_is_translated_when_answered_in_normal_time():
    from a2a_runtime.core.audio_segment import AudioSegmentPolicy
    for d in range(4, 21):
        reducer = delayed_pipeline(float(d))
        cap = AudioSegmentPolicy.sentences(float(d)).max_samples
        line = SourceSpan(0, int(cap))
        closed = cap / 48_000
        reducer.handle(p.Captured(line), int(closed * 1e9))
        reducer.handle(p.AudioSegment(line), int(closed * 1e9))
        restored = False
        for k in range(15):
            restored |= has(reducer.handle(p.Tick(), int((closed + 0.1 * k) * 1e9)), p.RestoreOriginalVoice)
        answer = reducer.handle(p.Translation(UnitID(1), p.Success(TranslationCandidate(UnitID(1), "Hello there."))),
                                int((closed + 1.5) * 1e9))
        assert not restored, d
        assert has(answer, p.Synthesize), d


def test_long_unit_whose_translator_stalls_is_restored_once_it_stalls():
    reducer = delayed_pipeline(5.0)
    line = SourceSpan(0, 144_000)
    reducer.handle(p.Captured(line), 3_000_000_000)
    reducer.handle(p.AudioSegment(line), 3_000_000_000)
    assert not has(reducer.handle(p.Tick(), 4_900_000_000), p.RestoreOriginalVoice)
    stalled = reducer.handle(p.Tick(), 5_100_000_000)
    assert p.CancelTranslation(UnitID(1)) in stalled and p.RestoreOriginalVoice(line) in stalled
    late = reducer.handle(p.Translation(UnitID(1), p.Success(TranslationCandidate(UnitID(1), "Hello."))), 5_200_000_000)
    assert not has(late, p.Synthesize)


def test_label_arriving_in_normal_time_after_the_deadline_still_gets_the_line_translated():
    reducer = delayed_pipeline(5.0, speakers=True)
    line = SourceSpan(0, 144_000)
    reducer.handle(p.Captured(line), 3_000_000_000)
    reducer.handle(p.AudioSegment(line), 3_000_000_000)
    reducer.handle(p.Translation(UnitID(1), p.Success(TranslationCandidate(UnitID(1), "Hello there."))), 3_600_000_000)
    assert not has(reducer.handle(p.Tick(), 4_000_000_000), p.RestoreOriginalVoice)
    assert not has(reducer.handle(p.Tick(), 4_400_000_000), p.RestoreOriginalVoice)
    assert has(reducer.handle(p.Speaker(SpeakerAssign(SPEAKER, line)), 4_500_000_000), p.Synthesize)
