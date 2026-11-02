"""Latency benchmarks for the three GPU stages, on real utterances of a real scene.

The whole design hangs on these numbers: an utterance is only worth dubbing live if
recognition + translation + the first audio chunk of the clone land inside the delay budget.
Everything is measured per utterance, on the *mixed* source (live capture has no separated
vocals), paced one call after another with the models kept warm.

    python bench_stages.py asr [--model sensevoice|qwen3-asr-0.6b|qwen3-asr-1.7b]
    python bench_stages.py mt  [--model a3b|9b]
    python bench_stages.py tts [--engine qwen|omnivoice]

Writes .local/livedub/bench/<stage>-<model>.json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import time

import numpy as np

import paths
from paths import ASR_RATE, OUT, load
from segmenter import Segmenter

SCENE = (583.0, 620.0)          # the scene the offline dub was directed on
EPISODE = paths.LAB / "input/ep08-16k.wav"
ASR_REPOS = {
    "sensevoice": "mlx-community/SenseVoiceSmall",
    "qwen3-asr-0.6b": "mlx-community/Qwen3-ASR-0.6B-8bit",
    "qwen3-asr-1.7b": "mlx-community/Qwen3-ASR-1.7B-8bit",
}
MT_REPOS = {
    "a3b": "mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit",
    "9b": "mlx-community/Qwen3.5-9B-MLX-4bit",
}
BENCH = OUT / "bench"
PARTICLE = re.compile(r"[啊呢呐吧呀哦嘛哈嗯额]|\s")


def utterances() -> list:
    audio = load(EPISODE, *SCENE, ASR_RATE)
    seg = Segmenter()
    out = []
    for i in range(0, len(audio), 320):
        out += seg.push(audio[i:i + 320])
    return out + seg.finish()


def record(stage: str, key: str, obj) -> None:
    BENCH.mkdir(parents=True, exist_ok=True)
    p = BENCH / f"{stage}-{key}.json"
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1))
    print(f"\nwrote {p}")


def cer(ref: str, hyp: str) -> float:
    try:
        import opencc
        ref = opencc.OpenCC("t2s").convert(ref)
    except Exception:
        pass
    r, h = PARTICLE.sub("", ref), PARTICLE.sub("", hyp)
    if not r:
        return float("nan")
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i] + [0] * len(h)
        for j, hc in enumerate(h, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc))
        prev = cur
    return prev[-1] / len(r)


def subtitle_text(start: float, end: float) -> str:
    fx = paths.REPO / ".local/fixtures/youtube-NRB0ICxMkrM.zh-HK-bNc5iLxGGqE.json3"
    if not fx.exists():
        return ""
    import opencc
    d = json.loads(fx.read_text())
    out = []
    for e in d.get("events", []):
        if "segs" not in e:
            continue
        t = e["tStartMs"] / 1000
        if start <= t < end:
            out.append(opencc.OpenCC("t2s").convert(
                "".join(s.get("utf8", "") for s in e["segs"]).replace("\n", "")))
    return "".join(out)


def bench_asr(model: str) -> None:
    utts = utterances()
    print(f"{len(utts)} utterances, {sum(u.duration for u in utts):.1f} s of speech")
    from mlx_audio.stt import load as load_stt
    t0 = time.perf_counter()
    rec = load_stt(ASR_REPOS[model])
    load_s = time.perf_counter() - t0
    rows, src = [], 0.0
    hot = ["全真教", "小龙女"]
    for u in utts:
        t = time.perf_counter()
        try:
            if model.startswith("qwen3-asr"):
                text = (rec.generate(u.samples, language="Chinese", hotwords=hot).text or "").strip()
            else:
                text = (rec.generate(u.samples, language="zh").text or "").strip()
        except TypeError:      # no hotword support in this signature
            text = (rec.generate(u.samples, language="zh").text or "").strip()
        dt = time.perf_counter() - t
        src += u.duration
        rows.append({"i": u.index, "dur": round(u.duration, 2), "wall": round(dt, 3),
                     "rtf": round(dt / u.duration, 3), "zh": text})
        print(f"  {u.index:2d} {u.duration:4.2f}s audio  {dt:5.3f}s wall  {text}")
    wall = sum(r["wall"] for r in rows)
    ref = subtitle_text(*SCENE)
    hyp = "".join(r["zh"] for r in rows)
    summary = {"model": model, "load_s": round(load_s, 2), "utterances": len(rows),
               "speech_s": round(src, 2), "wall_s": round(wall, 2), "rtf": round(wall / src, 3),
               "p50": round(float(np.median([r["wall"] for r in rows])), 3),
               "p90": round(float(np.percentile([r["wall"] for r in rows], 90)), 3),
               "max": round(max(r["wall"] for r in rows), 3),
               "cer_vs_subtitles": round(cer(ref, hyp), 4), "rows": rows}
    print(f"\n{model}: load {load_s:.1f}s  RTF {summary['rtf']}  wall/utt p50 {summary['p50']} "
          f"p90 {summary['p90']} max {summary['max']}  CER {summary['cer_vs_subtitles']}")
    record("asr", model.replace("/", "_"), summary)


def bench_mt(model: str, engine: str = "a3b") -> None:
    # Feed it what the recogniser actually returns at the pace the pipeline would: whole
    # utterances, not tidied subtitle sentences.
    chunks = []
    for name in ("asr-sensevoice.json", "asr-qwen3-asr-0.6b.json"):
        p = BENCH / name
        if p.exists():
            chunks = [r["zh"] for r in json.loads(p.read_text())["rows"] if len(r["zh"]) >= 3]
            break
    if not chunks:
        chunks = [c for c in re.split(r"[。？！]", subtitle_text(*SCENE)) if len(c) >= 3]
    from mlx_lm import load, stream_generate
    t0 = time.perf_counter()
    mlx, tok = load(str(paths.model_from_cache(MT_REPOS[model])))
    load_s = time.perf_counter() - t0
    print(f"loaded {model} in {load_s:.1f}s, {len(chunks)} lines")
    rows = []
    context: list[str] = []
    for src in chunks:
        budget = max(4, int(len(src) * 2.2))     # ~2 English words per Mandarin character at speed
        system = ("Translate the last line of a Mandarin dialogue into natural, speakable English "
                  "for voice-over. Keep names as in the glossary. "
                  f"Glossary: 全真教=Taoist sect, 小龙女=Dragon Maiden, 姑姑=Auntie. "
                  f"Use at most {budget} words. Reply with the translation only.\n"
                  + ("Earlier lines:\n" + "\n".join(context[-3:]) if context else ""))
        prompt = [{"role": "system", "content": system}, {"role": "user", "content": src}]
        ids = tok.apply_chat_template(prompt, add_generation_prompt=True)
        t = time.perf_counter()
        first = None
        out = ""
        for piece in stream_generate(model=mlx, tokenizer=tok, prompt=ids, max_tokens=64):
            if first is None and piece.token:
                first = time.perf_counter() - t
            out += piece.text
        dt = time.perf_counter() - t
        out = out.strip().replace("\n", " ")
        rows.append({"zh": src, "en": out, "wall": round(dt, 3), "first_token": round(first or 0, 3),
                     "words": len(out.split()), "budget": budget})
        print(f"  {dt:5.3f}s first {first or 0:5.3f}s  {len(out.split()):2d}/{budget:2d}w  {src} -> {out}")
        context.append(f"{src} -> {out}")
    walls = [r["wall"] for r in rows]
    summary = {"model": model, "load_s": round(load_s, 2), "lines": len(rows),
               "p50": round(float(np.median(walls)), 3), "p90": round(float(np.percentile(walls, 90)), 3),
               "max": round(max(walls), 3),
               "first_p50": round(float(np.median([r["first_token"] for r in rows])), 3),
               "over_budget": sum(1 for r in rows if r["words"] > r["budget"]), "rows": rows}
    print(f"\n{model}: p50 {summary['p50']}s p90 {summary['p90']}s max {summary['max']}s "
          f"first-token p50 {summary['first_p50']}s over-budget {summary['over_budget']}/{len(rows)}")
    record("mt", model, summary)


def bench_tts(engine: str, ref_wav: str, ref_text: str, text: str) -> None:
    """The product-path engine: A2A's resident worker, streamed, one line at a time."""
    import soundfile as sf

    from ttsd import RATE, TtsWorker
    raw, sr = sf.read(ref_wav, dtype="float32", always_2d=True)
    ref = paths.resample(raw.mean(axis=1), sr, RATE)
    lines = [text, "Taoist priests.", "Hmph. All Taoists are scum.",
             "Waiter! Answer me: who exactly were those two men?"]
    log = open(OUT / "bench" / f"tts-{engine}.log", "w") if (OUT / "bench").exists() else None
    t0 = time.perf_counter()
    worker = TtsWorker(engine, temperature=0.4, log=log)
    ready = worker.wait_ready()
    prep = worker.prepare("a", ref, ref_text, language="chinese")
    rows = []
    for line in lines:
        pieces, t, first = [], time.perf_counter(), None
        for chunk in worker.synth("a", line):
            if first is None:
                first = time.perf_counter() - t
            pieces.append(chunk)
        wall = time.perf_counter() - t
        audio = np.concatenate(pieces) if pieces else np.zeros(0, np.float32)
        rows.append({"text": line, "first": round(first or 0, 3), "wall": round(wall, 3),
                     "audio_s": round(len(audio) / RATE, 2),
                     "rtf": round(wall / max(0.1, len(audio) / RATE), 2), **worker.stats})
        print(f"  first {first or 0:5.2f}s  total {wall:5.2f}s  audio {len(audio)/RATE:5.2f}s  {line}")
    if pieces:
        o = OUT / "bench" / f"tts-{engine}-sample.wav"
        o.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(o), np.concatenate(pieces), RATE, subtype="PCM_16")
    worker.close()
    firsts = [r["first"] for r in rows if r["first"]]
    summary = {"engine": engine, "load_s": ready.get("load_s"), "warmup_s": ready.get("warmup_s"),
               "prepare_s": prep.get("prepare_s"), "reference_s": prep.get("seconds"),
               "first_p50": round(float(np.median(firsts)), 3) if firsts else None,
               "first_max": round(max(firsts), 3) if firsts else None,
               "rtf_p50": round(float(np.median([r["rtf"] for r in rows])), 2), "rows": rows}
    print(f"{engine}: load {ready.get('load_s')}s warmup {ready.get('warmup_s')}s "
          f"prepare {prep.get('prepare_s')}s first-audio p50 {summary['first_p50']}s "
          f"max {summary['first_max']}s rtf p50 {summary['rtf_p50']}")
    record("tts", engine, summary)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["asr", "mt", "tts"])
    ap.add_argument("--model", default="", help="asr: model key; mt: a3b or 9b")
    ap.add_argument("--engine", default="qwen", help="tts: qwen or omnivoice")
    ap.add_argument("--ref", default="", help="tts: reference wav")
    ap.add_argument("--ref-text", default="", help="tts: what the reference says")
    ap.add_argument("--text", default="Taoist priests. All Taoists are scum.")
    args = ap.parse_args()
    if args.stage == "asr":
        bench_asr(args.model or "sensevoice")
    elif args.stage == "mt":
        bench_mt(args.model or "a3b")
    else:
        ref = args.ref or str(pathlib.Path.home() / "Documents/github/Research/dub-anything/"
                              "work/condor-01/refs/waiter.wav")
        bench_tts(args.engine, ref, args.ref_text or "他们是全真教。", args.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
