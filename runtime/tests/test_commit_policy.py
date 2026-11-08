"""Port of apple/Tests/A2ACoreTests/CommitPolicyTests.swift."""

import sys

import pytest

from a2a_runtime.core.commit_policy import (Abandoned, Commit, CommitPolicy, CommitPolicyConfig, CommitReason, Hold,
                                            RequestFinalizeOut, RevisionBehindFrontier)
from a2a_runtime.core.text import CommitLexicon, NormalizedText, char_count, graphemes, unsafe_ending
from a2a_runtime.core.types import ASRHypothesis, ASRToken, SourceSpan, UnitID

MS = 1_000_000


def hyp(text, utt=1, final=False, frontier=48_000):
    return ASRHypothesis(utt, tuple(ASRToken(c) for c in text), final, frontier)


def commits(outputs):
    return [o.unit for o in outputs if isinstance(o, Commit)]


def content_only(text):
    """Spoken content only: recognizer punctuation carries no audio."""
    return "".join(k for k in NormalizedText.of(text).keys if k not in CommitLexicon.punctuation)


class DictionarySegmenter:
    """A segmenter over a fixed vocabulary, standing in for the host's system tokenizer: no
    cut may fall strictly inside one of the listed words."""

    def __init__(self, words):
        self.words = words

    def splits_word(self, text, offset):
        chars = graphemes(text)
        for word in self.words:
            w = graphemes(word)
            if len(w) <= 1 or not 0 < offset < len(chars):
                continue
            for start in range(0, len(chars) - len(w) + 1):
                if chars[start:start + len(w)] == w and start < offset < start + len(w):
                    return True
        return False


def test_only_but_compound_does_not_become_two_unrelated_clauses():
    for streaming in (False, True):
        p = CommitPolicy(CommitPolicyConfig(streaming_clauses=streaming))
        text = "他们只不过多看了几眼"
        p.process(hyp(text), 0)
        assert commits(p.process(hyp(text), 100 * MS)) == []
        assert commits(p.process(hyp(text), 1_000 * MS)) == []
        final = commits(p.process(hyp(text, final=True), 1_100 * MS))
        assert [u.text for u in final] == [text]


def test_late_punctuation_never_speaks_committed_content_twice():
    p = CommitPolicy(CommitPolicyConfig(max_unit_source_seconds=3))
    # Partials arrive unpunctuated, and the final result adds a period inside content that was
    # already committed. Nothing may be spoken twice or dropped.
    partial = "我们下周开会讨论这个方案"
    p.process(hyp(partial, frontier=4 * 48_000), 0)
    first = commits(p.process(hyp(partial, frontier=4 * 48_000), 100 * MS))
    assert first[0].reason == CommitReason.LENGTH_CLAUSE
    rest = commits(p.process(hyp("我们下周开会。讨论这个方案。", final=True, frontier=5 * 48_000), 200 * MS))
    assert content_only("".join(u.text for u in first + rest)) == content_only(partial)


def test_inserted_comma_does_not_shift_the_frontier_without_the_budget():
    p = CommitPolicy(CommitPolicyConfig(max_unit_keys=8))
    partial = "我们下周开会讨论这个方案"
    p.process(hyp(partial), 0)
    first = commits(p.process(hyp(partial), 100 * MS))
    assert [u.text for u in first] == ["我们下周开会讨论"]
    assert first[0].reason == CommitReason.FORCED_LENGTH
    rest = commits(p.process(hyp("我们下周开会，讨论这个方案。", final=True), 200 * MS))
    # Before re-alignment this committed "论这个方案。": the inserted comma moved the frontier
    # back over a key that had already been spoken.
    assert [u.text for u in rest] == ["这个方案。"]
    assert content_only("".join(u.text for u in first + rest)) == content_only(partial)


def test_long_unfinalized_utterance_closes_at_the_source_time_budget():
    p = CommitPolicy()
    text = "我们今天下午三点在会议室讨论下个季度的预算安排"
    p.process(hyp(text, frontier=5 * 48_000), 0)
    out = commits(p.process(hyp(text, frontier=5 * 48_000), 100 * MS))
    assert len(out) == 1 and out[0].reason == CommitReason.LENGTH_CLAUSE
    # Cut as late as the safety rules allow, leaving a splittable tail.
    assert out[0].text == "我们今天下午三点在会议室讨论下个季度的预"
    assert out[0].span == SourceSpan(0, 5 * 48_000) and out[0].timing_estimated
    # The tail waits for its own evidence, and its estimated span starts where the length
    # clause ended, so coverage never double-counts source.
    rest = commits(p.process(hyp(text, final=True, frontier=6 * 48_000), 200 * MS))
    assert [u.text for u in rest] == ["算安排"]
    assert rest[0].reason == CommitReason.FINALIZED
    assert rest[0].span == SourceSpan(5 * 48_000, 6 * 48_000)


def test_length_budget_waits_for_the_key_floor():
    p = CommitPolicy()
    text = "我们今天下午三点开会"
    p.process(hyp(text, frontier=10 * 48_000), 0)
    # 10 keys is below the floor: a frontier that runs ahead of the text (VAD held open over
    # music) must not turn a short clause into a committed unit.
    assert commits(p.process(hyp(text, frontier=10 * 48_000), 100 * MS)) == []


def test_length_budget_cuts_earlier_rather_than_end_on_a_number():
    p = CommitPolicy()
    text = "我们讨论一二三四五六七八九十"
    p.process(hyp(text, frontier=6 * 48_000), 0)
    out = commits(p.process(hyp(text, frontier=6 * 48_000), 100 * MS))
    assert [u.text for u in out] == ["我们讨论"] and out[0].reason == CommitReason.LENGTH_CLAUSE


def test_length_budget_holds_when_no_boundary_is_safe():
    p = CommitPolicy()
    text = "一二三四五六七八九十一二"
    p.process(hyp(text, frontier=6 * 48_000), 0)
    assert commits(p.process(hyp(text, frontier=6 * 48_000), 100 * MS)) == []
    # Still reported as waiting, with the same reason the reconsider window uses.
    out = p.process(hyp(text, frontier=6 * 48_000), 1_000 * MS)
    assert Hold(1, "number", 12, 900 * MS) in out


def test_length_budget_disabled_waits_for_finalization():
    p = CommitPolicy(CommitPolicyConfig(max_unit_source_seconds=0))
    text = "我们今天下午三点在会议室讨论下个季度的预算安排"
    p.process(hyp(text, frontier=8 * 48_000), 0)
    assert commits(p.process(hyp(text, frontier=8 * 48_000), 100 * MS)) == []
    assert len(commits(p.process(hyp(text, final=True, frontier=8 * 48_000), 200 * MS))) == 1


def test_length_budget_respects_word_boundaries_when_configured():
    p = CommitPolicy(CommitPolicyConfig(prefer_word_boundaries=True))
    text = "we will discuss the budget tomorrow afternoon"
    p.process(hyp(text, frontier=6 * 48_000), 0)
    out = commits(p.process(hyp(text, frontier=6 * 48_000), 100 * MS))
    assert out[0].text == "we will discuss the budget tomorrow"
    assert out[0].reason == CommitReason.LENGTH_CLAUSE


def test_length_budget_never_cuts_inside_a_word_the_segmenter_knows():
    # Per-character keys hide Mandarin words: the cut after 预 above would speak half of
    # 度的预算. Only the host's segmenter can see it, so the rule obeys it and cuts earlier.
    p = CommitPolicy(word_segmenter=DictionarySegmenter(["度的预算"]))
    text = "我们今天下午三点在会议室讨论下个季度的预算安排"
    p.process(hyp(text, frontier=5 * 48_000), 0)
    out = commits(p.process(hyp(text, frontier=5 * 48_000), 100 * MS))
    assert out[0].reason == CommitReason.LENGTH_CLAUSE
    assert out[0].text == "我们今天下午三点在会议室讨论下个季"
    # The rest of the word is not lost: the tail carries it to the next unit.
    rest = commits(p.process(hyp(text, final=True, frontier=6 * 48_000), 200 * MS))
    assert [u.text for u in rest] == ["度的预算安排"]
    assert content_only("".join(u.text for u in out + rest)) == content_only(text)


def test_successive_length_clauses_stay_disjoint():
    p = CommitPolicy()
    p.speech_started(48_000)
    first = "我们今天下午三点在会议室讨论下个季度的预算安排问题"
    p.process(hyp(first, frontier=5 * 48_000), 0)
    a = commits(p.process(hyp(first, frontier=5 * 48_000), 100 * MS))
    # The second half of the utterance keeps growing, and a second budget cut follows.
    second = first + "并且需要各位提前准备材料"
    b = commits(p.process(hyp(second, frontier=10 * 48_000), 200 * MS))
    c = commits(p.process(hyp(second, frontier=10 * 48_000), 300 * MS))
    units = a + b + c
    assert len(units) >= 2
    assert len({u.id for u in units}) == len(units)
    for x, y in zip(units, units[1:]):
        assert x.span.end <= y.span.start
    assert units[0].span.start == 48_000


def test_single_partial_is_not_stable():
    assert commits(CommitPolicy().process(hyp("我们下周开会。"), 0)) == []


def test_duplicate_hypotheses_commit_at_sentence_end_once():
    p = CommitPolicy()
    p.process(hyp("我们下周开会。"), 0)
    first = commits(p.process(hyp("我们下周开会。"), 100 * MS))
    assert [u.text for u in first] == ["我们下周开会。"]
    assert first[0].reason == CommitReason.SENTENCE_END and first[0].id == UnitID(1)
    assert commits(p.process(hyp("我们下周开会。"), 200 * MS)) == []
    assert p.frontier == 7


def test_only_agreed_prefix_beyond_frontier_commits():
    p = CommitPolicy()
    p.process(hyp("今天天气很好。明天"), 0)
    assert [u.text for u in commits(p.process(hyp("今天天气很好。明天下雨"), 100 * MS))] == ["今天天气很好。"]
    assert commits(p.process(hyp("今天天气很好。明天会下雨。"), 200 * MS)) == []
    nxt = commits(p.process(hyp("今天天气很好。明天会下雨。"), 300 * MS))
    assert [u.text for u in nxt] == ["明天会下雨。"] and nxt[0].id == UnitID(2)


def test_normalization_ignores_width_case_and_whitespace():
    p = CommitPolicy()
    p.process(hyp("我们用 Zoom 开会。"), 0)
    out = commits(p.process(hyp("我们用zoom开会."), 100 * MS))
    assert len(out) == 1 and out[0].text == "我们用zoom开会."


def test_vad_end_requests_finalize_but_does_not_commit_partial():
    p = CommitPolicy()
    p.process(hyp("好的"), 0)
    p.process(hyp("好的"), 100 * MS)
    assert p.speech_ended(96_000) == [RequestFinalizeOut(1)]
    assert p.frontier == 0


def test_finalized_short_acknowledgment_commits_immediately():
    out = commits(CommitPolicy().process(hyp("好的", final=True), 0))
    assert [u.text for u in out] == ["好的"]
    assert out[0].reason == CommitReason.FINALIZED and out[0].semantic_wait_ns == 0


def test_comma_needs_minimum_length():
    p = CommitPolicy()
    p.process(hyp("对，我们明天"), 0)
    assert commits(p.process(hyp("对，我们明天"), 100 * MS)) == []
    p.process(hyp("对，我们明天见面，然后"), 200 * MS)
    out = commits(p.process(hyp("对，我们明天见面，然后"), 300 * MS))
    assert [u.text for u in out] == ["对，我们明天见面，"] and out[0].reason == CommitReason.CLAUSE_COMMA


def test_negation_at_comma_holds_and_reports_after_window():
    p = CommitPolicy()
    p.process(hyp("这个方案我们不，"), 0)
    assert commits(p.process(hyp("这个方案我们不，"), 100 * MS)) == []
    late = p.process(hyp("这个方案我们不，"), 1_000 * MS)
    assert commits(late) == []
    assert any(isinstance(o, Hold) and o.reason == "negation" for o in late)
    p.process(hyp("这个方案我们不，同意。"), 1_100 * MS)
    out = commits(p.process(hyp("这个方案我们不，同意。"), 1_200 * MS))
    assert [u.text for u in out] == ["这个方案我们不，同意。"]
    assert out[0].semantic_wait_ns >= 1_100 * MS


def test_unfinished_number_and_decimal_are_not_split():
    p = CommitPolicy()
    p.process(hyp("价格是3."), 0)
    assert commits(p.process(hyp("价格是3."), 100 * MS)) == []
    p.process(hyp("价格是3.5万，"), 200 * MS)
    assert [u.text for u in commits(p.process(hyp("价格是3.5万，"), 300 * MS))] == ["价格是3.5万，"]
    q = CommitPolicy()
    q.process(hyp("会议定在三月二十，"), 0)
    assert commits(q.process(hyp("会议定在三月二十，"), 100 * MS)) == []


def test_conjunction_split_only_after_reconsider_window():
    p = CommitPolicy()
    text = "我们已经完成了测试但是还要检查"
    p.process(hyp(text), 0)
    assert commits(p.process(hyp(text), 100 * MS)) == []
    out = commits(p.process(hyp(text), 1_000 * MS))
    assert [u.text for u in out] == ["我们已经完成了测试"] and out[0].reason == CommitReason.CONJUNCTION


def test_forced_length_cap_bounds_waiting():
    p = CommitPolicy(CommitPolicyConfig(max_unit_keys=10))
    text = "一二三四五六七八九十甲乙丙丁"
    p.process(hyp(text), 0)
    out = commits(p.process(hyp(text), 100 * MS))
    assert out[0].reason == CommitReason.FORCED_LENGTH and char_count(out[0].text) <= 10


def test_revision_behind_frontier_is_reported_once():
    p = CommitPolicy()
    p.process(hyp("我们开会。"), 0)
    p.process(hyp("我们开会。"), 100 * MS)
    a = p.process(hyp("你们开会。然后"), 200 * MS)
    b = p.process(hyp("你们开会。然后"), 300 * MS)
    assert RevisionBehindFrontier(1, 5) in a and RevisionBehindFrontier(1, 5) not in b


def test_token_timing_is_used_when_every_token_has_it():
    tokens = (ASRToken("好的", SourceSpan(1_000, 20_000)), ASRToken("。", SourceSpan(20_000, 21_000)))
    unit = commits(CommitPolicy().process(ASRHypothesis(1, tokens, True, 30_000), 0))[0]
    assert unit.span == SourceSpan(1_000, 21_000) and not unit.timing_estimated


def test_missing_timing_uses_conservative_onset_to_frontier():
    p = CommitPolicy()
    p.speech_started(4_800)
    unit = commits(p.process(hyp("好的", final=True, frontier=60_000), 0))[0]
    assert unit.span == SourceSpan(4_800, 60_000) and unit.timing_estimated


def test_new_utterance_resets_comparison_and_reports_abandoned_text():
    p = CommitPolicy()
    p.process(hyp("我们", utt=1), 0)
    out = p.process(hyp("你好", utt=2), 100 * MS)
    assert Abandoned(1, "我们") in out and p.frontier == 0


def test_unsafe_ending_classifier():
    assert unsafe_ending("我们不") == "negation"
    assert unsafe_ending("We do not") == "negation"
    assert unsafe_ending("一共有三百二") == "number"
    assert unsafe_ending("价格是3.5万") is None
    assert unsafe_ending("因为") == "dangling"
    assert unsafe_ending("我们下周开会") is None
    assert unsafe_ending("Nothing") is None


def test_timed_new_utterance_advances_onset_without_vad_silence():
    p = CommitPolicy()
    p.speech_started(0)
    p.process(ASRHypothesis(1, (ASRToken("第一句。", SourceSpan(0, 48_000)),), True, 48_000), 1_000 * MS)
    p.process(ASRHypothesis(2, (ASRToken("新的", SourceSpan(480_000, 528_000)),), False, 528_000), 11_000 * MS)
    assert p.uncommitted_start == 480_000 and p.frontier == 0


def test_streaming_clause_splits_at_a_timed_pause():
    # Not in the Swift suite: exercises the prosodic-pause path faster-whisper word timings use.
    p = CommitPolicy(CommitPolicyConfig(streaming_clauses=True))
    tokens = (ASRToken("我们下周", SourceSpan(0, 48_000)), ASRToken("开会", SourceSpan(48_000, 72_000)),
              ASRToken("然后", SourceSpan(100_000, 120_000)), ASRToken("讨论", SourceSpan(120_000, 140_000)))
    h = ASRHypothesis(1, tokens, False, 140_000)
    p.process(h, 0)
    out = commits(p.process(h, 100 * MS))
    assert [u.text for u in out] == ["我们下周开会"] and out[0].reason == CommitReason.PROSODIC_PAUSE
    assert out[0].span == SourceSpan(0, 72_000) and not out[0].timing_estimated


@pytest.mark.skipif(sys.platform != "win32", reason="Windows.Data.Text segmenter")
def test_the_windows_segmenter_keeps_a_length_cut_off_han_words():
    from a2a_runtime.models.word_segmenter import WindowsWordSegmenter

    # The same line as the dictionary-segmenter test, with the system's own word data: the cut
    # backs off 预算 and the dangling 的 and lands after 季度, a whole word.
    p = CommitPolicy(word_segmenter=WindowsWordSegmenter("zh-Hans"))
    text = "我们今天下午三点在会议室讨论下个季度的预算安排"
    p.process(hyp(text, frontier=5 * 48_000), 0)
    out = commits(p.process(hyp(text, frontier=5 * 48_000), 100 * MS))
    assert out[0].reason == CommitReason.LENGTH_CLAUSE
    assert out[0].text == "我们今天下午三点在会议室讨论下个季度"
    rest = commits(p.process(hyp(text, final=True, frontier=6 * 48_000), 200 * MS))
    assert content_only("".join(u.text for u in out + rest)) == content_only(text)
