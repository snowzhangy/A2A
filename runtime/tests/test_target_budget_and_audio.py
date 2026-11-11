"""Ports of TargetBudgetTests.swift and AudioSegmentTests.swift (incl. AudioStreamFeedTests).

Fitting a translation into the time its source audio took: the spoken budget, the deletion
that respects it, and the silence verdict for answers that carry no content. Direct audio
translation: bounded speech spans and what a streaming translator may hear before they close.
"""

from __future__ import annotations

from a2a_runtime.core import pipeline as p
from a2a_runtime.core.audio_segment import (AdaptiveSpeechThreshold, Append, AudioSegmentPolicy, AudioStreamFeed,
                                            Close, Discard)
from a2a_runtime.core.direction import TranslationDirection
from a2a_runtime.core.reference_policy import ReferencePlan, ReferencePolicy, ReferencePolicyConfig, Rejected
from a2a_runtime.core.target_budget import TargetShortener
from a2a_runtime.core.text import char_count, graphemes
from a2a_runtime.core.trace import TraceKind
from a2a_runtime.core.types import (SourceSpan, SpeakerAssign, SpeakerID, SpeechEnd, SpeechStart, TranslationCandidate,
                                    UnitID)
from a2a_runtime.core.validator import Rejected as VerdictRejected
from a2a_runtime.core.validator import TranslationValidator

RATE = 48_000


def span(start: float, end: float) -> SourceSpan:
    return SourceSpan(int(start * RATE), int(end * RATE))


class FixedSegmenter:
    def splits_word(self, text, offset):
        return offset % 3 != 0


class AudioSession:
    """A 3 s direct-audio session: the shape the app runs when a source piece is sent whole."""

    def __init__(self, direction=TranslationDirection.CHINESE_TO_ENGLISH, target_segmenter=None, tune=None):
        config = p.PipelineConfig(session="budget", direction=direction)
        config.direct_audio_translation = True
        config.original_audio_enabled = True  # so a rejection would ask for the original bus
        if tune:
            tune(config)
        self.p = p.SessionPipeline(config, target_word_segmenter=target_segmenter)
        self.effects: list = []
        self.p.handle(p.Arm(), 0)
        self.p.handle(p.ArmSucceeded(), 0)
        self.p.handle(p.ProfileReady(SpeakerID("S1"), 1), 0)

    def piece(self, seconds: float = 3, t: float = 0) -> list:
        out = self.p.handle(p.AudioSegment(span(0, seconds)), int(t * 1e9))
        self.effects += out
        return out

    def translate(self, text: str, t: float, unit: int = 1) -> list:
        out = self.p.handle(p.Translation(UnitID(unit), p.Success(TranslationCandidate(UnitID(unit), text))), int(t * 1e9))
        self.effects += out
        return out

    def synthesized_text(self) -> str | None:
        return next((e.job.text for e in self.effects if isinstance(e, p.Synthesize)), None)

    def traces(self, kind: TraceKind):
        return [e.event for e in self.effects if isinstance(e, p.TraceOut) and e.event.kind == kind]

    def disposition(self):
        for e in self.traces(TraceKind.SOURCE_DISPOSITION):
            if e.value("disposition") is not None and e.value("reason") is not None:
                return e.value("disposition"), e.value("reason")
        return None

    def tick(self, t: float) -> None:
        self.effects += self.p.handle(p.Tick(), int(t * 1e9))


# MARK: Target budget


def test_budget_comes_from_the_spoken_rate_of_the_target_language():
    zh = p.PipelineConfig(session="zh")
    zh.direct_audio_translation = True
    assert zh.validator.spoken_characters_per_second == 15
    assert zh.validator.spoken_budget_characters(3) == 60
    en = p.PipelineConfig(session="en", direction=TranslationDirection.ENGLISH_TO_CHINESE)
    # Mandarin targets are budgeted in Han characters, not Latin ones.
    assert en.validator.spoken_characters_per_second == 5
    assert en.validator.spoken_budget_characters(3) == 20


def test_shortening_keeps_whole_clauses_then_whole_words():
    text = "The good news is that the price is final, and we can start on Monday morning if the team agrees."
    cut = TargetShortener.shorten(text, 48)
    assert cut == "The good news is that the price is final" and len(cut) <= 48
    # The only clause boundary sits at character 4: a word cut deeper into the line keeps the
    # message instead of dropping a whole sentence to a fragment.
    assert TargetShortener.shorten("Yes, we can start on Monday morning as planned.", 40) == \
        "Yes, we can start on Monday morning"


def test_an_early_clause_ending_loses_to_a_word_cut_that_fills_the_slot():
    text = "The new price is final now, so we can sign the contract with the client before Friday."
    assert TargetShortener.shorten(text, 60) == "The new price is final now, so we can sign the contract"
    # The old 40% preference is still available to a caller that wants it.
    assert TargetShortener.shorten(text, 60, prefer_clause_fraction=0.4) == "The new price is final now"


def test_shortening_refuses_to_speak_a_misleading_fragment():
    # One 90-character clause with a single comma at character 4: no safe prefix is left.
    assert TargetShortener.shorten("Yes, " + "a" * 91, 60) is None
    assert TargetShortener.shorten("fits inside the budget", 40) == "fits inside the budget"
    assert TargetShortener.shorten("nothing", 0) is None


def test_shortening_never_ends_on_a_dangling_function_word():
    cut = TargetShortener.shorten("Please tell the client that we will set the price of the extra seats today", 56)
    assert cut is not None and cut.endswith("price") and not cut.endswith("of")


def test_han_targets_cut_at_character_boundaries_and_avoid_words_when_a_segmenter_is_present():
    text = "我们下周开会讨论这个方案的成本和价格变化"
    assert char_count(TargetShortener.shorten(text, 14)) == 14
    # The injected segmenter pushes the cut back to the nearest boundary it accepts.
    assert TargetShortener.shorten(text, 14, word_segmenter=FixedSegmenter()) == "我们下周开会讨论这个方案"


def test_english_to_mandarin_shortening_uses_the_target_segmenter():
    line = "我们下周开会讨论这个方案的预算价格是三点五万元"
    plain = AudioSession(direction=TranslationDirection.ENGLISH_TO_CHINESE)
    plain.piece(t=1)
    plain.translate(line, 1.4)
    # 3 s of speech plus 1 s grace at the measured 5 Han characters a second.
    assert char_count(plain.synthesized_text()) == 20
    segmented = AudioSession(direction=TranslationDirection.ENGLISH_TO_CHINESE, target_segmenter=FixedSegmenter())
    segmented.piece(t=1)
    segmented.translate(line, 1.4)
    # Boundary 18 ends on 是, a dangling function word, so one more character goes.
    assert segmented.synthesized_text() == "".join(graphemes(line)[:17])
    # A cut that would land on a dangling function word gives one character back.
    assert TargetShortener.shorten("我们下周开会讨论这个方案的成本和价格", 16) == "我们下周开会讨论这个方案的成本"


def test_an_over_long_target_is_spoken_shorter_rather_than_dropped():
    s = AudioSession()
    s.piece(t=1)
    line = "The good news is that the price is final, and we can start on Monday morning."
    effects = s.translate(line, 1.4)
    spoken = s.synthesized_text()
    assert spoken is not None and spoken != line and len(spoken) <= 60
    assert any(isinstance(e, p.Synthesize) and e.job.text == spoken for e in effects)
    target = s.traces(TraceKind.COMMIT_TARGET)[0]
    assert target.value("shortened") is True and target.value("mt_chars") == len(line)
    assert s.disposition() is None or s.disposition()[0] != "original_only"  # the line still plays


def test_an_empty_answer_is_silence_rather_than_a_failed_translation():
    s = AudioSession()
    s.piece(t=1)
    effects = s.translate("   ", 1.4)
    assert s.disposition() == ("silence", "empty")
    assert s.traces(TraceKind.ERROR) == []
    # No fallback: nothing was skipped, so the original bus must not be asked for audio.
    assert s.traces(TraceKind.FALLBACK_ENTER) == []
    assert not any(isinstance(e, p.RampGains) for e in effects)
    # The unit is terminal: it never synthesizes and holds no debt.
    assert s.synthesized_text() is None
    assert s.traces(TraceKind.COMMIT_TARGET) == []
    s.tick(2)
    assert s.traces(TraceKind.FALLBACK_ENTER) == []


def test_a_filler_answer_from_music_or_a_pause_is_silence_too():
    s = AudioSession()
    s.piece(t=1)
    s.translate("Yeah.", 1.4)
    assert s.disposition() == ("silence", "filler")
    assert s.traces(TraceKind.ERROR) == []
    assert s.synthesized_text() is None
    # mt.result stays ok: the translator answered, there was simply nothing to say.
    result = s.traces(TraceKind.MT_RESULT)[0]
    assert result.value("ok") is True and result.value("silence") == "filler"


def test_silenced_units_do_not_become_the_translation_context():
    s = AudioSession()
    s.piece(t=1)
    s.translate("Oh.", 1.4)
    # The next piece asks for context; the filler must not have entered it.
    second = s.p.handle(p.AudioSegment(span(3, 6)), 2_000_000_000)
    requests = [e.request for e in second if isinstance(e, p.Translate)]
    assert len(requests) == 1 and requests[0].context == ()


def test_a_line_that_cannot_be_cut_stays_a_gap_rather_than_silence():
    def strict_tune(config):
        config.validator.shortens_over_budget_targets = False

    line = ("The good news is that the price is final, and we can start on Monday morning "
            "if the team agrees that the schedule works for everyone involved today.")
    strict = AudioSession(tune=strict_tune)
    strict.piece(t=1)
    strict.translate(line, 1.4)
    assert strict.disposition() == ("original_only", "too_long")
    # And with shortening on, the very same line is spoken instead of skipped.
    s = AudioSession()
    s.piece(t=1)
    s.translate(line, 1.4)
    assert s.synthesized_text() == "The good news is that the price is final, and we can start"
    assert s.disposition() is None


# MARK: Audio segments


def test_cuts_continuous_speech_and_closes_on_silence_without_overlap():
    q = AudioSegmentPolicy(max_seconds=3, min_seconds=0.25)
    assert q.process([SpeechStart(4_800)], 48_000) == []
    assert q.process([], 148_800) == [SourceSpan(4_800, 148_800)]
    assert q.process([SpeechEnd(196_800)], 211_200) == [SourceSpan(148_800, 196_800)]
    assert q.process([], 400_000) == []


def test_sentence_units_grow_with_picture_delay_within_bounds():
    assert AudioSegmentPolicy.sentences(8).max_samples == 4.5 * RATE
    low_latency = AudioSegmentPolicy.sentences(3)
    assert low_latency.max_samples == 86_400
    assert low_latency.dip_window_samples == 19_200
    assert low_latency.stream_limit_samples == 67_200
    assert AudioSegmentPolicy.sentences(4).max_samples == 134_400
    assert AudioSegmentPolicy.sentences(20).max_samples == 6 * RATE
    policy = AudioSegmentPolicy.sentences(8)
    assert policy.stream_limit_samples == policy.max_samples - RATE
    assert AudioSegmentPolicy().stream_limit_samples == AudioSegmentPolicy().max_samples


def test_three_second_picture_delay_cuts_continuous_speech_without_losing_audio():
    policy = AudioSegmentPolicy.sentences(3)
    policy.process([SpeechStart(0)], 0)
    assert policy.process([], 86_399) == []
    assert policy.process([], 86_400) == [SourceSpan(0, 86_400)]
    assert policy.process([SpeechEnd(110_400)], 120_000) == [SourceSpan(86_400, 110_400)]


def test_long_sentence_cut_uses_quietest_frame_before_cap():
    policy = AudioSegmentPolicy(max_seconds=5, min_seconds=0.25, dip_window_seconds=1)
    frame = 960
    closed = []
    at = 0
    policy.process([SpeechStart(0)], 0)
    while not closed and at < 6 * RATE:
        current = SourceSpan(at, at + frame)
        db = -50 if current.start == 211_200 else (-70 if current.start == 96_000 else -20)
        closed = policy.process([], current.end, (current, db))
        at += frame
    assert closed == [SourceSpan(0, 211_680)]
    assert policy.open_start == 211_680 and policy.cap_closes == 1 and policy.pause_closes == 0
    assert policy.process([SpeechEnd(300_000)], 310_000) == [SourceSpan(211_680, 300_000)]
    assert policy.pause_closes == 1


def test_cap_without_frame_levels_is_the_maximum():
    policy = AudioSegmentPolicy(max_seconds=5, min_seconds=0.25, dip_window_seconds=1)
    policy.process([SpeechStart(0)], 0)
    assert policy.process([], 240_000) == [SourceSpan(0, 240_000)]


def feed_threshold(threshold, levels, at):
    for db in levels:
        result = threshold.update(SourceSpan(at, at + 960), db)
        at += 960
    return result, at


def test_adaptive_threshold_finds_pauses_over_residue():
    threshold = AdaptiveSpeechThreshold(-45)
    at = 0
    for _ in range(6):
        value, at = feed_threshold(threshold, [-20] * 50 + [-36] * 25, at)
    assert -36 < value < -20 and abs(value - -28) <= 1


def test_adaptive_threshold_keeps_floor_for_quiet_or_flat_signals():
    threshold = AdaptiveSpeechThreshold(-45)
    value, _ = feed_threshold(threshold, [-25] * 100 + [-120] * 50, 0)
    assert value == -45
    flat = AdaptiveSpeechThreshold(-45)
    value, at = feed_threshold(flat, [-33] * 400, 0)
    assert value == -45
    value, _ = feed_threshold(flat, [-float("inf"), float("nan")], at)
    assert value == -45


def test_adaptive_threshold_only_counts_recent_window():
    threshold = AdaptiveSpeechThreshold(-45, window_seconds=1)
    _, at = feed_threshold(threshold, [-10] * 200, 0)
    value, _ = feed_threshold(threshold, [-20] * 34 + [-36] * 16, at)
    assert abs(value - -28) <= 1


def test_direct_audio_uses_source_time_for_validation_and_never_requires_transcript():
    config = p.PipelineConfig(session="audio")
    config.direct_audio_translation = True
    pl = p.SessionPipeline(config)
    pl.handle(p.Arm(), 0)
    pl.handle(p.ArmSucceeded(), 0)
    pl.handle(p.ProfileReady(SpeakerID("S1"), 1), 0)
    whole = SourceSpan(0, 144_000)
    effects = pl.handle(p.AudioSegment(whole), 3_000_000_000)
    assert any(isinstance(e, p.Translate) and e.request.source_span == whole and e.request.uncommitted_source == ""
               for e in effects)
    target = "We will meet next week to talk about the price."  # 47 characters: inside 3 s of speech
    translated = pl.handle(p.Translation(UnitID(1), p.Success(TranslationCandidate(UnitID(1), target))), 3_100_000_000)
    assert any(isinstance(e, p.Synthesize) and e.job.text == target for e in translated)
    assert not any(isinstance(e, p.Translate) for e in pl.handle(p.AudioSegment(whole), 3_200_000_000))
    validator = TranslationValidator(shortens_over_budget_targets=False)  # the hard cap, on its own
    assert validator.evaluate(TranslationCandidate(UnitID(2), "a" * 106), "", None,
                              source_duration_seconds=3) == VerdictRejected("too_long")


def test_audio_only_reference_explicitly_allows_missing_transcript():
    whole = SourceSpan(0, 144_000)
    normal = ReferencePolicy(SpeakerID("S1"))
    assert normal.offer(whole, "", -20, 0) == Rejected("empty")
    audio = ReferencePolicy(SpeakerID("S1"), ReferencePolicyConfig(allow_empty_text=True))
    plan = audio.offer(whole, "", -20, 0)
    assert isinstance(plan, ReferencePlan) and plan.seconds == 3 and plan.text == ""


def test_direct_audio_waits_for_the_attributed_speaker_profile():
    config = p.PipelineConfig(session="speakers")
    config.direct_audio_translation = True
    config.multi_speaker = True
    config.require_speaker_attribution = True
    pl = p.SessionPipeline(config)
    pl.handle(p.Arm(), 0)
    pl.handle(p.ArmSucceeded(), 0)
    pl.handle(p.ProfileReady(SpeakerID("S1"), 1), 0)
    whole = SourceSpan(0, 144_000)
    pl.handle(p.AudioSegment(whole), 3_000_000_000)
    result = pl.handle(p.Translation(UnitID(1), p.Success(TranslationCandidate(UnitID(1), "Hello there."))), 3_100_000_000)
    assert not any(isinstance(e, p.Synthesize) for e in result)
    labeled = pl.handle(p.Speaker(SpeakerAssign(SpeakerID("S2"), whole)), 3_200_000_000)
    assert not any(isinstance(e, p.Synthesize) for e in labeled)
    ready = pl.handle(p.ProfileReady(SpeakerID("S2"), 1), 3_300_000_000)
    assert any(isinstance(e, p.Synthesize) and e.job.speaker == SpeakerID("S2") for e in ready)


MAX = 3 * 48_000


def test_streams_voiced_audio_ahead_and_closes_each_span_with_its_rest():
    feed = AudioStreamFeed(MAX, min_append_seconds=0.1)
    # Speech starts: only audio up to the last voiced sample may go, in 100 ms steps or more.
    assert feed.update([], 4_800, 7_200) == []
    assert feed.update([], 4_800, 52_800) == [Append(SourceSpan(4_800, 52_800))]
    # A pause inside speech stops the stream at the last voiced sample.
    assert feed.update([], 4_800, 52_800) == []
    assert feed.update([], 4_800, 148_800 + 9_600) == [Append(SourceSpan(52_800, 148_800))]  # never past the cut
    # The cut closes the first span; the next one continues from it.
    first = SourceSpan(4_800, 148_800)
    assert feed.update([first], 148_800, 158_400) == [Close(first), Append(SourceSpan(148_800, 158_400))]
    # The speech end closes it with the audio that was not voiced yet when last streamed.
    second = SourceSpan(148_800, 170_000)
    assert feed.update([second], None, None) == [Append(SourceSpan(158_400, 170_000)), Close(second)]
    assert feed.update([], None, None) == []


def test_a_dropped_span_is_discarded_and_an_unstreamed_span_is_sent_whole():
    feed = AudioStreamFeed(MAX, min_append_seconds=0.1)
    assert feed.update([], 0, 9_600) == [Append(SourceSpan(0, 9_600))]
    # Too short for the segmenter: no span closes.
    assert feed.update([], None, None) == [Discard()]
    # A new speech start replaces a span that never closed.
    feed.update([], 48_000, 57_600)
    assert feed.update([], 96_000, 105_600) == [Discard(), Append(SourceSpan(96_000, 105_600))]
    # A span that opened and closed within one step goes out whole.
    fresh = AudioStreamFeed(MAX)
    whole = SourceSpan(0, 24_000)
    assert fresh.update([whole], None, None) == [Append(whole), Close(whole)]
