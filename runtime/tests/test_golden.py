"""Cross-implementation parity: the Swift golden scenarios, replayed through the Python port,
must reproduce apple/Tests/A2ACoreTests/Golden/*.jsonl byte for byte (ADR-0007).

A change to the Swift reducer that regenerates goldens fails here until this port follows.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from a2a_runtime import REPO_ROOT
from a2a_runtime.core import pipeline as p
from a2a_runtime.core.state_machine import acquiring_voice, translating
from a2a_runtime.core.types import SourceSpan, SpeechEnd, SpeechStart, UnitID
from scenario import S1, Scenario, success

GOLDEN = REPO_ROOT / "apple" / "Tests" / "A2ACoreTests" / "Golden"


def check_golden(name: str, lines: list[str]) -> None:
    expected = (GOLDEN / f"{name}.jsonl").read_bytes().decode("utf-8")
    actual = "\n".join(lines) + "\n"
    if expected != actual:
        e, a = expected.split("\n"), actual.split("\n")
        i = next((k for k in range(min(len(e), len(a))) if e[k] != a[k]), min(len(e), len(a)))
        pytest.fail(f"Golden {name} differs at line {i + 1}:\n  expected: {e[i] if i < len(e) else '<end>'}\n"
                    f"  actual:   {a[i] if i < len(a) else '<end>'}")


def test_golden_files_exist() -> None:
    assert sorted(f.stem for f in Path(GOLDEN).glob("*.jsonl")) == [
        "acquisition_no_profile", "basic_unit", "discontinuity", "fallback_recovery", "validation"]


def test_basic_unit() -> None:
    """Prepared profile, one sentence, translated, synthesized, played."""
    s = Scenario()
    s.armed()
    s.at(0, p.Captured(SourceSpan(0, 4_800)))
    s.at(100, p.Vad(SpeechStart(4_800)))
    s.partial(400, "我们下周")
    s.partial(700, "我们下周开会。")
    s.partial(900, "我们下周开会。")
    s.at(1_000, success(1, "We will meet next week."))
    s.at(1_200, p.PCM(s.header(1, gen=1), 1_200))
    s.at(1_250, p.PCM(s.header(1, gen=1), 2_400))
    s.at(1_300, p.PlayStarted(UnitID(1), 62_400))
    s.at(1_600, p.SynthesisFinished(UnitID(1), 1))
    s.at(1_700, p.Vad(SpeechEnd(81_600)))
    s.partial(1_800, "我们下周开会。", final=True)
    s.at(2_900, p.PlayEnded(UnitID(1), 141_600))
    s.at(3_000, p.Tick())
    check_golden("basic_unit", s.lines)
    assert s.pipeline.state == translating(S1)
    assert p.ReleaseHeld(UnitID(1)) in s.effects


def test_fallback_rejects_stale_generation_and_recovers() -> None:
    s = Scenario()
    s.armed()
    s.at(0, p.Captured(SourceSpan(0, 48_000)))
    s.at(0, p.Vad(SpeechStart(0)))
    s.partial(500, "第一句话。")
    s.partial(600, "第一句话。")
    s.at(700, success(1, "First sentence."))
    s.at(800, p.PCM(s.header(1, gen=1), 4_800))
    s.partial(900, "第一句话。第二句话。")
    s.partial(1_000, "第一句话。第二句话。")
    s.at(1_100, success(2, "Second sentence."))
    s.at(1_200, p.TranslatedBacklog(3.5))
    s.at(1_200, p.Tick())
    # Unit 2's PCM was generated for generation 1 and must be dropped.
    s.at(1_300, p.PCM(s.header(2, gen=1), 4_800))
    s.at(1_400, p.TranslatedBacklog(0))
    s.at(1_400, p.Tick())
    s.partial(1_500, "第一句话。第二句话。第三句。")
    s.partial(1_600, "第一句话。第二句话。第三句。")
    s.at(1_700, success(3, "Third."))
    s.at(1_800, p.PCM(s.header(3, gen=2), 4_800))
    # Not yet stable for 2 s: stays degraded with unit 3 held.
    s.at(2_000, p.Tick())
    s.at(3_500, p.Tick())
    check_golden("fallback_recovery", s.lines)
    assert s.pipeline.state == translating(S1)
    assert p.DropPCM(s.header(2, gen=1), "stale_generation") in s.effects
    assert p.FlushTranslatedBus() in s.effects
    assert p.ReleaseHeld(UnitID(3)) in s.effects


def test_acquisition_without_profile_keeps_original() -> None:
    s = Scenario()
    s.armed(profile=False)
    s.at(0, p.Vad(SpeechStart(0)))
    s.partial(300, "嗯，好的。", final=True)
    s.at(500, success(1, "Okay."))
    s.at(600, p.Tick())
    check_golden("acquisition_no_profile", s.lines)
    assert s.pipeline.state == acquiring_voice(S1)
    assert not any(isinstance(e, p.Synthesize) for e in s.effects)


def test_discontinuity_cancels_units() -> None:
    s = Scenario()
    s.armed()
    s.at(0, p.Vad(SpeechStart(0)))
    s.partial(200, "这是测试。", final=True)
    s.at(300, success(1, "This is a test."))
    s.at(400, p.Discontinuity("device change"))
    s.at(500, p.PCM(s.header(1, gen=1, epoch=1), 2_400))
    s.at(600, success(1, "late"))
    check_golden("discontinuity", s.lines)
    assert s.pipeline.gate.epoch == 2
    assert p.CancelSynthesis(UnitID(1)) in s.effects


def test_synthesis_failure_discards_held_pcm() -> None:
    s = Scenario()
    s.armed()
    s.at(0, p.Vad(SpeechStart(0)))
    s.partial(100, "第一。", final=True)
    s.at(200, success(1, "First."))
    s.at(300, p.PCM(s.header(1, gen=1), 240))  # below prebuffer: held
    s.at(400, p.SynthesisFailed(UnitID(1), "model crashed"))
    assert p.DiscardHeld(UnitID(1)) in s.effects
    assert p.CancelSynthesis(UnitID(1)) in s.effects
    assert s.pipeline.gate.is_canceled(UnitID(1))
    assert '"disposition":"original_only","reason":"tts_failed"' in s.lines[-1]
    assert s.pipeline.state == acquiring_voice(S1)


def test_validation_rejects_bad_translations() -> None:
    s = Scenario()
    s.armed()
    s.at(0, p.Vad(SpeechStart(0)))
    s.partial(100, "第一。", final=True, utt=1)
    s.at(200, success(1, "第一。"))
    s.partial(300, "第二。", final=True, utt=2)
    s.at(400, success(2, "Second."))
    s.partial(500, "第三。", final=True, utt=3)
    s.at(600, success(3, "second."))
    s.partial(700, "第四。", final=True, utt=4)
    s.at(800, p.Translation(UnitID(4), p.Failure("model error")))
    check_golden("validation", s.lines)
