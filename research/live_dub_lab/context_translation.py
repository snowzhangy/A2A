"""Causal contextual MT on actual committed ASR, using only earlier committed lines.

This isolated experiment replays text events; it does not measure end-to-end live
latency or use a future transcript. Model artifacts must be verified in the manifest.
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "research"))
import voice_clone

MODEL = "qwen3-4b-instruct-2507-mlx-4bit"
SYSTEM = """Translate the CURRENT Mandarin speech fragment into natural spoken English.
The input is live ASR and can contain homophone mistakes. Use preceding dialogue and
the provided terminology to interpret likely recognition errors, subjects and pronouns.
Translate only CURRENT; never repeat preceding lines or invent missing dialogue.
Preserve meaning, negation, numbers, names, intent and tone. Keep it concise without
dropping facts. A fragment may remain a fragment. Return only the English speech,
without explanations, labels, quotes or alternatives.
Mandarin 他/她/它 are homophones; ASR spelling is not reliable gender evidence.
Resolve pronouns and omitted subjects using the preceding discourse. Preserve plural
referents across fragments. Use terminology only for the expression it actually names;
never substitute an unrelated glossary word. Write grammatical English. Keep track of
who is being described across fragments; do not switch a woman's pronouns to male
solely because the recognizer wrote 他 instead of 她."""


def payload_for(text, history, series):
    for wrong, canonical in sorted(series.get("asr_aliases", {}).items(), key=lambda p: -len(p[0])):
        text = text.replace(wrong, canonical)
    preceding = [dict(unit=h["unit"], source=h["source"]) for h in history[-8:]]
    relevant = text + " ".join(h["source"] for h in preceding)
    terms = {k: v for k, v in series.get("glossary", {}).items() if k in relevant}
    return text, {"terminology": terms, "preceding": preceding, "CURRENT": text}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trace", type=Path, required=True)
    p.add_argument("--series", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--model", choices=("4b", "30b"), default="4b")
    a = p.parse_args()
    for path in (a.trace, a.series, a.out):
        path.resolve().relative_to((ROOT / ".local").resolve())
    if a.out.exists():
        raise FileExistsError("choose a fresh output directory")
    a.out.mkdir(mode=0o700, parents=True)
    os.chmod(a.out, 0o700)
    model_id = MODEL if a.model == "4b" else "qwen3-30b-a3b-instruct-2507-mlx-4bit"
    path, entry = voice_clone.verified_model(model_id)
    from mlx_lm import load, generate
    from mlx_lm.sample_utils import make_sampler
    model, tokenizer = load(str(path))
    series = json.loads(a.series.read_text())
    with a.trace.open() as f:
        events = [json.loads(line) for line in f]
    sources = [e for e in events if e["ev"] == "commit.source"]
    apple = {e["unit"]: e.get("text") for e in events if e["ev"] == "commit.target"}
    history = []
    rows = []
    for source in sources:
        text, payload = payload_for(source["text"], history, series)
        messages = [{"role": "system", "content": SYSTEM}]
        messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False)})
        prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        start = time.perf_counter()
        target = generate(model, tokenizer, prompt, max_tokens=160,
                          sampler=make_sampler(temp=0.0), verbose=False).strip()
        elapsed = time.perf_counter() - start
        row = dict(unit=source["unit"], source=source["text"], corrected_source=text, apple=apple.get(source["unit"]),
                   contextual=target, latency_s=elapsed,
                   available_at_ns=source["t_ns"], context_units=[h["unit"] for h in history[-8:]])
        rows.append(row)
        history.append(dict(unit=source["unit"], source=text, target=target))
        (a.out / "results.json").write_text(json.dumps(dict(model=model_id,
            revision=entry["converted"]["revision"], causal=True,
            scope="isolated text-event replay, actual ASR, preceding context only", rows=rows),
            ensure_ascii=False, indent=2) + "\n")
        print(f"Translated {source['unit']} using {len(history[:-1][-8:])} earlier units in {elapsed:.2f}s", flush=True)


if __name__ == "__main__":
    main()
