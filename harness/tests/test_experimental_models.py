"""Experimental model selection contracts; no weights, GPU, or network needed."""
from __future__ import annotations

import sys
import array
from types import SimpleNamespace

import numpy as np
import pytest

from a2a_harness import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "research"))
import voice_models
from tts_worker import ModelBackend, Voice, voiced_rms
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
                           language=language, mode=mode, seconds_per_character=0.075, conditioning_cache={})


class DurationEstimator:
    def __init__(self, frames=75):
        self.frames = frames
        self.calls = []

    def estimate_duration(self, text, reference_text, reference_frames):
        self.calls.append((text, reference_text, reference_frames))
        return self.frames


def test_catalog_has_explicit_model_choices_and_pinned_dependencies():
    doc = voice_models.manifest()
    assert [v["key"] for v in voice_models.installations(doc)] == ["qwen", "audio8", "omnivoice", "chatterbox"]
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
    assert [x["installed"] for x in voice_models.installations(doc)] == [True, True, True, False]
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


def test_omnivoice_profile_preparation_does_not_synthesize_a_warmup_sentence():
    model = Model()
    backend = ModelBackend("omnivoice", None)
    backend.adapter = OmniVoiceAdapter(model, SimpleNamespace(array=np.array, eval=lambda *args: None),
                                      DurationEstimator())
    v = voice()
    backend.prepare(v)
    assert v.native.shape == (50, 8)
    assert not any(isinstance(call, dict) for call in model.calls)
    assert backend.estimated_duration(v, "hello") == 3.0


@pytest.mark.parametrize("mode", ["icl", "xvector"])
def test_qwen_primes_conditioning_without_synthesizing_throwaway_speech(mode):
    calls = []
    class ConditioningModel:
        _icl_cache = {}
        def _prepare_icl_generation_inputs(self, **kwargs):
            calls.append(("icl", kwargs))
            return (np.zeros(1),) * 4
        def _prepare_generation_inputs(self, **kwargs):
            calls.append(("xvector", kwargs))
            return (np.zeros(1),) * 3
        def generate(self, **kwargs):
            raise AssertionError("per-speaker preparation must not generate speech")
    model = ConditioningModel()
    mx = SimpleNamespace(array=np.array, eval=lambda *args: None)
    backend = ModelBackend("qwen", None)
    backend.model = model
    backend.adapter = QwenAdapter(model, mx)
    v = voice("chinese", mode)
    backend.prepare(v)
    assert len(calls) == 1 and calls[0][0] == mode
    assert calls[0][1]["language"] == "chinese"
    assert ("ref_text" in calls[0][1]) == (mode == "icl")
    np.testing.assert_array_equal(v.native, v.pcm)


def test_whole_phrase_initial_gain_uses_the_entire_voice_not_only_the_onset():
    backend = ModelBackend("omnivoice", None)
    samples = np.concatenate([np.full(7680, 0.01, dtype=np.float32),
                              np.full(7680, 0.2, dtype=np.float32)])
    backend.adapter = SimpleNamespace(generate=lambda *args: iter([SimpleNamespace(audio=samples, sample_rate=24_000)]))
    backend.mx = SimpleNamespace(eval=lambda *args: None)
    backend.np = np
    backend.streaming = False
    v = Voice(array.array("f", [0.1] * 24_000), "reference", "icl")
    first = next(backend.generate(v, "hello", 0.32))
    assert len(first) == 7680
    assert v.gain == pytest.approx(v.target_rms / voiced_rms(array.array("f", samples)))
    assert v.gain < 1.0  # the quiet first chunk alone would have requested a much larger gain


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


def test_qwen_returning_voice_is_independent_of_intervening_actor_sampling():
    rng = np.random.RandomState()
    backend = ModelBackend("qwen", 0.6)
    backend.mx = SimpleNamespace(eval=lambda *args: None, random=SimpleNamespace(seed=rng.seed))
    backend.np = np
    backend.streaming = True
    def generate(*args):
        yield SimpleNamespace(audio=rng.normal(size=48).astype(np.float32), sample_rate=24_000)
    backend.adapter = SimpleNamespace(generate=generate)
    a = Voice(array.array("f", [0.1] * 24), "reference A", "icl")
    b = Voice(array.array("f", [0.2] * 24), "reference B", "icl")
    first = list(backend.generate(a, "Hello again.", 0.32))
    list(backend.generate(b, "Another actor speaks.", 0.32))
    repeated = list(backend.generate(a, "Hello again.", 0.32))
    assert first == repeated
    # Re-enrollment/session labels do not enter the seed. The actual reference does.
    assert a.sampling_seed == Voice(a.pcm, a.text, "icl").sampling_seed
    assert a.sampling_seed != b.sampling_seed


def test_qwen_switching_actors_restores_their_own_reference_codec_cache():
    class CacheModel:
        def _prepare_icl_generation_inputs(self, ref_audio, **kwargs):
            self._icl_cache["same-upstream-key"] = ref_audio.copy()
            return (np.zeros(1),) * 4
        def generate(self, **kwargs):
            yield self._icl_cache["same-upstream-key"].copy()
    adapter = QwenAdapter(CacheModel(), SimpleNamespace(array=np.array, eval=lambda *args: None))
    a, b = voice(), voice()
    # Same length and sample sum, different voices: the old fingerprint collides.
    a.pcm, b.pcm = [0.1, 0.2], [0.2, 0.1]
    a.native, b.native = adapter.prepare(a), adapter.prepare(b)
    out = [list(adapter.generate(v, "Hello.", 0.32, 100, 0.6))[0] for v in (a, b, a)]
    np.testing.assert_array_equal(out[0], a.pcm)
    np.testing.assert_array_equal(out[1], b.pcm)
    np.testing.assert_array_equal(out[2], a.pcm)


def test_audio8_passes_the_matching_reference_and_scales_codec_frame_budget():
    from tts_model_adapters import Audio8Adapter
    model = Model()
    adapter = Audio8Adapter(model, SimpleNamespace())
    reference = voice()
    assert list(adapter.generate(reference, "Hello there.", 0.32, 100, 0.6)) == ["audio"]
    call = model.calls[-1]
    assert call["ref_audio"] is reference.native
    assert call["ref_text"] == reference.text
    assert call["max_tokens"] == 173
    assert call["temperature"] == 0.6
    assert not adapter.streaming


def test_reference_only_transcription_is_in_memory_and_only_for_models_that_need_it(monkeypatch):
    calls = []
    def transcribe(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=b'{"text": "reference words"}')
    monkeypatch.setattr("tts_worker.subprocess.run", transcribe)
    adapter = SimpleNamespace(prepare=lambda reference: "encoded")
    for name in ("audio8", "omnivoice"):
        backend = ModelBackend(name, None, "/helper", "zh-CN")
        backend.adapter = adapter
        reference = Voice(array.array("f", [0.1] * 24000), "", "xvector")
        backend.prepare(reference)
        assert reference.text == "reference words" and reference.native == "encoded"
    assert len(calls) == 2
    assert calls[0][0] == ["/helper", "--stdin-f32", "--rate", "24000", "--locale", "zh-CN"]
    assert len(calls[0][1]["input"]) == 24000 * 4
    assert calls[0][1]["stderr"] is not None
    backend = ModelBackend("qwen", None, "/helper", "zh-CN")
    backend.adapter = adapter
    backend.prepare(Voice(array.array("f", [0.1] * 24000), "", "xvector"))
    assert len(calls) == 2


def test_audio8_quantized_cache_uses_embedding_output_dtype():
    from tts_model_adapters import configure_audio8_quantized_runtime
    class Embedding:
        scales = True
        weight = np.zeros((1,), dtype=np.uint32)
        def __call__(self, tokens):
            return np.zeros((1, 1, 4), dtype=np.float16)
    recorded = []
    embedding = Embedding()
    slow = SimpleNamespace(embeddings=embedding, text_embeddings=embedding,
                           _setup_generation_caches=lambda batch, dtype: recorded.append((batch, dtype)))
    configure_audio8_quantized_runtime(SimpleNamespace(model=slow), SimpleNamespace(array=np.array))
    slow._setup_generation_caches(2, np.uint32)
    assert recorded == [(2, np.dtype("float16"))]
