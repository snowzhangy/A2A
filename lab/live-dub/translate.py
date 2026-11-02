"""Dubbing translation with a local LLM (mlx-lm): context, series glossary, speaker, and a time budget.

Each request carries the line's Mandarin, who says it, how long the actor speaks, recent lines (with the
English already chosen), and, when the lookahead delay has already heard it, the next line. The model
writes one spoken English line sized to the actor's time (about 4 syllables per second, the pace of
dub-anything's directed script on this show).

usage: python translate.py --model qwen3.5-9b --script goal   (translates the 12 lines of the goal scene)
"""
from __future__ import annotations

import argparse
import json
import re
import time

from common import ACTORS, RUNS, write_json

LLMS = {
    "qwen3-4b": "mlx-community/Qwen3-4B-Instruct-2507-4bit",
    "qwen3.5-9b": "mlx-community/Qwen3.5-9B-MLX-4bit",
    "qwen3-30b-a3b": "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit",
}
SYLLABLES_PER_SECOND = 4.0

SYSTEM = """You are the dubbing translator for a Mandarin TV drama being dubbed into English live.
Write the English line an actor will speak over the original's time slot.
- Natural, idiomatic spoken English that a viewer understands at once; keep tone, register and intent.
- Fit the time: aim for the requested syllable count (within about 15%). Shorter lines: drop filler,
  not meaning. Longer slots: keep the full meaning; do not pad with new content.
- Use the glossary spellings for names and terms exactly.
- Mandarin particles and interjections (啊, 呢, 哼) become English ones only when they carry emotion.
- The Mandarin comes from speech recognition. 他, 她 and 它 sound identical, so choose he/she/it from
  context (who is being talked about), not from the character; other homophone slips are possible.
- Output only the English line, no quotes, notes or alternatives."""


def syllables(text: str) -> int:
    """Rough English syllable count (vowel groups per word, minimum one)."""
    n = 0
    for word in re.findall(r"[A-Za-z']+", text):
        groups = re.findall(r"[aeiouy]+", word.lower())
        k = len(groups) - (word.lower().endswith("e") and len(groups) > 1 and not word.lower().endswith("le"))
        n += max(1, k)
    return n


def load_series(series: str) -> dict:
    path = ACTORS / series / "series.json"
    return json.loads(path.read_text()) if path.exists() else {"title": "", "glossary": {}, "characters": {}}


class Translator:
    """One dubbing translator per session.

    With `kv_reuse` (default), the session is a running chat: each line is a user turn carrying its
    Mandarin, speaker, slot and glossary hits, and the chosen English is the assistant turn. The model's
    KV cache holds the conversation, so a new line only prefills its own turn (about 60-100 tokens)
    instead of the whole prompt. Past `max_turns` the oldest turns are dropped in one step and the cache
    is rebuilt once. Without it, every line gets one self-contained prompt listing the previous lines.
    """

    def __init__(self, model: str, series: dict | None = None, context_lines: int = 6,
                 syllables_per_second: float = SYLLABLES_PER_SECOND, kv_reuse: bool = True, max_turns: int = 16,
                 strict: bool = False):
        from mlx_lm import load
        self.name = model
        self.model, self.tok = load(LLMS[model])
        self.series = series or {"title": "", "glossary": {}, "characters": {}}
        self.context_lines = context_lines
        self.rate = syllables_per_second
        self.kv_reuse, self.max_turns = kv_reuse, max_turns
        self.strict = strict  # catch-up mode: the budget is a ceiling; condense like a subtitle
        self.history: list[dict] = []
        self.cache, self.cached = None, []

    def _system(self) -> str:
        s = self.series
        head = [SYSTEM]
        if s.get("title"):
            head.append(f"Show: {s['title']}")
        if s.get("synopsis"):
            head.append(f"Scene notes: {s['synopsis']}")
        return "\n".join(head)

    def _turn(self, zh: str, seconds: float, speaker: str | None) -> str:
        used = {k: v for k, v in self.series.get("glossary", {}).items() if k in zh}
        target = max(1, round(seconds * self.rate))
        budget = f"at most {target} syllables" if self.strict else f"about {target} syllables"
        out = [f"[{speaker or 'unidentified speaker'}, {seconds:.1f} s, {budget}] {zh}"]
        if used:
            out.append("Glossary: " + "; ".join(f"{k} = {v}" for k, v in used.items()))
        return "\n".join(out)

    def _messages(self, zh: str, seconds: float, speaker: str | None, next_zh: str | None) -> list[dict]:
        s = self.series
        lines = []
        if s.get("title"):
            lines.append(f"Show: {s['title']}")
        if s.get("synopsis"):
            lines.append(f"Scene notes: {s['synopsis']}")
        used = {k: v for k, v in s.get("glossary", {}).items()
                if k in zh or any(k in h["zh"] for h in self.history[-self.context_lines:]) or (next_zh and k in next_zh)}
        if used:
            lines.append("Glossary: " + "; ".join(f"{k} = {v}" for k, v in used.items()))
        if self.history:
            lines.append("Previous lines:")
            for h in self.history[-self.context_lines:]:
                lines.append(f"  [{h['speaker'] or '?'}] {h['zh']} => {h['en']}")
        target = max(1, round(seconds * self.rate))
        lines.append(f"Line to dub, spoken by {speaker or 'an unidentified character'} over {seconds:.1f} s "
                     f"(about {target} syllables):")
        lines.append(f"  {zh}")
        if next_zh:
            lines.append(f"Next line (context only, do not translate): {next_zh}")
        return [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "\n".join(lines)}]

    def _chat(self, zh: str, seconds: float, speaker: str | None) -> list[dict]:
        rule = ("\nThe dub runs live, just behind the picture: the syllable count is a hard ceiling. Condense like"
                " a good subtitle (keep who, what, and the emotion; drop filler, repetition and politeness"
                " padding); never exceed it." if self.strict else "")
        msgs = [{"role": "system", "content": self._system() + rule +
                 "\nEach user turn is the next line of the scene; reply with its English line only."}]
        for h in self.history:
            msgs += [{"role": "user", "content": h["turn"]}, {"role": "assistant", "content": h["en"] or "..."}]
        return msgs + [{"role": "user", "content": self._turn(zh, seconds, speaker)}]

    def _generate_cached(self, msgs: list[dict]) -> str:
        from mlx_lm import stream_generate
        from mlx_lm.models.cache import make_prompt_cache, trim_prompt_cache
        from mlx_lm.sample_utils import make_sampler
        kwargs = {"enable_thinking": False} if "qwen3.5" in self.name else {}
        text = self.tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False, **kwargs)
        full = self.tok.encode(text, add_special_tokens=False)
        if self.cache is None or full[:len(self.cached)] != self.cached:
            self.cache, self.cached = make_prompt_cache(self.model), []
        suffix = full[len(self.cached):]
        out = []
        for r in stream_generate(self.model, self.tok, suffix, prompt_cache=self.cache, max_tokens=80,
                                 sampler=make_sampler(temp=0.0)):
            out.append(r.text)
        # Drop the generated tokens from the cache; the template re-renders the reply next turn.
        extra = self.cache[0].offset - len(full)
        if extra > 0:
            trim_prompt_cache(self.cache, extra)
        self.cached = full
        return "".join(out)

    def __call__(self, zh: str, seconds: float, speaker: str | None = None, next_zh: str | None = None) -> dict:
        from mlx_lm import generate
        from mlx_lm.sample_utils import make_sampler
        t = time.perf_counter()
        if self.kv_reuse:
            if len(self.history) >= self.max_turns:
                self.history = self.history[-self.context_lines:]
            en = self._generate_cached(self._chat(zh, seconds, speaker))
        else:
            msgs = self._messages(zh, seconds, speaker, next_zh)
            kwargs = {"enable_thinking": False} if "qwen3.5" in self.name else {}
            prompt = self.tok.apply_chat_template(msgs, add_generation_prompt=True, tokenize=False, **kwargs)
            en = generate(self.model, self.tok, prompt, max_tokens=80, sampler=make_sampler(temp=0.0))
        dt = time.perf_counter() - t
        en = en.strip().strip('"').splitlines()[0].strip() if en.strip() else ""
        self.history.append({"zh": zh, "en": en, "speaker": speaker, "turn": self._turn(zh, seconds, speaker)})
        return {"en": en, "latency_s": round(dt, 3), "syllables": syllables(en),
                "target_syllables": max(1, round(seconds * self.rate))}


def goal_script() -> list[dict]:
    d = json.load(open("/Users/yangzhang/Documents/github/Research/dub-anything/work/condor-01/script.json"))
    return d["lines"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3.5-9b", choices=sorted(LLMS))
    ap.add_argument("--series", default="condor")
    ap.add_argument("--no-lookahead", action="store_true")
    ap.add_argument("--no-kv", action="store_true", help="one self-contained prompt per line (no cache reuse)")
    ap.add_argument("--syl-rate", type=float, default=SYLLABLES_PER_SECOND)
    ap.add_argument("--strict", action="store_true", help="syllable budget is a ceiling (catch-up mode)")
    a = ap.parse_args()
    lines = goal_script()
    tr = Translator(a.model, load_series(a.series), syllables_per_second=a.syl_rate, kv_reuse=not a.no_kv, strict=a.strict)
    tr("你好", 1.0)  # warm-up
    tr.history.clear()
    names = load_series(a.series).get("characters", {})
    out = []
    for i, l in enumerate(lines):
        nxt = None if a.no_lookahead or i + 1 >= len(lines) else lines[i + 1]["zh"]
        r = tr(l["zh"], l["end"] - l["start"], names.get(l["voice"], l["voice"]), nxt)
        out.append({"zh": l["zh"], "seconds": round(l["end"] - l["start"], 2), "goal": l["text"],
                    "goal_syllables": syllables(l["text"]), **r})
        print(f"{r['latency_s']:.2f}s {r['syllables']:>2}/{r['target_syllables']:<2} {r['en']}   || {l['text']}")
    write_json(RUNS / f"mt-goal-{a.model}{'-nolook' if a.no_lookahead else ''}{'-nokv' if a.no_kv else '-kv'}-r{a.syl_rate:g}.json", out)
    lat = sorted(o["latency_s"] for o in out)
    fit = [o["syllables"] / o["target_syllables"] for o in out]
    print({"model": a.model, "latency_p50": lat[len(lat) // 2], "latency_max": lat[-1],
           "syllable_ratio_p50": round(sorted(fit)[len(fit) // 2], 2)})


if __name__ == "__main__":
    main()
