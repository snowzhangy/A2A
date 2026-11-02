"""Resident research translator. Bounded JSONL over stdio; verified local weights only."""
import argparse
import json
import os
import sys
import time
import threading
from pathlib import Path

PROTOCOL = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr
from context_translation import ROOT, SYSTEM, payload_for


def send(value):
    PROTOCOL.write(json.dumps(value, ensure_ascii=False) + "\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--fake", action="store_true")
    p.add_argument("--series", type=Path)
    p.add_argument("--fake-gate", type=Path, help="test-only: hold fake responses until this file exists")
    p.add_argument("--fake-startup-gate", type=Path, help="test-only: hold fake startup before stdin is read")
    a = p.parse_args()
    # The host can be killed while Metal is still loading/warming the model, before
    # the stdin loop starts. Never leave a large orphaned worker in that case.
    parent = os.getppid()
    def watch_parent():
        while True:
            time.sleep(0.2)
            if os.getppid() != parent:
                os._exit(0)
    threading.Thread(target=watch_parent, daemon=True).start()
    if a.fake:
        while a.fake_startup_gate and not a.fake_startup_gate.exists():
            time.sleep(0.01)
    series = {}
    if a.series:
        a.series.resolve().relative_to((ROOT / ".local").resolve())
        series = json.loads(a.series.read_text())
    if not a.fake:
        from voice_clone import verified_model
        from mlx_lm import load, generate
        from mlx_lm.sample_utils import make_sampler
        path, _ = verified_model("qwen3-30b-a3b-instruct-2507-mlx-4bit")
        model, tokenizer = load(str(path))
        sampler = make_sampler(temp=0.0)
        warm = tokenizer.apply_chat_template([{"role": "user", "content": "Translate 你好 into English."}],
                                            tokenize=False, add_generation_prompt=True)
        generate(model, tokenizer, warm, max_tokens=8, sampler=sampler, verbose=False)
    send(dict(ev="ready"))
    while True:
        line = sys.stdin.buffer.readline(65537)
        if not line:
            return
        if len(line) > 65536:
            raise ValueError("request exceeds 64KB")
        request = json.loads(line)
        if request.get("op") == "quit":
            return
        job = int(request["id"])
        try:
            text = request["source"]
            if not isinstance(text, str) or not 1 <= len(text) <= 4096:
                raise ValueError("invalid source length")
            history = request.get("context", [])[-8:]
            if a.fake:
                while a.fake_gate and not a.fake_gate.exists():
                    time.sleep(0.01)
                time.sleep(0.08)
                target = "Hello."
            else:
                metadata = dict(series)
                metadata["glossary"] = dict(series.get("glossary", {}), **request.get("glossary", {}))
                _, payload = payload_for(text, history, metadata)
                prompt = tokenizer.apply_chat_template([
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    tokenize=False, add_generation_prompt=True)
                target = generate(model, tokenizer, prompt, max_tokens=160, sampler=sampler, verbose=False).strip()
            if not target or len(target.encode()) > 32768:
                raise ValueError("invalid output length")
            send(dict(ev="result", id=job, text=target))
        except Exception as e:
            send(dict(ev="error", id=job, message=type(e).__name__))


if __name__ == "__main__":
    main()
