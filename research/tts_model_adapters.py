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
        # Upstream keys ICL conditioning by (transcript, length, sample sum). Those
        # values are not a unique identity. Each prepared voice owns its codec cache;
        # the worker serializes prepare/generate and bounds prepared voices to eight.
        voice.conditioning_cache = {}
        self.model._icl_cache = voice.conditioning_cache
        # Prime the reference codec/text cache and speaker encoder without generating
        # throwaway speech. These private helpers are covered by the pinned mlx-audio
        # version; the global worker warmup still compiles actual generation.
        if voice.mode == "icl":
            inputs = self.model._prepare_icl_generation_inputs(
                text="Warm up.", ref_audio=reference, ref_text=voice.text, language=voice.language)
        else:
            inputs = self.model._prepare_generation_inputs(
                text="Warm up.", ref_audio=reference, language=voice.language)
        self.mx.eval(*inputs)
        return reference

    def generate(self, voice, text, interval, max_tokens, temperature):
        self.model._icl_cache = voice.conditioning_cache
        kwargs = dict(text=text, ref_audio=voice.native, lang_code=voice.language, stream=True,
                      streaming_interval=interval, max_tokens=max_tokens, verbose=False)
        if temperature is not None:
            kwargs["temperature"] = temperature
        if voice.mode == "icl":
            kwargs["ref_text"] = voice.text
        yield from self.model.generate(**kwargs)


class Audio8Adapter:
    # The pinned MLX implementation yields a complete phrase, not streaming audio.
    streaming = False

    def __init__(self, model, mx):
        self.model, self.mx = model, mx

    def prepare(self, voice):
        if not voice.text.strip():
            raise ValueError("Audio8 needs the reference transcript")
        from mlx_audio.resample import resample_audio_array
        import numpy as np
        audio = resample_audio_array(np.asarray(voice.pcm, dtype=np.float32), 24_000, 44_100)
        reference = self.mx.array(audio)
        self.mx.eval(reference)
        return reference

    def generate(self, voice, text, interval, max_tokens, temperature):
        # Audio8 runs at approximately 21.5 codec frames/s versus Qwen's 12.5.
        kwargs = dict(text=text, ref_audio=voice.native, ref_text=voice.text,
                      max_tokens=min(900, int(max_tokens * (44_100 / 2048) / 12.5) + 1))
        if temperature is not None:
            kwargs["temperature"] = temperature
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

    def estimated_duration(self, voice, text):
        # Use the model's phonetic weighting and THIS reference's codec duration.
        # A rate calibrated on generated warm-up audio is circular for a model
        # whose output length is fixed by duration_s, and ignores zh/en density.
        # The upstream MLX default estimates against "Nice to meet you.", rather
        # than the supplied reference, so pass the reference-based duration explicitly.
        frames = self.duration_estimator.estimate_duration(text, voice.text, voice.native.shape[0])
        return min(20.0, max(0.5, frames / 25.0))

    def generate(self, voice, text, interval, max_tokens, temperature):
        # Diffusion generates the whole phrase. Chunking afterwards does not make it streaming.
        duration = self.estimated_duration(voice, text)
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


def configure_audio8_quantized_runtime(model, mx):
    # mlx-audio 0.6.1 reads the tied output embedding as a dense matrix.
    # This conversion quantizes it, so use its quantized linear operation.
    from types import MethodType
    slow = model.model
    if hasattr(slow.embeddings, "scales"):
        def quantized_slow_step(self, input_ids, cache_start, cache_positions, position_ids, attention_mask):
            hidden = self._embed(input_ids)
            rope = self._freqs_cis[position_ids]
            mask = self._causal_mask(attention_mask, cache_positions, self.config.max_seq_len)
            for layer in self.layers:
                hidden = layer(hidden, rope, mask, cache_start)
            hidden = hidden[:, -1:]
            normalized = self.norm(hidden)
            logits = self.embeddings.as_linear(normalized)[:, -1]
            return logits, normalized if self.config.norm_fastlayer_input else hidden
        slow._slow_step = MethodType(quantized_slow_step, slow)
        # Packed quantized weights are uint32; using that dtype for the KV
        # cache truncates the attention keys/values and produces garbage audio.
        # Match upstream: determine dtype from the decoded embedding output.
        setup_cache = slow._setup_generation_caches
        def quantized_setup_cache(self, batch_size, _packed_weight_dtype):
            embedding_dtype = self.text_embeddings(mx.array([[0]])).dtype
            return setup_cache(batch_size, embedding_dtype)
        slow._setup_generation_caches = MethodType(quantized_setup_cache, slow)

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
    if key == "audio8":
        configure_audio8_quantized_runtime(model, mx)
        return Audio8Adapter(model, mx)
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
        # Keep the array alive so Python cannot recycle its identity for another
        # speaker. Reference arrays are immutable and reused by the pinned port.
        cache_key = (id(audio), sr)
        if cache_key not in cache:
            if len(cache) >= 8:
                cache.pop(next(iter(cache)))
            cache[cache_key] = (audio, extract(audio, sr))
        return cache[cache_key][1]

    model.extract_speaker_embedding = cached
    return QwenAdapter(model, mx)
