"""Delivery instruments: does a dubbed line move the way the actor's line moved?

Ported from dub-anything's `scripts/register.py` (pitch, effort, level, arousal) and
`eval/score.py` (Spearman follow-through, noise floor), restricted to **numpy** so the
numbers come out next to the harness instead of on a GPU box. Their SQUIM quality score,
WeSpeaker likeness and Whisper accent confidence stay out: they need torch models, and
`research/speaker_similarity.py` is already circular on our own clones.

What this measures, and what it does not. Reference dubs were tuned by ear against these
numbers, and their finding is that a take that follows the actor's register sounds acted
while a take that sits at the actor's average sounds read. Arousal is therefore the headline
figure: pitch (semitones re 100 Hz), vocal effort (spectral tilt, 1–4 kHz against 100 Hz–1 kHz,
which rises as a speaker pushes their voice) and level, as robust z-scores **within the
actor's own lines**, so 0 is how that actor usually speaks, +1 is raised, -1 subdued. The
takes are scored on the actor's scale, not their own: the question is whether the English
reaches the heights and the quiet the actor did, and `dynamics` says directly whether the
dub is flatter than the actor (below 1) rather than merely uncorrelated.

Correlation is the wrong measure for one line and the right measure for a scene: below
`MIN_LINES` pairs the aggregate is reported as `insufficient_lines` rather than as a number.

    uv run --project harness python research/dub_delivery.py \
        --source .local/runs/a/source.wav --takes .local/runs/a/translated.wav \
        --units .local/runs/a/units.json [--json .local/runs/a/delivery.json]

    uv run --project harness python research/dub_delivery.py --from-run .local/runs/a [more runs...]

`units.json` is a list of `{"unit", "src_start", "src_end", "take_start", "take_end"}` in
seconds, and `--from-run` builds it from a run's `trace.jsonl` (`commit.source` spans and the
`play.start`/`play.end` output anchors) next to the run's own `source.wav` / `translated.wav`.
Compared A/B variants share one `units.json`, so the same lines are scored in both.
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

import numpy as np

SR, FRAME, HOP = 16_000, 1024, 256
FMIN, FMAX = 70, 500
MIN_LINE = 0.6          # s; shorter than this, the register cannot be measured
MIN_LINES = 4           # pairs below which a rank correlation is not a number
# A feature whose interquartile spread across an actor's lines is smaller than this is
# treated as constant, so a flat stretch of speech cannot invent a huge z-score.
FLOOR = {"pitch": 1.0, "effort": 1.0, "level": 1.5}
WEIGHT = {"pitch": 1.0, "effort": 1.0, "level": 0.5}
CANONICAL_RATE = 48_000


# MARK: Audio in

def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Mono float32 samples and their rate from a PCM16 / float32 WAV (stdlib-free, no scipy)."""
    blob = Path(path).read_bytes()
    if blob[:4] != b"RIFF" or blob[8:12] != b"WAVE":
        raise ValueError(f"{path} is not a WAV file")
    pos, fmt, chunks = 12, None, []
    while pos + 8 <= len(blob):
        size, = struct.unpack("<I", blob[pos + 4:pos + 8])
        tag = blob[pos:pos + 4]
        if tag == b"fmt ":
            code, channels, rate, _, _, bits = struct.unpack("<HHIIHH", blob[pos + 8:pos + 24])
            if code == 0xFFFE:  # extensible: the real format is in the sub-format
                code, = struct.unpack("<H", blob[pos + 34:pos + 36])
            fmt = (code, channels, rate, bits)
        elif tag == b"data":
            chunks.append(blob[pos + 8:pos + 8 + size])
        pos += 8 + size + (size & 1)
    code, channels, rate, bits = fmt
    data = b"".join(chunks)
    if code == 3 and bits == 32:
        x = np.frombuffer(data, dtype="<f4")
    elif code == 1 and bits == 16:
        x = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    elif code == 1 and bits == 24:
        raw = np.frombuffer(data, dtype=np.uint8).reshape(-1, 3)
        x = ((raw[:, 0].astype(np.int32) | raw[:, 1] << 8 | raw[:, 2] << 16) << 8 >> 8).astype(np.float32) / 8_388_608.0
    else:
        raise ValueError(f"{path}: unsupported WAV format {code=}/{bits=}")
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(x, dtype=np.float32), rate


def resample(x: np.ndarray, rate: int, target: int = SR) -> np.ndarray:
    """Linear resample. Good enough for a register measure; the pitch floor is 70 Hz."""
    if rate == target or len(x) == 0:
        return x
    out = np.arange(int(len(x) * target / rate), dtype=np.float64)
    return np.interp(out / target * rate, np.arange(len(x), dtype=np.float64), x).astype(np.float32)


def slice_speech(x: np.ndarray, start: float, end: float) -> np.ndarray:
    lo, hi = max(0, int(start * SR)), min(len(x), int(end * SR))
    return x[lo:hi]


# MARK: Register features (scripts/register.py)

def yin(frames: np.ndarray) -> np.ndarray:
    """Pitch per frame in Hz; NaN where the frame is not periodic enough to claim a pitch."""
    tmin, tmax = SR // FMAX, SR // FMIN
    w = FRAME - tmax
    r = np.fft.irfft(np.conj(np.fft.rfft(frames[:, :w], 2048)) * np.fft.rfft(frames, 2048), 2048)[:, : tmax + 1]
    cs = np.concatenate([np.zeros((len(frames), 1)), np.cumsum(frames ** 2, axis=1)], axis=1)
    taus = np.arange(tmax + 1)
    d = cs[:, w][:, None] + cs[:, taus + w] - cs[:, taus] - 2 * r
    cmnd = np.ones_like(d)
    cmnd[:, 1:] = d[:, 1:] * taus[1:] / np.maximum(np.cumsum(d[:, 1:], axis=1), 1e-12)
    f0 = np.full(len(frames), np.nan)
    for k, c in enumerate(cmnd[:, tmin:]):
        below = np.nonzero(c < 0.15)[0]
        if len(below):
            j = below[0]
            while j + 1 < len(c) and c[j + 1] < c[j]:
                j += 1
        else:
            j = int(np.argmin(c))
            if c[j] > 0.35:
                continue
        f0[k] = SR / (tmin + j)
    return f0


def features(x: np.ndarray) -> dict | None:
    """Register of a stretch of 16 kHz speech: pitch (semitones re 100 Hz), effort
    (spectral tilt, dB) and level (dBFS), taken over its loud frames."""
    if len(x) < FRAME + HOP * 8:
        return None
    n = 1 + (len(x) - FRAME) // HOP
    fr = x[np.arange(FRAME)[None] + HOP * np.arange(n)[:, None]].astype(np.float64)
    db = 10 * np.log10((fr ** 2).mean(axis=1) + 1e-10)
    loud = db > db.max() - 25
    f0 = yin(fr[loud])
    f0 = f0[~np.isnan(f0)]
    power = np.abs(np.fft.rfft(fr[loud] * np.hanning(FRAME), axis=1)) ** 2
    hz = np.fft.rfftfreq(FRAME, 1 / SR)
    tilt = 10 * np.log10(power[:, (hz >= 1000) & (hz < 4000)].sum()
                         / (power[:, (hz >= 100) & (hz < 1000)].sum() + 1e-12))
    return {"pitch": 12 * np.log2(np.median(f0) / 100) if len(f0) >= 5 else np.nan,
            "effort": float(tilt),
            "level": float(np.percentile(db, 95))}


def norms(feats: list[dict]) -> dict[str, tuple[float, float]]:
    """Median and robust spread (IQR/1.349, never below FLOOR) of each feature."""
    out = {}
    for key in FLOOR:
        v = np.array([f[key] for f in feats if not np.isnan(f[key])])
        if len(v) == 0:
            out[key] = (0.0, FLOOR[key])
            continue
        q1, med, q3 = np.percentile(v, [25, 50, 75])
        out[key] = (float(med), max(float((q3 - q1) / 1.349), FLOOR[key]))
    return out


def arousal(f: dict, nm: dict) -> float:
    parts = [(WEIGHT[k] * (f[k] - nm[k][0]) / nm[k][1], WEIGHT[k]) for k in FLOOR if not np.isnan(f[k])]
    return sum(p for p, _ in parts) / sum(w for _, w in parts) if parts else np.nan


def spearman(a, b) -> float:
    """Rank correlation, or NaN when there is no order to follow: fewer than MIN_LINES
    measurable pairs, or one side that never moves (a flat dub has no ranking at all)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    ok = ~(np.isnan(a) | np.isnan(b))
    a, b = a[ok], b[ok]
    if len(a) < MIN_LINES or np.std(a) < 1e-9 or np.std(b) < 1e-9:
        return float("nan")
    return float(np.corrcoef(np.argsort(np.argsort(a)), np.argsort(np.argsort(b)))[0, 1])


def floor_db(x: np.ndarray) -> float:
    """Quietest versus loudest 20 ms inside the speech (dB): the noise between words.
    A negative number this wide says the reference carried room tone or music into the clone."""
    n = len(x) // 320
    if n < 2:
        return float("nan")
    db = 10 * np.log10((x[: n * 320].reshape(n, 320) ** 2).mean(axis=1) + 1e-12)
    on = np.nonzero(db > db.max() - 40)[0]
    span = db[on[0]: on[-1] + 1] if len(on) else db
    return float(np.percentile(span, 10) - np.percentile(span, 95))


# MARK: Scoring

def actor_features(source: np.ndarray, units: list[dict]) -> tuple[list[dict | None], list[int]]:
    """The actor's register per line, and which lines are measurable at all."""
    src = [features(slice_speech(source, u["src_start"], u["src_end"]))
           if u["src_end"] - u["src_start"] >= MIN_LINE else None for u in units]
    return src, [i for i, f in enumerate(src) if f is not None]


def score(source: np.ndarray, takes: list[tuple[str, np.ndarray]], units: list[dict],
          with_norms: dict | None = None) -> dict:
    """Score one or more rendered variants against the same actor lines.

    `units` gives both windows in seconds. Pass `with_norms` from one call to the next when the
    variants are different runs of the same scene: their take windows differ, so each needs its own
    units, and sharing the actor's scale is what keeps the two scores comparable.
    """
    src, keep = actor_features(source, units)
    if len(keep) < MIN_LINES:
        return {"lines": len(keep), "status": "insufficient_lines",
                "note": f"{MIN_LINES} measurable actor lines are the minimum"}
    nm = with_norms or norms([src[i] for i in keep])
    out = {"lines": len(keep), "status": "ok", "variants": []}
    for name, take in takes:
        tf = [features(slice_speech(take, u["take_start"], u["take_end"])) if i in keep else None
              for i, u in enumerate(units)]
        pairs = [i for i in keep if tf[i] is not None]
        rows = [{"unit": units[i].get("unit", f"line{i}"),
                 "actor_arousal": round(arousal(src[i], nm), 3),
                 "take_arousal": round(arousal(tf[i], nm), 3),
                 "delta": round(arousal(tf[i], nm) - arousal(src[i], nm), 3)} for i in pairs]
        actor = np.array([arousal(src[i], nm) for i in pairs])
        got = np.array([arousal(tf[i], nm) for i in pairs])
        delta = got - actor
        variant = {
            "name": name,
            "lines": len(pairs),
            "arousal_corr": spearman(actor, got),
            "pitch_corr": spearman([src[i]["pitch"] for i in pairs], [tf[i]["pitch"] for i in pairs]),
            "effort_corr": spearman([src[i]["effort"] for i in pairs], [tf[i]["effort"] for i in pairs]),
            "level_corr": spearman([src[i]["level"] for i in pairs], [tf[i]["level"] for i in pairs]),
            # 1.0 means the dub moves as much as the actor; 0.4 means it says the words flat.
            "dynamics": float(np.std(got) / np.std(actor)) if np.std(actor) > 1e-9 else float("nan"),
            # Shape and offset are separate claims: a dub pitched an octave above the actor can
            # follow the scene perfectly while sitting high on the actor's scale.
            "bias": float(np.mean(delta)),
            "shape_mae": float(np.mean(np.abs(delta - np.mean(delta)))),
            "floor_db": floor_db(take),
            "rows": rows,
        }
        out["variants"].append(variant)
    return out


# MARK: Units from a run

def units_from_trace(trace: Path, source_rate: int = CANONICAL_RATE) -> list[dict]:
    """Actor spans from `commit.source`, take windows from the unit's own play anchors.

    Spans are samples at the canonical capture rate; `out_sample` counters run at the device
    output rate, taken from the buffer records like the harness does.
    """
    rates = set()
    spans, start, end = {}, {}, {}
    for line in trace.read_text().splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("ev") == "buffer" and "rate" in e:
            rates.add(int(e["rate"]))
        elif "unit" not in e:
            continue
        elif e["ev"] == "commit.source":
            spans[e["unit"]] = e["span"]
        elif e["ev"] == "play.start":
            start.setdefault(e["unit"], e["out_sample"])
        elif e["ev"] == "play.end":
            end[e["unit"]] = e["out_sample"]
    out_rate = int(next(iter(rates))) if len(rates) == 1 else source_rate
    units = []
    for unit, span in spans.items():
        if unit not in start or unit not in end:
            continue
        units.append({"unit": unit,
                      "src_start": span[0] / source_rate, "src_end": span[1] / source_rate,
                      "take_start": start[unit] / out_rate, "take_end": end[unit] / out_rate})
    return sorted(units, key=lambda u: u["src_start"])


# MARK: CLI

def _load(path: str) -> tuple[np.ndarray, int]:
    x, rate = read_wav(path)
    return resample(x, rate), rate


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", help="what the pipeline heard (--source-out)")
    ap.add_argument("--takes", nargs="+", default=[], help="rendered audio; pass several to compare")
    ap.add_argument("--units", nargs="+", default=[],
                    help="units.json: one shared file, or one per --takes file")
    ap.add_argument("--from-run", nargs="+", default=[],
                    help="run directories holding trace.jsonl, source.wav and translated.wav")
    ap.add_argument("--take-name", default=None, help="label for a single --takes file")
    ap.add_argument("--json", dest="json_out", help="write the full result here")
    args = ap.parse_args(argv)

    runs = []
    for run in args.from_run:
        root = Path(run)
        units = root / "units.json"
        if not units.exists():
            source = root / "source.wav"
            source_x, source_rate = read_wav(root / "source.wav")
            units.write_text(json.dumps(units_from_trace(root / "trace.jsonl", source_rate), indent=1))
        runs.append({"units": json.loads(units.read_text()),
                     "source": root / "source.wav",
                     "takes": [root / "translated.wav"]})
    if args.source:
        runs.append({"units": [Path(u) for u in args.units] if args.units else [],
                     "source": Path(args.source), "takes": [Path(t) for t in args.takes]})
    if not runs:
        raise SystemExit(__doc__)

    report = []
    for run in runs:
        if not run["units"]:
            raise SystemExit("--source needs --units")
        source, _ = _load(run["source"])
        # One units file shared by every variant, or one per variant: two runs of the same scene
        # render each line at a different length, so their take windows are not interchangeable.
        units = [json.loads(u.read_text()) for u in run["units"]]
        pairs = [(take, units[0] if len(units) == 1 else units[n]) for n, take in enumerate(run["takes"])]
        if len(pairs) != len(run["takes"]):
            raise SystemExit("give --units once, or once per --takes file")
        variants, scale = [], None
        for take, unit_list in pairs:
            _, keep = actor_features(source, unit_list)
            scale = scale or (norms([f for f in [actor_features(source, unit_list)[0][i] for i in keep]]))
            result = score(source, [(args.take_name or take.stem, _load(take)[0])], unit_list, with_norms=scale)
            if result["status"] != "ok":
                variants.append({"name": take.stem, "status": result["status"]})
                continue
            variants.extend(result["variants"])
        result = {"lines": variants[0].get("lines") if variants else 0,
                  "status": "ok" if variants and all("status" not in v for v in variants) else "partial",
                  "variants": variants}
        report.append({"source": str(run["source"]), **result})
        if result["status"] != "ok":
            print(f"{run['source']}: {result['status']} ({result['note']})")
            continue
        print(f"{run['source']}  {result['lines']} lines scored")
        head = (f"{'variant':<28}{'arousal':>9}{'pitch':>8}{'effort':>8}{'level':>8}"
                f"{'dyn':>7}{'shape':>7}{'floor':>8}")
        print(head)
        fmt = lambda x: "  n/a" if not np.isfinite(x) else f"{x:6.2f}"
        for v in result["variants"]:
            if "status" in v:
                print(f"{v['name']:<28}{v['status']}")
                continue
            print(f"{v['name']:<28}{fmt(v['arousal_corr'])}{fmt(v['pitch_corr'])}{fmt(v['effort_corr'])}"
                  f"{fmt(v['level_corr'])}{fmt(v['dynamics'])}{fmt(v['shape_mae'])}{v['floor_db']:8.1f}")
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
