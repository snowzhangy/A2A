"""Port of apple/Tests/A2ACoreTests/CommitPolicyTests.swift."""

from a2a_runtime.core.commit_policy import (Abandoned, Commit, CommitPolicy, CommitPolicyConfig, CommitReason, Hold,
                                            RequestFinalizeOut, RevisionBehindFrontier)
from a2a_runtime.core.text import char_count, unsafe_ending
from a2a_runtime.core.types import ASRHypothesis, ASRToken, SourceSpan, UnitID

MS = 1_000_000


def hyp(text, utt=1, final=False, frontier=48_000):
    return ASRHypothesis(utt, tuple(ASRToken(c) for c in text), final, frontier)


def commits(outputs):
    return [o.unit for o in outputs if isinstance(o, Commit)]


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
