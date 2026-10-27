"""Trace v0 → Chrome trace JSON for visual inspection in Perfetto (ui.perfetto.dev)."""

from __future__ import annotations

TRACKS = {
    "buf": (1, "audio buffers"),
    "vad": (2, "vad"),
    "spk": (2, "vad"),
    "asr": (3, "asr"),
    "commit": (4, "commit"),
    "mt": (5, "mt"),
    "tts": (6, "tts"),
    "pcm": (6, "tts"),
    "play": (7, "playout"),
    "state": (8, "session"),
    "fallback": (8, "session"),
    "gain": (8, "session"),
    "profile": (8, "session"),
    "session": (8, "session"),
    "unit": (9, "units"),
    "source": (9, "units"),
    "error": (10, "errors"),
    "trace": (10, "errors"),
}

# (begin event, end events, slice name)
SLICES = [
    ("mt.request", {"mt.result"}, "mt"),
    ("tts.request", {"tts.first_chunk"}, "tts first audio"),
    ("play.start", {"play.end"}, "playout"),
    ("commit.source", {"play.end", "unit.cancel"}, "unit"),
]

SKIP_INSTANT = {"buf.capture", "buf.output", "tts.chunk", "queue.depth", "resource.sample"}


def convert(events: list[dict], *, include_buffers: bool = False) -> dict:
    if not events:
        return {"traceEvents": [], "displayTimeUnit": "ms"}
    t0 = min(e["t_ns"] for e in events)
    out: list[dict] = []
    pid = 1
    for tid, name in sorted({v for v in TRACKS.values()}):
        out.append({"ph": "M", "name": "thread_name", "pid": pid, "tid": tid, "args": {"name": name}})
    out.append({"ph": "M", "name": "process_name", "pid": pid, "args": {"name": f"a2a {events[0].get('session', '')}"}})

    def ts(e: dict) -> float:
        return (e["t_ns"] - t0) / 1000.0

    open_slices: dict[tuple[str, str], dict] = {}
    for e in events:
        ev = e["ev"]
        tid = TRACKS.get(ev.split(".")[0], (10, "errors"))[0]
        args = {k: v for k, v in e.items() if k not in {"v", "t_ns", "session", "ev"}}
        if ev == "queue.depth":
            out.append({"ph": "C", "name": "queue", "pid": pid, "ts": ts(e),
                        "args": {"debt_ms": e["debt_ms"], "oldest_source_age_ms": e["oldest_source_age_ms"]}})
        elif ev == "resource.sample":
            counters = {k: v for k, v in e.items() if k.endswith("_bytes")}
            if counters:
                out.append({"ph": "C", "name": f"memory {e.get('process', '')}", "pid": pid, "ts": ts(e), "args": counters})
        elif ev not in SKIP_INSTANT or include_buffers:
            out.append({"ph": "i", "s": "t", "name": ev, "pid": pid, "tid": tid, "ts": ts(e), "args": args})

        unit = e.get("unit")
        if unit:
            for begin, ends, name in SLICES:
                if ev == begin:
                    open_slices[(name, unit)] = e
                elif ev in ends and (name, unit) in open_slices:
                    b = open_slices.pop((name, unit))
                    cat = name.replace(" ", "_")
                    out.append({"ph": "b", "cat": cat, "name": f"{name} {unit}", "id": f"{cat}-{unit}", "pid": pid, "ts": ts(b)})
                    out.append({"ph": "e", "cat": cat, "name": f"{name} {unit}", "id": f"{cat}-{unit}", "pid": pid, "ts": ts(e)})
    return {"traceEvents": out, "displayTimeUnit": "ms"}
