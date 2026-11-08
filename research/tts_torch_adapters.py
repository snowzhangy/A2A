"""PyTorch model adapters for the resident TTS worker (`tts_worker.py --backend torch`).

CUDA on Windows and Linux, MPS on Apple silicon, CPU elsewhere (ADR-0007). Paths come from
verified manifest entries only; loading never downloads. Reference audio stays in memory.
"""
from __future__ import annotations

import os
import sys


def pick_device(requested: str = "auto") -> str:
    import torch

    if requested != "auto":
        if requested == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("--device cuda requested but torch.cuda is not available")
        if requested == "mps" and not torch.backends.mps.is_available():
            raise RuntimeError("--device mps requested but MPS is not available")
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def device_label(device: str) -> str:
    import torch

    if device.startswith("cuda"):
        return f"cuda:{torch.cuda.get_device_name(0)}"
    return device


class _PhraseGraph:
    """Replays OmniVoice's per-step forward as one CUDA graph.

    Eager PyTorch is launch-bound on Windows: a 16-step phrase issues ~29,500 kernel launches
    at ~25 µs each under WDDM (889 ms wall for 177 ms of GPU work on an RTX 4090). Within a
    phrase every step passes the same input tensors (updated in place) with the same shapes,
    so the first step runs eagerly as a warm-up, the second is captured, and the rest replay
    the graph. A new phrase (new tensors) captures again and releases the previous graph.
    Any capture failure disables graphs for the session and falls back to eager forwards.
    """

    def __init__(self, model, torch) -> None:
        self.torch = torch
        self.eager = model.forward
        self.key = None
        self.graph = None
        self.output = None
        self.warm = False
        self.disabled = False
        self.replays = 0
        model.forward = self

    def __call__(self, input_ids=None, audio_mask=None, attention_mask=None, **kwargs):
        torch = self.torch
        if self.disabled or kwargs or attention_mask is None or torch.is_grad_enabled():
            return self.eager(input_ids=input_ids, audio_mask=audio_mask, attention_mask=attention_mask, **kwargs)
        key = (input_ids.data_ptr(), audio_mask.data_ptr(), attention_mask.data_ptr(), tuple(input_ids.shape),
               tuple(attention_mask.shape))
        if key == self.key and self.graph is not None:
            self.graph.replay()
            self.replays += 1
            return self.output
        if key != self.key:
            self.key, self.graph, self.output, self.warm = key, None, None, False
            return self.eager(input_ids=input_ids, audio_mask=audio_mask, attention_mask=attention_mask)
        try:
            # The eager first step of this phrase was the warm-up (cuBLAS handles, workspaces).
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph):
                output = self.eager(input_ids=input_ids, audio_mask=audio_mask, attention_mask=attention_mask)
        except Exception as error:  # an operator that cannot be captured: stay eager
            self.disabled = True
            sys.stderr.write(f"tts_worker: CUDA graphs disabled ({type(error).__name__}: {error})\n")
            return self.eager(input_ids=input_ids, audio_mask=audio_mask, attention_mask=attention_mask)
        self.graph, self.output = graph, output
        graph.replay()  # capture records without executing; run this step through the graph
        self.replays += 1
        return output


class OmniVoiceTorchAdapter:
    """OmniVoice generates each phrase by masked diffusion: whole-phrase output (no audio
    streaming) but fast on a GPU, with native speed control used for fluent pacing."""

    streaming = False
    native_speed = True

    def __init__(self, path, device: str, num_step: int) -> None:
        import torch
        from omnivoice import OmniVoice

        self.torch = torch
        self.device = device
        # float16 on CUDA; MPS fp16 is unstable for this model's tokenizer path, CPU needs fp32.
        dtype = torch.float16 if device.startswith("cuda") else torch.float32
        device_map = "cuda:0" if device == "cuda" else device
        self.model = OmniVoice.from_pretrained(str(path), device_map=device_map, dtype=dtype, load_asr=False)
        self.num_step = num_step
        self.graphs = None
        if device.startswith("cuda") and os.environ.get("A2A_TTS_CUDA_GRAPHS", "1") != "0":
            self.graphs = _PhraseGraph(self.model, torch)

    def prepare(self, voice):
        import numpy as np

        if not voice.text.strip():
            raise ValueError("OmniVoice needs the reference transcript")
        wave = self.torch.from_numpy(np.asarray(voice.pcm, dtype=np.float32))
        # Without inference mode the audio tokenizer keeps its activations for autograd: a
        # 9.5 s reference peaked 1.81 GB above the weights instead of 0.17 GB. On Windows
        # (WDDM) that spike pages the other workers' VRAM out to system RAM, and recognition
        # and synthesis then stalled together for 6.5 s in a live run. The tokenizer stays
        # fp32: fp16 autocast changes a third of the reference codes.
        with self.torch.inference_mode():
            prompt = self.model.create_voice_clone_prompt(ref_audio=(wave, 24_000), ref_text=voice.text)
        if self.device.startswith("cuda"):
            # Memory a process merely caches still counts as resident under WDDM.
            self.torch.cuda.empty_cache()
        return prompt

    def generate(self, voice, text, speed: float = 1.0):
        import numpy as np

        language = "zh" if voice.language == "chinese" else "en"
        with self.torch.inference_mode():
            audio = self.model.generate(text=text, language=language, voice_clone_prompt=voice.native,
                                        speed=speed if speed and abs(speed - 1.0) > 1e-3 else None,
                                        num_step=self.num_step)
        rate = getattr(self.model, "sampling_rate", 24_000)
        if rate != 24_000:
            raise RuntimeError(f"unexpected sample rate {rate}")
        for wav in audio:
            yield np.asarray(wav, dtype=np.float32).reshape(-1)


def import_model_package(key: str) -> None:
    """Imports the model's package ahead of `load_adapter`, so the worker can overlap the
    import (12 s for omnivoice on Windows, most of it transformers.modeling_utils) with
    verifying the weights."""
    if key == "omnivoice":
        import omnivoice  # noqa: F401


def load_adapter(key: str, path, device: str):
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    if key == "omnivoice":
        # 16 decoding steps: back-transcribed, 18/18 demo phrases were exact at 16, 24, and 32
        # steps (249 / 312 / 381 ms median on an RTX 4090 with CUDA graphs); at the 1.3x
        # fluent cap 16 and 32 steps both misread one word in 18. A2A_OMNIVOICE_STEPS overrides.
        return OmniVoiceTorchAdapter(path, device, int(os.environ.get("A2A_OMNIVOICE_STEPS", "16")))
    raise ValueError(f"no PyTorch adapter for {key}: the torch backend supports omnivoice "
                     "(qwen and chatterbox run on MLX via --backend mlx)")
