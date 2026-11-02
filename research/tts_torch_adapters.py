"""PyTorch model adapters for the resident TTS worker (`tts_worker.py --backend torch`).

CUDA on Windows and Linux, MPS on Apple silicon, CPU elsewhere (ADR-0007). Paths come from
verified manifest entries only; loading never downloads. Reference audio stays in memory.
"""
from __future__ import annotations

import os


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

    def prepare(self, voice):
        import numpy as np

        if not voice.text.strip():
            raise ValueError("OmniVoice needs the reference transcript")
        wave = self.torch.from_numpy(np.asarray(voice.pcm, dtype=np.float32))
        return self.model.create_voice_clone_prompt(ref_audio=(wave, 24_000), ref_text=voice.text)

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


def load_adapter(key: str, path, device: str):
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    if key == "omnivoice":
        # 16 decoding steps halve generation time against 32 with little audible loss for
        # short phrases (the upstream README suggests 16 for speed); A2A_OMNIVOICE_STEPS overrides.
        return OmniVoiceTorchAdapter(path, device, int(os.environ.get("A2A_OMNIVOICE_STEPS", "16")))
    raise ValueError(f"no PyTorch adapter for {key}: the torch backend supports omnivoice "
                     "(qwen and chatterbox run on MLX via --backend mlx)")
