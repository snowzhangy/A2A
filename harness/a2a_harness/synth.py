"""Synthetic contract fixtures.

Generates speech-like tone bursts (one per Mandarin character), an exact reference
alignment, scripted ASR hypotheses that lag the audio like a streaming recognizer, and
a clause-level MT dictionary. No human speech or voice data is involved, so these
fixtures may live in git and run in hosted CI. They test plumbing and timing, never
model quality.

    uv run --project harness python -m a2a_harness.synth --out fixtures/synthetic --sentences 4

`--utterance-sentences N` models a recognizer whose utterances span several sentences: VAD
stays open over music or continuous talk, sentence terminators inside the group appear only in
the final result (recognition adds punctuation late), and nothing finalizes until the group
ends. That is the pattern behind the long units measured in reports/week2/dub-anything-review.md,
so it is the fixture for the commit policy's source-time budget.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from . import CANONICAL_RATE
from .audio import write_wav

# (clauses in Mandarin, matching English clauses). Numbers, dates, negation, and names
# exercise the commit-policy holds.
SENTENCES: list[tuple[list[str], list[str]]] = [
    (["我们下周开会。"], ["We will meet next week."]),
    (["这个方案我们不同意，", "因为成本太高。"], ["We do not agree with this plan,", "because the cost is too high."]),
    (["价格是三点五万元。"], ["The price is 35,000 yuan."]),
    (["请在三月二十号之前，", "把报告发给我。"], ["Before March 20,", "please send me the report."]),
    (["好的。"], ["Okay."]),
    (["张经理明天不在办公室。"], ["Manager Zhang will not be in the office tomorrow."]),
    (["我们已经完成了测试，", "但是还要检查一下数据。"], ["We have finished testing,", "but we still need to check the data."]),
    (["会议改到下午两点。"], ["The meeting has moved to 2 p.m."]),
    (["没有问题。"], ["No problem."]),
    (["谢谢大家，", "我们下次再聊。"], ["Thank you, everyone,", "let's talk next time."]),
]

PUNCTUATION = set("，。！？；、,.!?;")


@dataclass
class SynthConfig:
    sentences: int = 10
    repeat_to_seconds: float | None = None
    char_ms: float = 220.0
    gap_ms: float = 30.0
    pause_ms: float = 800.0
    lead_ms: float = 500.0
    tail_ms: float = 1000.0
    asr_lag_ms: float = 150.0
    partial_every_ms: float = 200.0
    final_delay_ms: float = 350.0
    token_timing: bool = True
    seed: int = 7
    fixture_id: str = "synthetic-zh"
    # Sentences per recognizer utterance; 1 finalizes every sentence.
    utterance_sentences: int = 1


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _clause_ranges(n: int):
    for i in range(n):
        for j in range(i + 1, n + 1):
            yield i, j


def generate(out_dir: Path, cfg: SynthConfig = SynthConfig()) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(cfg.seed)
    rate = CANONICAL_RATE

    chosen = [SENTENCES[i % len(SENTENCES)] for i in range(cfg.sentences)]
    if cfg.repeat_to_seconds:
        est = sum(
            (sum(len(c) for c in zh) * (cfg.char_ms + cfg.gap_ms) + cfg.pause_ms) / 1000 for zh, _ in SENTENCES
        ) / len(SENTENCES)
        count = max(cfg.sentences, int(cfg.repeat_to_seconds / est) + 1)
        chosen = [SENTENCES[i % len(SENTENCES)] for i in range(count)]

    t = cfg.lead_ms / 1000
    units = []
    hypotheses = []
    dictionary: dict[str, str] = {}
    segments: list[tuple[float, float, float]] = []  # (start, end, f0)

    for utt, group in enumerate(_chunks(chosen, max(1, cfg.utterance_sentences)), 1):
        # One recognizer utterance per group: hypotheses keep growing across sentence
        # boundaries and finalize only after the last sentence, and the terminators of the
        # earlier sentences (marked `late`) are withheld from partials.
        chars = []
        for position, (zh_clauses, en_clauses) in enumerate(group):
            for i, j in _clause_ranges(len(zh_clauses)):
                dictionary["".join(zh_clauses[i:j])] = " ".join(en_clauses[i:j])
            text = "".join(zh_clauses)
            sentence = []
            f0 = float(rng.uniform(110, 220))
            for ch in text:
                if ch in PUNCTUATION:
                    # Punctuation has no audio; it attaches to the end of the previous character.
                    prev_end = (sentence or chars)[-1]["end"] if (sentence or chars) else t
                    entry = {"c": ch, "start": round(prev_end, 4), "end": round(prev_end, 4)}
                    if position + 1 < len(group) and ch in "。！？":
                        entry["late"] = True
                    sentence.append(entry)
                    continue
                start, end = t, t + cfg.char_ms / 1000
                segments.append((start, end, f0 * float(rng.uniform(0.9, 1.1))))
                sentence.append({"c": ch, "start": round(start, 4), "end": round(end, 4)})
                t = end + cfg.gap_ms / 1000
            chars.extend(sentence)
            units.append({"text": text, "translation": " ".join(en_clauses), "chars": sentence})

        # Streaming hypotheses: characters whose audio ended at least asr_lag ago.
        utterance_end = chars[-1]["end"]
        first = chars[0]["start"]
        h_t = first + cfg.partial_every_ms / 1000
        while h_t < utterance_end + cfg.final_delay_ms / 1000:
            visible = [c for c in chars if c["end"] <= h_t - cfg.asr_lag_ms / 1000]
            if visible:
                hypotheses.append(_hyp(visible, utt, h_t, final=False, timing=cfg.token_timing))
            h_t += cfg.partial_every_ms / 1000
        hypotheses.append(_hyp(chars, utt, utterance_end + cfg.final_delay_ms / 1000, final=True, timing=cfg.token_timing))
        t = utterance_end + cfg.pause_ms / 1000

    duration = t + cfg.tail_ms / 1000
    audio = _render(segments, duration, rate, rng)

    stem = cfg.fixture_id
    write_wav(out_dir / f"{stem}.wav", audio, rate)
    (out_dir / f"{stem}.asr.json").write_text(
        json.dumps({"hypotheses": hypotheses}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    (out_dir / f"{stem}.mt.json").write_text(
        json.dumps(dictionary, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest = {
        "id": stem,
        "kind": "synthetic",
        "license": "Synthetic tone bursts generated by a2a_harness.synth; no speech or voice data.",
        "audio": f"{stem}.wav",
        "sample_rate": rate,
        "duration_s": round(duration, 4),
        "warm_interval_s": [0.0, round(duration, 4)],
        "asr_script": f"{stem}.asr.json",
        "mt_dictionary": f"{stem}.mt.json",
        "alignment_uncertainty_ms": 0.0,
        "generator": {"module": "a2a_harness.synth", **{k: v for k, v in cfg.__dict__.items()}},
        "reference": {"units": units},
    }
    path = out_dir / f"{stem}.yaml"
    path.write_text(yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _hyp(chars: list[dict], utt: int, at: float, *, final: bool, timing: bool) -> dict:
    if not final:
        chars = [c for c in chars if not c.get("late")]
    text = "".join(c["c"] for c in chars)
    entry = {"at_sample": int(round(at * CANONICAL_RATE)), "utt": utt, "text": text, "final": final}
    if timing:
        entry["tokens"] = [
            {"text": c["c"], "start": int(round(c["start"] * CANONICAL_RATE)), "end": int(round(c["end"] * CANONICAL_RATE))}
            for c in chars
        ]
    return entry


def _render(segments, duration: float, rate: int, rng: np.random.Generator) -> np.ndarray:
    n = int(round(duration * rate))
    # Low noise floor: no digital silence, well under the VAD threshold.
    audio = (rng.standard_normal(n) * 10 ** (-72 / 20)).astype(np.float32)
    fade = int(0.01 * rate)
    for start, end, f0 in segments:
        a, b = int(round(start * rate)), int(round(end * rate))
        tt = np.arange(b - a) / rate
        burst = sum((0.5 / k) * np.sin(2 * np.pi * f0 * k * tt) for k in range(1, 6))
        burst = burst + 0.05 * rng.standard_normal(b - a)
        env = np.ones(b - a)
        env[:fade] = np.linspace(0, 1, fade)
        env[-fade:] = np.linspace(1, 0, fade)
        audio[a:b] += (0.12 * burst * env).astype(np.float32)
    return audio


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--sentences", type=int, default=10)
    ap.add_argument("--minutes", type=float, help="repeat sentences to roughly this duration")
    ap.add_argument("--id", default="synthetic-zh")
    ap.add_argument("--no-token-timing", action="store_true")
    ap.add_argument("--utterance-sentences", type=int, default=1,
                    help="sentences per recognizer utterance; partials cross sentence boundaries "
                         "and nothing finalizes until the group ends")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    cfg = SynthConfig(
        sentences=args.sentences,
        repeat_to_seconds=args.minutes * 60 if args.minutes else None,
        token_timing=not args.no_token_timing,
        seed=args.seed,
        fixture_id=args.id,
        utterance_sentences=args.utterance_sentences,
    )
    print(generate(args.out, cfg))


if __name__ == "__main__":
    main()
