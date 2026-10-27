"""Pass/fail check for a live (tap) run against a zh_demo clip.

Inputs are the run directory written by `a2a-pipeline --tap-… --research-text
--source-out source.wav` and the clip's `expected.json`. The check:

1. aligns what the pipeline heard (`source.wav`) with `clip.wav`, so units map to clip time;
2. per expected sentence, joins the English of the units that overlap it and were played,
   and requires every keyword group to match;
3. requires a muted tap, no capture overflow, no `error` events, and no trace drops;
4. optionally compares the back-transcript of `translated.wav` (`a2a-transcribe`) with the
   played English. `translated.wav` holds only the translated audio the mixer consumed, so
   a unit cut off by a fallback flush contributes at most the fragment that was heard.

Latency is reported from device timestamps: speech onset/end come from the RT capture
records, playback from the mixer's output records. System-voice runs are plumbing only.

    uv run --project harness python -m a2a_harness.live_check --run .local/runs/zh-live \\
        --expected .local/fixtures/zh-demo/expected.json [--transcript transcript.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from . import CANONICAL_RATE, trace
from .audio import read_wav
from .metrics import TimeMap, wer

PAD_S = 0.3
MIN_ALIGNMENT = 0.5
MAX_BACK_WER = 0.2


def align(source: np.ndarray, clip: np.ndarray, rate: int, window_s: float = 20.0) -> tuple[float, float]:
    """Clip time (s) of source sample 0, and the Pearson correlation at that offset.

    The offset is negative when capture started before the player produced the clip's
    first sample (the usual case for a tap opened before playback)."""
    a = source[: int(window_s * rate)].astype(np.float64)
    b = clip.astype(np.float64)
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    corr = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)
    # corr[k] pairs a[i] with b[i + k]; indices past len(b) wrap around to negative k.
    k = int(np.argmax(np.concatenate([corr[n - len(a) + 1 :], corr[: len(b)]]))) - (len(a) - 1)
    src = source[max(0, -k) :]
    ref = b[max(0, k) :]
    m = min(len(src), len(ref))
    if m < rate:
        return k / rate, 0.0
    return k / rate, float(np.corrcoef(src[:m], ref[:m])[0, 1])


def matches(text: str, groups: list[list[str]]) -> list[list[str]]:
    """Keyword groups with no alternative present in `text` (empty = pass)."""
    low = text.lower()
    return [g for g in groups if not any(k.lower() in low for k in g)]


def check(run: Path, expected_path: Path, transcript_path: Path | None = None) -> dict:
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    events = trace.load(run / "trace.jsonl")
    validation = trace.validate(events)
    summary = json.loads((run / "summary.json").read_text())
    rate = CANONICAL_RATE

    source, src_rate = read_wav(run / "source.wav")
    clip, clip_rate = read_wav(expected_path.parent / "clip.wav")
    if src_rate != rate or clip_rate != rate:
        raise ValueError("source.wav and clip.wav must be 48 kHz")
    offset_s, alignment = align(source, clip, rate)

    capture = TimeMap.from_anchors([(e["span"][0], e["t_ns"]) for e in events if e["ev"] == "buf.capture"
                                    and "device_index" in e and e.get("host_time_valid")])
    output = TimeMap.from_anchors([(e["out_sample"], e["t_ns"]) for e in events if e["ev"] == "buf.output"
                                   and e.get("host_time_valid")])

    units: dict[str, dict] = {}
    for e in events:
        u = e.get("unit")
        if not u:
            continue
        r = units.setdefault(u, {"unit": u})
        if e["ev"] == "commit.source":
            r["span"] = e["span"]
            r["source"] = e.get("text")
        elif e["ev"] == "commit.target":
            r["target"] = e.get("text")
        elif e["ev"] == "play.start":
            r["play_start"] = e["out_sample"]
        elif e["ev"] == "play.end":
            r["play_end"] = e["out_sample"]
        elif e["ev"] == "source.disposition":
            r["disposition"] = e["disposition"]
    ordered = sorted((r for r in units.values() if "span" in r), key=lambda r: r["span"][0])
    for r in ordered:
        r["clip_s"] = [r["span"][0] / rate + offset_s, r["span"][1] / rate + offset_s]
        r["played"] = "play_end" in r and r.get("disposition") == "translated"

    sentences = []
    for s in expected["sentences"]:
        lo, hi = s["onset_s"] - PAD_S, s["offset_s"] + PAD_S
        overlap = [r for r in ordered if r["clip_s"][0] < hi and r["clip_s"][1] > lo]
        played = [r for r in overlap if r["played"]]
        english = " ".join(r.get("target") or "" for r in played).strip()
        missing = matches(english, s["keywords"])
        row = {"index": s["index"], "text": s["text"], "english": english,
               "units": [r["unit"] for r in overlap], "played_units": [r["unit"] for r in played],
               "missing_keywords": missing, "pass": bool(played) and not missing}
        if played and capture and output:
            onset_ns = capture.t_ns((s["onset_s"] - offset_s) * rate)
            end_ns = capture.t_ns((s["offset_s"] - offset_s) * rate)
            first_ns = output.t_ns(played[0]["play_start"])
            row["lag_onset_s"] = round((first_ns - onset_ns) / 1e9, 3)
            row["play_after_end_s"] = round((first_ns - end_ns) / 1e9, 3)
        sentences.append(row)

    kinds = validation.kinds
    errors = [e for e in events if e["ev"] == "error"]
    tap = summary.get("tap", {})
    played_text = " ".join(r.get("target") or "" for r in ordered if r["played"])
    report = {
        "plumbing_only": not summary.get("tts_preserves_speaker_identity", False),
        "identity_qualified": bool(summary.get("tts_identity_qualified", False)),
        "alignment": {"clip_offset_s": round(offset_s, 4), "correlation": round(alignment, 4)},
        "sentences": sentences,
        "units": len(ordered),
        "units_played": sum(r["played"] for r in ordered),
        "units_not_in_any_sentence": [r["unit"] for r in ordered
                                      if not any(r["unit"] in s["units"] for s in sentences)],
        "fallbacks": kinds.get("fallback.enter", 0),
        "catch_ups": kinds.get("catch_up", 0),
        "errors": len(errors),
        "error_samples": errors[:5],
        "trace_drops": validation.trace_drops,
        "schema_ok": validation.ok,
        "schema_errors": validation.errors[:5],
        "tap_muted": tap.get("muted"),
        "capture_overflow_frames": tap.get("capture_overflow_frames"),
        "underruns": summary.get("underruns"),
        "thresholds": {"max_debt_seconds": summary.get("max_debt_seconds"),
                       "max_source_lag_seconds": summary.get("max_source_lag_seconds")},
    }
    lags = [s["lag_onset_s"] for s in sentences if "lag_onset_s" in s]
    if lags:
        report["lag_onset_s"] = {"median": float(np.median(lags)), "max": max(lags)}
    if transcript_path:
        heard = json.loads(transcript_path.read_text(encoding="utf-8")).get("text", "")
        report["back_transcript"] = {"text": heard, "wer_vs_played": round(wer(played_text, heard), 4)}

    failures = []
    if not all(s["pass"] for s in sentences):
        failures.append("sentence not translated and played: "
                        + ", ".join(str(s["index"]) for s in sentences if not s["pass"]))
    if alignment < MIN_ALIGNMENT:
        failures.append(f"source/clip alignment {alignment:.2f} < {MIN_ALIGNMENT}")
    if errors:
        failures.append(f"{len(errors)} error events")
    if validation.trace_drops:
        failures.append(f"{validation.trace_drops} trace drops")
    if not validation.ok:
        failures.append("trace schema errors")
    if tap.get("muted") is not True:
        failures.append("tap not muted")
    if tap.get("capture_overflow_frames", 0):
        failures.append("capture overflow")
    if "back_transcript" in report and report["back_transcript"]["wer_vs_played"] > MAX_BACK_WER:
        failures.append(f"back-transcript WER {report['back_transcript']['wer_vs_played']:.2f} > {MAX_BACK_WER}")
    report["failures"] = failures
    report["pass"] = not failures
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", type=Path, required=True)
    ap.add_argument("--expected", type=Path, required=True)
    ap.add_argument("--transcript", type=Path, help="a2a-transcribe JSON of the run's translated.wav")
    ap.add_argument("--json", type=Path, help="write the full report here (default: <run>/live_check.json)")
    args = ap.parse_args()
    report = check(args.run, args.expected, args.transcript)
    out = args.json or args.run / "live_check.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for s in report["sentences"]:
        mark = "PASS" if s["pass"] else "FAIL"
        lag = f" lag_onset {s['lag_onset_s']:.2f}s" if "lag_onset_s" in s else ""
        print(f"{mark} {s['index']}: {s['text']} -> {s['english'] or '(nothing played)'}{lag}")
    print(json.dumps({k: report[k] for k in ("pass", "failures", "alignment", "units", "units_played", "fallbacks",
                                             "errors", "trace_drops", "tap_muted", "lag_onset_s", "plumbing_only",
                                             "identity_qualified")
                      if k in report}, ensure_ascii=False))
    if "back_transcript" in report:
        print("back-transcript WER vs played:", report["back_transcript"]["wer_vs_played"])
    sys.exit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
