"""Replay a causal dubbing pass on a single-GPU queue for a presentation delay D, with deadlines enforced.

The viewer sees and hears the programme D seconds late (A2A shows the picture itself, or plays the file).
A line's English must start at its own slot, `start + D` in wall time. Work arrives as audio does:

  separation  every `hop` s of audio, cost hop * sep_rtf, top priority (the background bed must keep up)
  ASR + align at segment end + separation latency + VAD hangover, cost as measured in the pass
  MT, TTS     per line after its segment's ASR, cost as measured; ordered by deadline

The GPU runs one job at a time (MLX models share it). Before a line's MT starts, the scheduler drops it
when even its measured cost cannot meet `start + D + max_late` (catch-up, as fluent playout does). A take
that is ready late starts late (never over the previous take); past `max_late` it is dropped. Costs are
the pass's measured wall times, so the scheduler has an oracle estimate: real runs will drop a little more.

usage: python simulate.py RUN_DIR [--delays 0,3,6,10,15,20] [--separation roformer|isolation|hybrid]
       python simulate.py RUN_DIR --mode catchup [--max-speed 1.3 --max-lag 7]
"""
from __future__ import annotations

import argparse
import heapq
import json
import pathlib

SEPARATION = {  # latency (s), GPU cost per second of audio
    "roformer": (4.0, 0.25),   # 4 s windows, 1/8 overlap (measured RTF 0.23-0.27 at 8 s windows)
    "isolation": (0.15, 0.0),  # AUSoundIsolation: 0.13 s latency, 2.7% RT on its own engine path
    "hybrid": (0.15, 0.0),     # AUSoundIsolation for bed/VAD/speaker; RoFormer on each line for its reference
}
LINE_ROFORMER_RTF = 0.2  # 1.6 s per 8 s window, measured; a line is one window


def simulate(lines: list[dict], segments: list[dict], delay: float, separation: str = "roformer",
             max_late: float = 1.0, hangover: float = 0.25, hop: float = 3.5) -> dict:
    sep_latency, sep_rtf = SEPARATION[separation]
    end_t = max([s["end"] for s in segments] + [0]) + 30
    by_seg: dict[int, list[dict]] = {}
    for ln in lines:
        by_seg.setdefault(ln["seg"], []).append(ln)
    # Job: (ready, priority, deadline, seq, kind, payload)
    jobs: list = []
    seq = 0

    def push(ready, prio, deadline, kind, payload):
        nonlocal seq
        heapq.heappush(jobs, (ready, prio, deadline, seq, kind, payload))
        seq += 1

    if sep_rtf:
        t = hop
        while t < end_t:
            push(t, 0, t, "sep", hop * sep_rtf)
            t += hop
    # Every segment costs recognition, including those that turn out empty.
    for seg in segments:
        group = by_seg.get(seg["seg"], [])
        cost = sum(seg.get("compute", {}).get(k, 0.0) for k in ("asr", "align"))
        deadline = (group[0]["start"] if group else seg["start"]) + delay
        push(seg["end"] + sep_latency + hangover, 1, deadline, "asr", (group, cost))

    now = 0.0
    ready_q: list = []
    played, dropped = [], []
    last_play_end = -1e9
    while jobs or ready_q:
        while jobs and jobs[0][0] <= now:
            r, p, d, s, k, pl = heapq.heappop(jobs)
            heapq.heappush(ready_q, (p, d, s, k, pl, r))
        if not ready_q:
            now = jobs[0][0]
            continue
        p, d, s, k, pl, r = heapq.heappop(ready_q)
        if k == "sep":
            now += pl
        elif k == "asr":
            group, cost = pl
            now += cost
            for g in group:
                push(now, 2, g["start"] + delay, "mt", g)
        elif k == "mt":
            g = pl
            c = g.get("compute", {})
            need = c.get("mt", 0.0) + c.get("tts", 0.0)
            if separation == "hybrid":
                need += (g["end"] - g["start"] + 0.5) * LINE_ROFORMER_RTF
            if now + need > g["start"] + delay + max_late or "take" not in g:
                dropped.append({"line": g, "why": "catch_up" if "take" in g else "no_take"})
                continue
            now += c.get("mt", 0.0)
            push(now, 2, g["start"] + delay, "tts", g)
        elif k == "tts":
            g = pl
            now += g.get("compute", {}).get("tts", 0.0)
            if separation == "hybrid":
                now += (g["end"] - g["start"] + 0.5) * LINE_ROFORMER_RTF
            slot = g["start"] + delay
            start = max(slot, now, last_play_end)
            late = start - slot
            if late > max_late:
                dropped.append({"line": g, "why": "late"})
                continue
            last_play_end = start + g["take_s"]
            played.append({"line": g, "late": round(late, 3), "ready_margin": round(slot - now, 3)})
    total = sum(g["end"] - g["start"] for g in lines if g.get("take"))
    got = sum(x["line"]["end"] - x["line"]["start"] for x in played)
    lates = sorted(x["late"] for x in played)
    end_err = sorted(abs(x["late"] + x["line"]["take_s"] - (x["line"]["end"] - x["line"]["start"])) for x in played)
    q = lambda v, f: v[min(len(v) - 1, int(f * len(v)))] if v else None
    return {"delay": delay, "separation": separation, "lines": len([g for g in lines if g.get("take")]),
            "played": len(played), "on_time": sum(x["late"] <= 0.05 for x in played),
            "coverage": round(got / max(total, 1e-9), 3), "late_p50": q(lates, 0.5), "late_p90": q(lates, 0.9),
            "end_error_p50": round(q(end_err, 0.5), 3) if end_err else None,
            "end_within_0.25": sum(e <= 0.25 for e in end_err),
            "dropped": {w: sum(x["why"] == w for x in dropped) for w in ("catch_up", "late", "no_take")},
            "_played": played}


def simulate_catchup(lines: list[dict], segments: list[dict], separation: str = "isolation", max_speed: float = 1.3,
                     max_lag: float = 7.0, hangover: float = 0.15, hop: float = 3.5, press: bool = True) -> dict:
    """No picture delay: each take plays as soon as it is ready and the bus is free.

    A take that had to wait behind the previous one is sped up to win the wait back within itself
    (up to `max_speed`). A line whose English would start more than `max_lag` after the actor started is
    skipped before MT or at play time (the oldest unheard line goes first, as fluent playout does).
    """
    from translate import syllables
    sep_latency, sep_rtf = SEPARATION[separation]
    end_t = max([s["end"] for s in segments] + [0]) + 30
    by_seg: dict[int, list[dict]] = {}
    for ln in lines:
        by_seg.setdefault(ln["seg"], []).append(ln)
    jobs, seq = [], 0

    def push(ready, prio, deadline, kind, payload):
        nonlocal seq
        heapq.heappush(jobs, (ready, prio, deadline, seq, kind, payload))
        seq += 1

    if sep_rtf:
        t = hop
        while t < end_t:
            push(t, 0, t, "sep", hop * sep_rtf)
            t += hop
    for seg in segments:
        group = by_seg.get(seg["seg"], [])
        cost = sum(seg.get("compute", {}).get(k, 0.0) for k in ("asr", "align"))
        push(seg["end"] + sep_latency + hangover, 1, seg["start"], "asr", (group, cost))
    now, ready_q, played, dropped = 0.0, [], [], []
    bus_free = -1e9
    while jobs or ready_q:
        while jobs and jobs[0][0] <= now:
            r, p, d, s_, k, pl = heapq.heappop(jobs)
            heapq.heappush(ready_q, (p, d, s_, k, pl, r))
        if not ready_q:
            now = jobs[0][0]
            continue
        p, d, s_, k, pl, r = heapq.heappop(ready_q)
        if k == "sep":
            now += pl
        elif k == "asr":
            group, cost = pl
            now += cost
            for g in group:
                push(now, 2, g["start"], "mt", g)
        elif k == "mt":
            g = pl
            c = g.get("compute", {})
            if "take" not in g or now + c.get("mt", 0.0) + c.get("tts", 0.0) - g["start"] > max_lag:
                dropped.append(g)
                continue
            now += c.get("mt", 0.0)
            push(now, 2, g["start"], "tts", g)
        elif k == "tts":
            g = pl
            now += g.get("compute", {}).get("tts", 0.0)
            if separation == "hybrid":
                now += (g["end"] - g["start"] + 0.5) * LINE_ROFORMER_RTF
            # Mid-line catch-up: the take on the bus plays its remaining content faster once this one waits.
            if played and bus_free > now and press:
                cur = played[-1]
                content_left = (bus_free - now) * cur["speed"]
                if cur.get("tail_speed", 0) < max_speed and cur["speed"] < max_speed:
                    cur["switch_at"] = round(now - cur["start_t"], 3)
                    cur["tail_speed"] = max_speed
                    bus_free = now + content_left / max_speed
            start = max(now, bus_free)
            if start - g["start"] > max_lag:
                dropped.append(g)
                continue
            wait = start - now
            take = max(g["take_s"], 1e-3)
            speed = min(max_speed, max(1.0, take / max(take - wait, take / max_speed)))
            bus_free = start + take / speed
            played.append({"line": g, "late": round(start - g["start"], 3), "speed": round(speed, 3),
                           "start_t": start, "syl_per_s": syllables(g.get("en", "")) / (take / speed)})
    q = lambda v, f: round(sorted(v)[min(len(v) - 1, int(f * len(v)))], 3) if v else None
    total = sum(g["end"] - g["start"] for g in lines if g.get("take"))
    onset = [x["late"] for x in played]
    def heard(x):  # seconds the take occupies the bus, with any mid-line speed-up
        d = x["line"]["take_s"] / x["speed"]
        if "switch_at" in x and x["switch_at"] < d:
            d = x["switch_at"] + (d - x["switch_at"]) * x["speed"] / x["tail_speed"]
        return d
    end_lag = [x["late"] + heard(x) - (x["line"]["end"] - x["line"]["start"]) for x in played]
    return {"mode": "catchup", "separation": separation, "max_speed": max_speed, "max_lag": max_lag,
            "lines": len([g for g in lines if g.get("take")]), "played": len(played),
            "coverage": round(sum(x["line"]["end"] - x["line"]["start"] for x in played) / max(total, 1e-9), 3),
            "onset_lag_p50": q(onset, 0.5), "onset_lag_p90": q(onset, 0.9), "onset_lag_max": q(onset, 1.0),
            "end_lag_p50": q(end_lag, 0.5), "end_lag_p90": q(end_lag, 0.9),
            "sped_up": sum(x["speed"] > 1.01 or "tail_speed" in x for x in played),
            "speed_p90": q([max(x["speed"], x.get("tail_speed", 1.0)) for x in played], 0.9),
            "syl_per_s_p50": q([x["syl_per_s"] for x in played], 0.5), "syl_per_s_p90": q([x["syl_per_s"] for x in played], 0.9),
            "dropped": len(dropped), "_played": played}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--delays", default="0,3,6,10,15,20")
    ap.add_argument("--separation", default="roformer", choices=sorted(SEPARATION))
    ap.add_argument("--max-late", type=float, default=1.0)
    ap.add_argument("--mode", default="delay", choices=["delay", "catchup"],
                    help="delay: picture delayed by D; catchup: no picture delay, the voice catches up")
    ap.add_argument("--max-speed", type=float, default=1.3)
    ap.add_argument("--max-lag", type=float, default=7.0)
    ap.add_argument("--no-press", action="store_true", help="catch-up only on the waiting take, not mid-line")
    a = ap.parse_args()
    run = pathlib.Path(a.run)
    lines = json.load(open(run / "lines.json"))
    segments = json.load(open(run / "segments.json"))
    results = []
    if a.mode == "catchup":
        r = simulate_catchup(lines, segments, a.separation, a.max_speed, a.max_lag, press=not a.no_press)
        played = r.pop("_played")
        (run / f"schedule-catchup-{a.separation}.json").write_text(json.dumps(
            [{"start": x["line"]["start"], "late": x["late"], "speed": x["speed"], "take": x["line"]["take"],
              **({"switch_at": x["switch_at"], "tail_speed": x["tail_speed"]} if "switch_at" in x else {})}
             for x in played]))
        print(r)
        return
    for d in [float(x) for x in a.delays.split(",")]:
        r = simulate(lines, segments, d, a.separation, a.max_late)
        played = r.pop("_played")
        (run / f"schedule-D{d:g}-{a.separation}.json").write_text(json.dumps(
            [{"start": x["line"]["start"], "late": x["late"], "take": x["line"]["take"]} for x in played]))
        results.append(r)
        print(r)
    (run / f"sweep-{a.separation}.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
