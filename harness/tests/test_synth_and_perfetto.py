import json

from a2a_harness import CANONICAL_RATE, fixture, perfetto, trace, REPO_ROOT
from a2a_harness.audio import read_wav
from a2a_harness.synth import SynthConfig, generate


def test_generation_is_deterministic(tmp_path):
    a = generate(tmp_path / "a", SynthConfig(sentences=3, fixture_id="d"))
    b = generate(tmp_path / "b", SynthConfig(sentences=3, fixture_id="d"))
    for suffix in (".wav", ".asr.json", ".mt.json"):
        assert (a.parent / f"d{suffix}").read_bytes() == (b.parent / f"d{suffix}").read_bytes()


def test_fixture_loads_and_audio_matches_manifest(synthetic_fixture):
    fx = fixture.load(synthetic_fixture)
    audio, rate = read_wav(fx.audio)
    assert rate == CANONICAL_RATE == fx.sample_rate
    assert abs(len(audio) / rate - fx.duration_s) < 0.01
    assert fx.kind == "synthetic" and fx.units
    assert not (audio == 0).any(), "synthetic audio must have a noise floor, not digital silence"


def test_scripted_hypotheses_never_use_future_audio(synthetic_fixture):
    fx = fixture.load(synthetic_fixture)
    script = json.loads(fx.asr_script.read_text(encoding="utf-8"))["hypotheses"]
    assert script
    for h in script:
        for tok in h.get("tokens", []):
            assert tok["end"] <= h["at_sample"], h


def test_dictionary_covers_every_clause_combination(synthetic_fixture):
    fx = fixture.load(synthetic_fixture)
    table = json.loads(fx.mt_dictionary.read_text(encoding="utf-8"))
    for unit in fx.units:
        assert unit.text in table


def test_merged_utterances_grow_across_sentences_and_finalize_once(tmp_path):
    path = generate(tmp_path / "long", SynthConfig(sentences=4, utterance_sentences=2, fixture_id="long"))
    script = json.loads((path.parent / "long.asr.json").read_text(encoding="utf-8"))["hypotheses"]
    by: dict[int, list[dict]] = {}
    for h in script:
        by.setdefault(h["utt"], []).append(h)
    assert len(by) == 2, "four sentences in two recognizer utterances"
    for utt, group in by.items():
        finals = [h for h in group if h["final"]]
        assert len(finals) == 1, f"utt {utt}: one final per recognizer utterance"
        terminators = finals[0]["text"].count("。")
        assert terminators == 2, f"utt {utt} must span two sentences"
        assert all(h["text"].count("。") < terminators for h in group if not h["final"]), \
            "recognition adds sentence terminators late, so a partial never shows all of them"
    reference = fixture.load(path).units
    longest = max(len(h["text"]) for h in by[1] if not h["final"])
    assert longest > len(reference[0].text) - 1, "partials must cross the sentence boundary"


def test_perfetto_conversion_of_golden_trace():
    events = trace.load(REPO_ROOT / "apple/Tests/A2ACoreTests/Golden/fallback_recovery.jsonl")
    out = perfetto.convert(events)
    phases = {e["ph"] for e in out["traceEvents"]}
    assert {"M", "i", "b", "e", "C"} <= phases
    names = {e["name"] for e in out["traceEvents"] if e["ph"] == "b"}
    assert "mt u1" in names and "playout u1" not in names   # u1 never finished playing
    json.dumps(out)
