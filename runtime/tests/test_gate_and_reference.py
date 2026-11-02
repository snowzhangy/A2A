"""Ports of PlayoutGateTests.swift and ReferencePolicyTests.swift."""

import math

from a2a_runtime.core.playout_gate import PlayoutGate
from a2a_runtime.core.reference_policy import ReferencePolicy, Rejected
from a2a_runtime.core.types import ASRHypothesis, ASRToken, PCMChunkHeader, PCMFormat, SourceSpan, SpeakerID, UnitID

RATE = 48_000


def header(unit, epoch=1, gen=1):
    return PCMChunkHeader(UnitID(unit), SpeakerID("S1"), 1, epoch, gen, PCMFormat(24_000, 1), SourceSpan(0, 48_000), False)


def test_retired_ranges_preserve_holes_for_unfinished_units():
    gate = PlayoutGate()
    for uid in [1, 3, 5, 2, 6, 7]:
        gate.mark_finished(UnitID(uid))
        gate.forget_terminal_unit(UnitID(uid))
    for uid in [1, 2, 3, 5, 6, 7]:
        assert gate.admit(header(uid)).reason == "retired_unit"
    assert gate.admit(header(4)).is_accepted
    gate.forget_terminal_unit(UnitID(4))  # joins the two ranges
    assert gate.admit(header(4)).reason == "retired_unit"
    assert gate.retired_ranges == [(1, 7)]
    gate.advance_epoch()
    assert gate.admit(header(4, epoch=2, gen=gate.generation)).is_accepted


def test_gate_rejections():
    gate = PlayoutGate()
    assert gate.admit(header(1)).is_accepted
    gate.advance_generation()
    assert gate.admit(header(1, gen=1)).reason == "stale_generation"
    assert gate.admit(header(1, gen=2)).is_accepted
    gate.advance_epoch()
    assert gate.epoch == 2
    assert gate.admit(header(1, epoch=1, gen=gate.generation)).reason == "stale_epoch"
    gate.cancel(UnitID(3))
    gate.mark_finished(UnitID(4))
    assert gate.admit(header(3, epoch=2, gen=gate.generation)).reason == "canceled_unit"
    assert gate.admit(header(4, epoch=2, gen=gate.generation)).reason == "already_finished"
    gate.advance_epoch()
    assert gate.admit(header(3, epoch=3, gen=gate.generation)).is_accepted


def final(text, start, end, timed=True):
    span = SourceSpan(int(start * RATE), int(end * RATE))
    return ASRHypothesis(1, (ASRToken(text, span if timed else None),), True, span.end)


def seconds(s, at=0.0):
    return SourceSpan(int(at * RATE), int((at + s) * RATE))


def test_speech_span_pads_token_timing_and_needs_final_timed_results():
    policy = ReferencePolicy(SpeakerID("S1"))
    pad = policy.config.padding_samples
    assert policy.speech_span(final("我们开会。", 1, 3)) == SourceSpan(RATE - pad, 3 * RATE + pad)
    assert policy.speech_span(final("我们开会。", 1, 3, timed=False)) is None
    partial = ASRHypothesis(1, (ASRToken("我们", seconds(1)),), False, RATE)
    assert policy.speech_span(partial) is None
    assert policy.speech_span(final("好", 0, 1)).start == 0


def test_first_version_at_minimum_then_one_upgrade_at_target():
    policy = ReferencePolicy(SpeakerID("S1"))
    assert policy.offer(seconds(2), "第一句。", -20, 0) is None
    v1 = policy.offer(seconds(2, 3), "第二句。", -20, 0)
    assert v1.version == 1 and len(v1.segments) == 2 and v1.text == "第一句。 第二句。"
    assert abs(v1.seconds - 4) < 1e-9
    assert policy.offer(seconds(2, 6), "三。", -20, 0) is None
    v2 = policy.offer(seconds(2.5, 9), "四。", -20, 0)
    assert v2.version == 2 and len(v2.segments) == 4 and policy.is_complete
    assert policy.offer(seconds(2, 12), "五。", -20, 0) == Rejected("complete")


def test_rejects_quiet_clipped_short_and_empty_segments():
    policy = ReferencePolicy(SpeakerID("S1"))
    assert policy.offer(seconds(3), "  ", -20, 0) == Rejected("empty")
    assert policy.offer(seconds(0.2), "好。", -20, 0) == Rejected("too_short")
    assert policy.offer(seconds(3), "好。", -60, 0) == Rejected("quiet")
    assert policy.offer(seconds(3), "好。", -math.inf, 0) == Rejected("quiet")
    assert policy.offer(seconds(3), "好。", -20, 0.01) == Rejected("clipped")
    assert policy.accepted_seconds == 0


def test_short_dialogue_turns_accumulate():
    policy = ReferencePolicy(SpeakerID("S1"))
    for i in range(5):
        assert policy.offer(seconds(0.5, i), "Okay", -20, 0) is None
    plan = policy.offer(seconds(0.5, 5), "All right", -20, 0)
    assert plan.version == 1 and plan.seconds == 3
    assert plan.text == "Okay Okay Okay Okay Okay All right"
    h = final("好", 1, 1.4)
    assert policy.token_span(h) == SourceSpan(48_000, 67_200)
    assert policy.speech_span(h) == SourceSpan(43_200, 72_000)


def test_segment_beyond_cap_ends_acquisition_without_duplicate_version():
    policy = ReferencePolicy(SpeakerID("S1"))
    assert policy.offer(seconds(11), "很长。", -20, 0) == Rejected("too_long")
    assert policy.offer(seconds(6), "一。", -20, 0).version == 1
    assert policy.offer(seconds(5, 7), "二。", -20, 0) == Rejected("too_long")
    assert policy.is_complete


def test_upgrade_issued_when_next_segment_cannot_fit():
    policy = ReferencePolicy(SpeakerID("S1"))
    assert policy.offer(seconds(4), "一。", -20, 0) is not None
    assert policy.offer(seconds(2, 5), "二。", -20, 0) is None
    v2 = policy.offer(seconds(5, 8), "三。", -20, 0)
    assert v2.version == 2 and [s.text for s in v2.segments] == ["一。", "二。"]


def test_reset_starts_over_for_a_new_speaker():
    policy = ReferencePolicy(SpeakerID("S1"))
    policy.offer(seconds(4), "一。", -20, 0)
    policy.reset(SpeakerID("S2"))
    assert policy.accepted_seconds == 0 and not policy.is_complete
    v1 = policy.offer(seconds(3.5), "二。", -20, 0)
    assert v1.speaker == SpeakerID("S2") and v1.version == 1
