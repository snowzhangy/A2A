"""Experimental model selection contracts; no weights, GPU, or network needed."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import numpy as np
import pytest

from a2a_harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "research"))
import voice_models
from tts_model_adapters import ChatterboxAdapter, OmniVoiceAdapter, QwenAdapter


class Model:
    def __init__(self):
        self.calls = []
        self.audio_tokenizer = SimpleNamespace(encode=self.encode)

    def encode(self, reference):
        self.calls.append(("encode", reference))
        return [np.zeros((50, 8), dtype=np.int32)]

    def prepare_conditionals(self, reference, rate):
        self.calls.append(("prepare", rate))
        return "cached-conditionals"

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        yield "audio"


def voice(language="english", mode="icl"):
    return SimpleNamespace(pcm=[0.1] * 24, native="cached-reference", text="reference words",
                           language=language, mode=mode, seconds_per_character=0.075)


class DurationEstimator:
    def __init__(self, frames=75):
        self.frames = frames
        self.calls = []

    def estimate_duration(self, text, reference_text, reference_frames):
        self.calls.append((text, reference_text, reference_frames))
        return self.frames


def test_catalog_has_three_explicit_model_choices_and_pinned_dependencies():
    doc = voice_models.manifest()
    assert [v["key"] for v in voice_models.installations(doc)] == ["qwen", "omnivoice", "chatterbox"]
    for key in voice_models.KEYS:
        entries = voice_models.selection(key, doc)
        assert entries[-1]["experimental_key"] == key
        assert all(e["research_only"] and len(e["upstream"]["revision"]) == 40 for e in entries)
        assert all(f["sha256"] and f["size"] > 0 for e in entries for f in e["files"])
    assert len(voice_models.selection("chatterbox", doc)) == 2
    with pytest.raises(ValueError):
        voice_models.selection("auto", doc)


def test_installation_requires_the_selected_model_and_every_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_models, "ROOT", tmp_path)
    doc = {"cache_dir": "models", "entries": []}
    for key in voice_models.KEYS:
        doc["entries"].append(dict(id=key, experimental_key=key, enabled=True, status="verified",
                                    upstream={"revision": "rev"}, files=[{"path": "w", "size": 4}],
                                    dependencies=["codec"] if key == "chatterbox" else []))
    doc["entries"].append(dict(id="codec", enabled=True, status="verified", upstream={"revision": "rev"},
                                files=[{"path": "w", "size": 2}]))
    for key in voice_models.KEYS:
        folder = tmp_path / "models" / key / "rev"
        folder.mkdir(parents=True)
        (folder / "w").write_bytes(b"1234")
    assert [x["installed"] for x in voice_models.installations(doc)] == [True, True, False]
    folder = tmp_path / "models/codec/rev"
    folder.mkdir(parents=True)
    (folder / "w").write_bytes(b"12")
    assert all(x["installed"] for x in voice_models.installations(doc))
    (folder / "w").write_bytes(b"1")
    assert not voice_models.installations(doc)[-1]["installed"]


@pytest.mark.parametrize("language,code", [("english", "en"), ("chinese", "zh")])
def test_omnivoice_reuses_reference_tokens_and_selects_target_language(language, code):
    model = Model()
    mx = SimpleNamespace(array=np.array, eval=lambda *args: None)
    estimator = DurationEstimator()
    adapter = OmniVoiceAdapter(model, mx, estimator)
    v = voice(language)
    v.native = adapter.prepare(v)
    assert list(adapter.generate(v, "translated words", 0.32, 100, None)) == ["audio"]
    assert list(adapter.generate(v, "more words", 0.32, 100, None)) == ["audio"]
    assert sum(isinstance(c, tuple) and c[0] == "encode" for c in model.calls) == 1
    assert model.calls[-1]["language"] == code
    assert model.calls[-1]["ref_tokens"] is v.native
    assert model.calls[-1]["ref_text"] == v.text
    assert model.calls[-1]["duration_s"] == 3
    assert estimator.calls == [("translated words", v.text, 50), ("more words", v.text, 50)]
    assert not adapter.streaming


def test_omnivoice_duration_cannot_drift_from_its_own_warmup_or_pacing_feedback():
    model = Model()
    estimator = DurationEstimator()
    adapter = OmniVoiceAdapter(model, SimpleNamespace(array=np.array, eval=lambda *args: None), estimator)
    v = voice()
    v.text = "这是中文参考。"
    v.native = adapter.prepare(v)
    list(adapter.generate(v, "That's her.", 0.32, 100, None))
    initial = model.calls[-1]["duration_s"]
    v.seconds_per_character = 100  # another utterance's generated duration must not set this one's length
    list(adapter.generate(v, "That's her.", 0.32, 100, None))
    assert model.calls[-1]["duration_s"] == initial
    assert estimator.calls[-1] == ("That's her.", v.text, 50)


@pytest.mark.parametrize("frames,seconds", [(1, 0.5), (10000, 20)])
def test_omnivoice_reference_based_duration_stays_bounded(frames, seconds):
    model = Model()
    adapter = OmniVoiceAdapter(model, SimpleNamespace(array=np.array, eval=lambda *args: None),
                               DurationEstimator(frames))
    v = voice()
    v.native = adapter.prepare(v)
    list(adapter.generate(v, "Speech.", 0.32, 100, None))
    assert model.calls[-1]["duration_s"] == seconds


@pytest.mark.parametrize("language,code", [("english", "en"), ("chinese", "zh")])
def test_chatterbox_uses_prepared_conditionals_without_reencoding(language, code):
    model = Model()
    adapter = ChatterboxAdapter(model, SimpleNamespace(array=np.array))
    v = voice(language)
    v.native = adapter.prepare(v)
    assert list(adapter.generate(v, "translated words", 0.32, 100, None)) == ["audio"]
    assert model.calls[0] == ("prepare", 24_000)
    assert model.calls[-1]["conds"] == "cached-conditionals"
    assert model.calls[-1]["lang_code"] == code
    assert model.calls[-1]["max_new_tokens"] == 200
    assert not adapter.streaming


@pytest.mark.parametrize("mode", ["icl", "xvector"])
def test_qwen_keeps_streaming_and_its_conditioning_modes(mode):
    model = Model()
    adapter = QwenAdapter(model, None)
    v = voice("chinese", mode)
    assert list(adapter.generate(v, "translated words", 0.32, 100, 0.6)) == ["audio"]
    args = model.calls[-1]
    assert args["stream"] and args["lang_code"] == "chinese"
    assert args["ref_audio"] == v.native
    assert ("ref_text" in args) == (mode == "icl")
    assert adapter.streaming
