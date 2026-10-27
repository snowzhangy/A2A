"""MLX model-specific reference preparation and generation. All paths are verified locally."""
from __future__ import annotations

import json


class QwenAdapter:
    streaming = True

    def __init__(self, model, mx):
        self.model, self.mx = model, mx

    def prepare(self, voice):
        reference = self.mx.array(voice.pcm)
        self.mx.eval(reference)
        return reference

    def generate(self, voice, text, interval, max_tokens, temperature):
        kwargs = dict(text=text, ref_audio=voice.native, lang_code=voice.language, stream=True,
                      streaming_interval=interval, max_tokens=max_tokens, verbose=False)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if voice.mode == "icl":
            kwargs["ref_text"] = voice.text
        yield from self.model.generate(**kwargs)


class OmniVoiceAdapter:
    streaming = False

    def __init__(self, model, mx, duration_estimator):
        self.model, self.mx = model, mx
        self.duration_estimator = duration_estimator

    def prepare(self, voice):
        if not voice.text.strip():
            raise ValueError("OmniVoice needs the reference transcript")
        reference = self.mx.array(voice.pcm)[None, :, None]
        tokens = self.model.audio_tokenizer.encode(reference)[0]
        self.mx.eval(tokens)
        return tokens

    def generate(self, voice, text, interval, max_tokens, temperature):
        # Diffusion generates the whole phrase. Chunking afterwards does not make it streaming.
        # Use the model's phonetic weighting and THIS reference's codec duration.
        # A rate calibrated on generated warm-up audio is circular for a model
        # whose output length is fixed by duration_s, and ignores zh/en density.
        # The upstream MLX default estimates against "Nice to meet you.", rather
        # than the supplied reference, so pass the reference-based duration explicitly.
        frames = self.duration_estimator.estimate_duration(text, voice.text, voice.native.shape[0])
        duration = min(20.0, max(0.5, frames / 25.0))
        yield from self.model.generate(text=text, language="zh" if voice.language == "chinese" else "en",
                                       ref_tokens=voice.native, ref_text=voice.text,
                                       duration_s=duration, num_steps=32)


class ChatterboxAdapter:
    streaming = False

    def __init__(self, model, mx):
        self.model, self.mx = model, mx

    def prepare(self, voice):
        return self.model.prepare_conditionals(self.mx.array(voice.pcm), 24_000)

    def generate(self, voice, text, interval, max_tokens, temperature):
        kwargs = dict(text=text, conds=voice.native, lang_code="zh" if voice.language == "chinese" else "en",
                      max_new_tokens=min(1000, max_tokens * 2), verbose=False)
        if temperature is not None:
            kwargs["temperature"] = temperature
        yield from self.model.generate(**kwargs)


def load_adapter(key, path, dependencies, mx):
    if key == "chatterbox":
        # The upstream post-load hook calls snapshot_download for an unpinned tokenizer.
        # Load its components directly from our independently verified manifest entries.
        from mlx_audio.tts.models.chatterbox import Model, ModelConfig
        from mlx_audio.tts.models.chatterbox.s3tokenizer import S3TokenizerV2
        model = Model(ModelConfig.from_dict(json.loads((path / "config.json").read_text(encoding="utf-8"))))
        weights = model.sanitize(mx.load(str(path / "model.safetensors")))
        model.load_weights(list(weights.items()), strict=True)
        model._init_text_tokenizers(path)
        tokenizer_path = dependencies["s3-tokenizer-v2-mlx"]
        tokenizer = S3TokenizerV2("speech_tokenizer_v2_25hz")
        weights = mx.load(str(tokenizer_path / "model.safetensors"))
        if hasattr(tokenizer, "sanitize"):
            weights = tokenizer.sanitize(weights)
        tokenizer.load_weights(list(weights.items()), strict=True)
        model._s3_tokenizer = tokenizer
        model._conds = None
        mx.eval(model.parameters(), tokenizer.parameters())
        model.eval()
        return ChatterboxAdapter(model, mx)

    from mlx_audio.tts.utils import load_model
    model = load_model(str(path), strict=True)
    if key == "omnivoice":
        from mlx_audio.tts.models.omnivoice.duration import RuleDurationEstimator
        if model.text_tokenizer is None or model.audio_tokenizer.semantic_model is None:
            raise RuntimeError("OmniVoice needs local text and full reference-audio tokenizers")
        return OmniVoiceAdapter(model, mx, RuleDurationEstimator())
    if key != "qwen":
        raise ValueError(f"unknown experimental model: {key}")
    if (model.tokenizer is None or model.speech_tokenizer is None
            or not model.speech_tokenizer.has_encoder or model.speaker_encoder is None):
        raise RuntimeError("required local tokenizers or encoders did not load")
    extract = model.extract_speaker_embedding
    cache = {}

    def cached(audio, sr=24_000):
        cache_key = (audio.size, float(audio.sum()))
        if cache_key not in cache:
            if len(cache) >= 8:
                cache.pop(next(iter(cache)))
            cache[cache_key] = extract(audio, sr)
        return cache[cache_key]

    model.extract_speaker_embedding = cached
    return QwenAdapter(model, mx)
