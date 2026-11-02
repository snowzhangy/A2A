"""Offline-only ASR/translation worker. Protocol data and model inputs stay in memory."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys

from .protocol import WorkerChannel


def cuda_free_bytes() -> int | None:
    """Advisory free-memory reading for the default GPU; unknown is not a guarantee."""
    if os.environ.get("CUDA_VISIBLE_DEVICES") not in (None, "", "0"):
        return None
    try:
        output = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                                check=True, capture_output=True, text=True, timeout=5,
                                creationflags=0x08000000 if sys.platform == "win32" else 0)
        return int(output.stdout.splitlines()[0].strip()) * 1024 * 1024
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def asr_auto_device(cuda: bool, free_bytes: int | None, model_bytes: int) -> tuple[str, str | None]:
    if not cuda:
        return "cpu", None
    # Weights plus headroom for the CUDA runtime, decoding, and other applications.
    needed = model_bytes + 2 * 1024**3
    if free_bytes is not None and free_bytes < needed:
        return "cpu", "insufficient free GPU memory for recognition; use --asr-device cuda to override"
    return "cuda", None


def mt_auto_cuda(free_bytes: int | None, model_bytes: int) -> tuple[str, str | None]:
    # Qwen's safetensors are FP16; keep space for the context and generation cache.
    needed = model_bytes + 1536 * 1024**2
    if free_bytes is not None and free_bytes < needed:
        return "cpu", "insufficient free GPU memory for translation; use --mt-device cuda to override"
    return "cuda", None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("asr", "mt"))
    parser.add_argument("--path", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--debug-errors", action="store_true")
    args = parser.parse_args()
    channel = WorkerChannel()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    note = None
    try:
        if args.stage == "asr":
            from faster_whisper import WhisperModel
            import ctranslate2
            import numpy as np

            cuda = ctranslate2.get_cuda_device_count() > 0
            if args.device == "cuda" and not cuda:
                raise RuntimeError("CUDA requested but CTranslate2 cannot see a CUDA device")
            if args.device == "auto":
                device, note = asr_auto_device(cuda, cuda_free_bytes(), (Path(args.path) / "model.bin").stat().st_size)
            else:
                device = "cuda" if args.device == "cuda" else "cpu"
            # CTranslate2 has no MPS backend; explicit MPS still uses CPU for ASR.
            model = WhisperModel(args.path, device=device, compute_type="float16" if device == "cuda" else "int8",
                                 local_files_only=True)
            segments, _ = model.transcribe(np.zeros(16_000, dtype=np.float32), language=args.source, beam_size=1,
                                           condition_on_previous_text=False, vad_filter=False)
            list(segments)
        else:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            if args.device == "auto":
                if torch.cuda.is_available():
                    size = sum(p.stat().st_size for p in Path(args.path).glob("*.safetensors"))
                    device, note = mt_auto_cuda(cuda_free_bytes(), size)
                else:
                    device = "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu"
            else:
                device = args.device
            if device == "cuda" and not torch.cuda.is_available():
                raise RuntimeError("CUDA requested but PyTorch cannot see a CUDA device")
            if device == "mps" and not (getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()):
                raise RuntimeError("MPS requested but PyTorch cannot see an MPS device")
            dtype = torch.float16 if device in ("cuda", "mps") else torch.float32
            tokenizer = AutoTokenizer.from_pretrained(args.path, local_files_only=True)
            model = AutoModelForCausalLM.from_pretrained(args.path, local_files_only=True, dtype=dtype).to(device).eval()
            warmup = tokenizer.apply_chat_template([{"role": "user", "content": "Hello."}], tokenize=True,
                                                   add_generation_prompt=True, return_tensors="pt",
                                                   return_dict=True).to(device)
            with torch.inference_mode():
                model.generate(**warmup, max_new_tokens=1, do_sample=False)
        channel.send({"ev": "ready", "device": device, "note": note})
        while (frame := channel.receive()) is not None:
            request, payload = frame
            try:
                if args.stage == "asr":
                    audio = np.frombuffer(payload, dtype="<f4")
                    segments, _ = model.transcribe(audio, language=args.source, beam_size=1, word_timestamps=True,
                                                   condition_on_previous_text=False, vad_filter=False)
                    words = []
                    for segment in segments:
                        if segment.words:
                            words.extend({"text": w.word, "start": w.start, "end": w.end} for w in segment.words)
                        else:
                            words.append({"text": segment.text, "start": segment.start, "end": segment.end})
                    channel.send({"ev": "result", "id": request["id"], "words": words})
                else:
                    context = request.get("context", [])
                    glossary = request.get("glossary", [])
                    instruction = (f"Translate the user's {args.source} text into {args.target}. "
                                   "Return only the translation, preserving meaning and numbers. "
                                   "The following context is for reference; translate only the final text.")
                    messages = [{"role": "system", "content": instruction}]
                    for pair in context[-4:]:
                        messages.extend([{"role": "user", "content": pair[0]}, {"role": "assistant", "content": pair[1]}])
                    if glossary:
                        messages.append({"role": "system", "content": "Terminology: " + "; ".join(f"{a} = {b}" for a, b in glossary)})
                    messages.append({"role": "user", "content": request["text"]})
                    inputs = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True,
                                                           return_tensors="pt", return_dict=True).to(device)
                    with torch.inference_mode():
                        output = model.generate(**inputs, max_new_tokens=256, do_sample=False)
                    generated = output[0, inputs["input_ids"].shape[-1]:]
                    text = tokenizer.decode(generated, skip_special_tokens=True).strip()
                    complete = len(generated) < 256 or (len(generated) and int(generated[-1]) == tokenizer.eos_token_id)
                    channel.send({"ev": "result", "id": request["id"], "text": text, "complete": bool(complete)})
            except Exception as error:
                # Do not include input text in error messages or default diagnostics.
                message = f"{type(error).__name__}: {error}" if args.debug_errors else type(error).__name__
                channel.send({"ev": "error", "id": request.get("id"), "message": message})
    except Exception as error:
        channel.send({"ev": "error", "message": f"{type(error).__name__}: {error}"})
        sys.exit(1)


if __name__ == "__main__":
    main()
