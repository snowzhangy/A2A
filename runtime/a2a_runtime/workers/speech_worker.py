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
                                check=True, capture_output=True, text=True, encoding="utf-8", timeout=5,
                                creationflags=0x08000000 if sys.platform == "win32" else 0)
        return int(output.stdout.splitlines()[0].strip()) * 1024 * 1024
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


# CTranslate2 int8 weights with float16 compute: about half the VRAM of float16 for large-v3
# with negligible accuracy loss, which leaves room for translation and synthesis on one GPU.
ASR_CUDA_COMPUTE = os.environ.get("A2A_ASR_COMPUTE", "int8_float16")
ASR_BEAM = int(os.environ.get("A2A_ASR_BEAM", "5"))
# Mandarin prompt: Whisper otherwise often omits punctuation (CommitPolicy closes clauses at
# punctuation) or drifts into traditional characters.
ASR_PROMPTS = {"zh": "以下是普通话的句子，使用简体中文和标点符号。"}
# Stock credits and channel calls Whisper invents over music or silence (the same idea as
# dub-anything's languages/zh.py). A segment made only of one of these is dropped.
HALLUCINATIONS = ("感谢观看", "感謝觀看", "谢谢观看", "謝謝觀看", "感谢收看", "欢迎订阅", "歡迎訂閱", "请订阅",
                  "請訂閱", "字幕由", "字幕提供", "点赞订阅", "點讚訂閱", "请不吝点赞", "明镜与点点", "amara.org",
                  "thanks for watching", "thank you for watching", "subtitles by", "please subscribe")


def _phrase_key(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())  # Han characters are alphanumeric


def hallucinated(segment) -> bool:
    """Drop segments Whisper produces from non-speech: likely silence with low confidence,
    runaway repetition, or a bare stock credit."""
    if getattr(segment, "no_speech_prob", 0) > 0.6 and getattr(segment, "avg_logprob", 0) < -1.0:
        return True
    if getattr(segment, "compression_ratio", 0) > 2.4:
        return True
    key = _phrase_key(segment.text)
    return bool(key) and any(_phrase_key(h) in key and len(key) <= len(_phrase_key(h)) + 4 for h in HALLUCINATIONS)


def asr_auto_device(cuda: bool, free_bytes: int | None, model_bytes: int) -> tuple[str, str | None]:
    if not cuda:
        return "cpu", None
    # Weights (int8 is about 55% of the float16 file) plus headroom for the CUDA runtime,
    # decoding, and other applications.
    needed = int(model_bytes * (0.55 if ASR_CUDA_COMPUTE.startswith("int8") else 1.0)) + 1024**3
    if free_bytes is not None and free_bytes < needed:
        return "cpu", "insufficient free GPU memory for recognition; use --asr-device cuda to override"
    return "cuda", None


def mt_auto_cuda(free_bytes: int | None, model_bytes: int) -> tuple[str, str | None]:
    # Qwen's safetensors are FP16; keep space for the context and generation cache.
    needed = model_bytes + 1536 * 1024**2
    if free_bytes is not None and free_bytes < needed:
        return "cpu", "insufficient free GPU memory for translation; use --mt-device cuda to override"
    return "cuda", None


def free_after_reserve(free_bytes: int | None, reserve_bytes: int) -> int | None:
    """Free GPU memory left once workers that load after this one take `reserve_bytes`. Under
    WDDM an oversubscribed card pages every worker to system RAM and stalls them together,
    so the translator sizes itself for the voice model that still has to load."""
    return None if free_bytes is None else max(0, free_bytes - reserve_bytes)


def mt_ct2_compute(free_bytes: int | None, model_bytes: int, bf16: bool = True) -> tuple[str, str | None]:
    """bfloat16 when the converted weights fit with headroom, int8 weights otherwise; the
    int8 path is slower on a GPU but halves memory (measured on an RTX 4090)."""
    full = "bfloat16" if bf16 else "float16"
    if free_bytes is None or free_bytes >= model_bytes + 1024**3:
        return full, None
    if free_bytes >= int(model_bytes * 0.55) + 1024**3:
        return f"int8_{full}", "translation weights quantized to int8 to fit free GPU memory"
    return "cpu", "insufficient free GPU memory for translation; use --mt-device cuda to override"


def ct2_artifact(compute: str, derived: str, derived_int8: str | None) -> str:
    """The conversion to load for `compute`: the stored-int8 one for an int8 compute type
    when it exists, so CTranslate2 does not quantize the bfloat16 weights at load (37 s
    against 9 s on the reference PC), else the bfloat16 one."""
    return derived_int8 if derived_int8 and compute.startswith("int8") else derived


def wait_for_load(channel) -> None:
    """With --wait-load the host starts this worker beside the others, so its imports and
    tokenizer overlap theirs, and says when it may read free GPU memory: once recognition
    holds its own, exactly what this worker measured when the stages loaded one by one."""
    frame = channel.receive()
    if frame is None:
        os._exit(0)  # the host stopped during start-up
    if frame[0].get("op") != "load":
        raise RuntimeError("expected the host's load signal")


def load_translator(args, tokenizer, gate=lambda: None, sized=lambda: None):
    """Returns (translate(messages) -> (text, complete), device, note). CTranslate2 (fused
    kernels) when a converted artifact is available and the device is CUDA or CPU; PyTorch
    otherwise, including Apple MPS, where CTranslate2 has no backend. `gate` returns when
    this worker may size itself against free GPU memory; `sized` reports that it has, so
    the voice that loads after it may start claiming memory."""
    end_tokens = ["<|im_end|>", "<|endoftext|>"]
    max_new = 256

    def chat_ids(messages):
        ids = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
        return list(ids["input_ids"] if hasattr(ids, "keys") else ids)

    if args.derived and args.device in ("auto", "cuda", "cpu"):
        import ctranslate2

        gate()
        cuda = ctranslate2.get_cuda_device_count() > 0
        if args.device == "cuda" and not cuda:
            raise RuntimeError("CUDA requested but CTranslate2 cannot see a CUDA device")
        device, note = ("cuda", None) if cuda and args.device != "cpu" else ("cpu", None)
        if device == "cuda":
            bf16 = ctranslate2.get_cuda_compute_capability(0)[0] >= 8 if hasattr(ctranslate2, "get_cuda_compute_capability") else True
            free = free_after_reserve(cuda_free_bytes(), args.reserve_mb * 1024**2) if args.device == "auto" else None
            compute, note = mt_ct2_compute(free, (Path(args.derived) / "model.bin").stat().st_size, bf16)
            if compute == "cpu":
                device, compute = "cpu", "int8"
        else:
            compute = "int8"
        artifact = ct2_artifact(compute, args.derived, args.derived_int8)
        sized()
        generator = ctranslate2.Generator(artifact, device=device, compute_type=compute,
                                          intra_threads=0 if device == "cuda" else max(4, (os.cpu_count() or 8) // 2))

        def translate(messages):
            tokens = tokenizer.convert_ids_to_tokens(chat_ids(messages))
            result = generator.generate_batch([tokens], max_length=max_new, sampling_topk=1,
                                              include_prompt_in_result=False, end_token=end_tokens)
            out = result[0].sequences_ids[0]
            return tokenizer.decode(out, skip_special_tokens=True).strip(), len(out) < max_new

        stored = " stored" if artifact != args.derived else ""
        note = "; ".join(x for x in (note, f"ctranslate2 {compute}{stored}") if x)
        return translate, device, note

    import torch
    from transformers import AutoModelForCausalLM

    note = None if args.derived else "PyTorch decoding (convert with a2a_runtime.models.derive for ~5x faster translation)"
    gate()
    if args.device == "auto":
        if torch.cuda.is_available():
            size = sum(p.stat().st_size for p in Path(args.path).glob("*.safetensors"))
            device, why = mt_auto_cuda(free_after_reserve(cuda_free_bytes(), args.reserve_mb * 1024**2), size)
            note = "; ".join(x for x in (note, why) if x) or None
        else:
            device = "mps" if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available() else "cpu"
    else:
        device = args.device
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("CUDA requested but PyTorch cannot see a CUDA device")
    if device == "mps" and not (getattr(torch.backends, "mps", None) and torch.backends.mps.is_available()):
        raise RuntimeError("MPS requested but PyTorch cannot see an MPS device")
    # bfloat16 where supported (Qwen3's training dtype; float16 can overflow), float16 on MPS.
    if device == "cuda":
        dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    else:
        dtype = torch.float16 if device == "mps" else torch.float32
    sized()
    model = AutoModelForCausalLM.from_pretrained(args.path, local_files_only=True, dtype=dtype).to(device).eval()
    eos = {tokenizer.convert_tokens_to_ids(t) for t in end_tokens}

    def translate(messages):
        inputs = torch.tensor([chat_ids(messages)], device=device)
        with torch.inference_mode():
            output = model.generate(inputs, attention_mask=torch.ones_like(inputs), max_new_tokens=max_new,
                                    do_sample=False)
        generated = output[0, inputs.shape[-1]:]
        complete = len(generated) < max_new or (len(generated) and int(generated[-1]) in eos)
        return tokenizer.decode(generated, skip_special_tokens=True).strip(), bool(complete)

    note = "; ".join(x for x in (note, str(dtype).replace("torch.", "")) if x)
    return translate, device, note


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("asr", "mt"))
    parser.add_argument("--path", required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--debug-errors", action="store_true")
    parser.add_argument("--derived", default=None, help="verified CTranslate2 conversion of --path (MT)")
    parser.add_argument("--derived-int8", default=None,
                        help="verified stored-int8 CTranslate2 conversion of --path, for int8 compute (MT)")
    parser.add_argument("--reserve-mb", type=int, default=0,
                        help="GPU memory to leave for workers that load after this one (device auto)")
    parser.add_argument("--wait-load", action="store_true",
                        help="MT: wait for the host's load frame before sizing against GPU memory, and "
                             "report `sized` once sized (the stages start together)")
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
            threads = 0 if device == "cuda" else max(4, (os.cpu_count() or 8) // 2)
            compute = ASR_CUDA_COMPUTE if device == "cuda" else "int8"
            model = WhisperModel(args.path, device=device, compute_type=compute, local_files_only=True,
                                 cpu_threads=threads)
            prompt = ASR_PROMPTS.get(args.source)
            note = (note + "; " if note else "") + compute
            segments, _ = model.transcribe(np.zeros(16_000, dtype=np.float32), language=args.source, beam_size=1,
                                           condition_on_previous_text=False, vad_filter=False)
            list(segments)
        else:
            # The tokenizer always comes from the verified source checkpoint's tokenizer.json.
            # AutoTokenizer resolves Qwen2Tokenizer through modules that import torch, which
            # the CTranslate2 path never uses and which took 5-30 s to load on Windows; this
            # class gives the same ids, tokens, and decodes for the chat template.
            from transformers import PreTrainedTokenizerFast

            tokenizer = PreTrainedTokenizerFast.from_pretrained(args.path, local_files_only=True)
            gate = (lambda: wait_for_load(channel)) if args.wait_load else (lambda: None)
            sized = (lambda: channel.send({"ev": "sized"})) if args.wait_load else (lambda: None)
            translate, device, note = load_translator(args, tokenizer, gate, sized)
            # A realistic warm-up allocates the cache and selects kernels before the first unit.
            translate([{"role": "user", "content": "我们下周开会。"}])
        channel.send({"ev": "ready", "device": device, "note": note})
        # A reader thread keeps the protocol responsive while a model call runs: requests queue
        # in order, and a cancel removes a queued translation before it starts.
        import queue
        import threading

        requests: queue.Queue = queue.Queue()
        canceled: set = set()
        lock = threading.Lock()

        def read() -> None:
            while (frame := channel.receive()) is not None:
                if frame[0].get("op") == "cancel":
                    with lock:
                        canceled.add(frame[0].get("id"))
                else:
                    requests.put(frame)
            os._exit(0)  # the host closed stdin or died: never leave a model resident

        threading.Thread(target=read, daemon=True).start()
        while True:
            request, payload = requests.get()
            with lock:
                if request.get("id") in canceled:
                    canceled.discard(request.get("id"))
                    continue
            try:
                if args.stage == "asr" and request.get("op") == "lid":
                    audio = np.frombuffer(payload, dtype="<f4")
                    # Language detection needs no transcript; the generator is intentionally not consumed.
                    _segments, info = model.transcribe(audio, language=None, beam_size=1,
                                                       condition_on_previous_text=False, vad_filter=False)
                    channel.send({"ev": "lid", "id": request["id"], "language": info.language,
                                  "probability": float(info.language_probability)})
                elif args.stage == "asr":
                    audio = np.frombuffer(payload, dtype="<f4")
                    # Partials and finals decode identically: a committed prefix must survive into
                    # the final, and greedy partials with beam-search finals disagree on numbers
                    # ("20%" vs "百分之二十"). Beam search on a GPU, greedy on the CPU; no
                    # temperature fallback, which would make the hypotheses non-deterministic.
                    options = dict(beam_size=ASR_BEAM if device == "cuda" else 1, temperature=0.0)
                    segments, _ = model.transcribe(audio, language=args.source, word_timestamps=True,
                                                   condition_on_previous_text=False, vad_filter=False,
                                                   initial_prompt=prompt, **options)
                    words = []
                    for segment in segments:
                        if hallucinated(segment):
                            continue
                        if segment.words:
                            words.extend({"text": w.word, "start": w.start, "end": w.end} for w in segment.words)
                        else:
                            words.append({"text": segment.text, "start": segment.start, "end": segment.end})
                    channel.send({"ev": "result", "id": request["id"], "words": words})
                else:
                    context = request.get("context", [])
                    glossary = request.get("glossary", [])
                    instruction = (f"You are a simultaneous interpreter. Translate the user's {args.source} speech "
                                   f"into natural spoken {args.target}. Return only the translation of the final "
                                   "message: no notes, quotes, or explanations. Preserve meaning, negation, names, "
                                   "and numbers exactly. Earlier turns are context; never repeat them.")
                    messages = [{"role": "system", "content": instruction}]
                    for pair in context[-4:]:
                        messages.extend([{"role": "user", "content": pair[0]}, {"role": "assistant", "content": pair[1]}])
                    if glossary:
                        messages.append({"role": "system", "content": "Terminology: " + "; ".join(f"{a} = {b}" for a, b in glossary)})
                    messages.append({"role": "user", "content": request["text"]})
                    text, complete = translate(messages)
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
