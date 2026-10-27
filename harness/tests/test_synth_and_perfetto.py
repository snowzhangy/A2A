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
    script = json.loads(fx.asr_script.read_text())["hypotheses"]
    assert script
    for h in script:
        for tok in h.get("tokens", []):
            assert tok["end"] <= h["at_sample"], h


def test_dictionary_covers_every_clause_combination(synthetic_fixture):
    fx = fixture.load(synthetic_fixture)
    table = json.loads(fx.mt_dictionary.read_text())
    for unit in fx.units:
        assert unit.text in table


def test_perfetto_conversion_of_golden_trace():
    events = trace.load(REPO_ROOT / "apple/Tests/A2ACoreTests/Golden/fallback_recovery.jsonl")
    out = perfetto.convert(events)
    phases = {e["ph"] for e in out["traceEvents"]}
    assert {"M", "i", "b", "e", "C"} <= phases
    names = {e["name"] for e in out["traceEvents"] if e["ph"] == "b"}
    assert "mt u1" in names and "playout u1" not in names   # u1 never finished playing
    json.dumps(out)
