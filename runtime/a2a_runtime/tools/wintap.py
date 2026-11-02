"""a2a-wintap: Windows capture qualification tool (the a2a-tapctl counterpart, ADR-0007).

  list                         audio sessions with their processes
  devices                      render endpoints (IDs for --output-device)
  capture (--pid N | --exe NAME) --seconds S --out x.wav [--exclude] [--duck]
  fidelity --reference ref.wav --capture cap.wav [--json out.json]
  mute-probe (--pid N | --exe NAME) [--seconds S] [--json out.json]
                               does process loopback still capture a muted session?
  volume-probe (--pid N | --exe NAME) [--seconds S] [--json out.json]
                               captured level against the source's session volume
  monitor (--pid N | --exe NAME) --seconds S [--duck] [--delay-ms D] [--output-device DEV]
          [--rendered-out x.wav]
                               re-render the captured app through the mixer (A2)
  restore                      repair sessions a crashed session left silenced
  guard --host-pid N           (internal) repair silenced sessions once that host exits

`--exclude` captures every process except the target tree (whole-system mode). `--duck`
lowers the source to -60 dB and restores the capture by the exact inverse gain.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

from .. import native
from ..audio import windows as win
from ..media import signal, wav
from ..media.wav import WAVStreamWriter

RATE = 48_000
CHANNELS = 2


def _source(args) -> win.Source:
    if args.pid is None and not args.exe:
        raise SystemExit("pass --pid N or --exe NAME")
    return win.resolve(pid=args.pid, exe=args.exe)


def _engine(render: bool, rendered_seconds: float = 0) -> native.Engine:
    e = native.Engine(channels=CHANNELS, capture_ring_frames=RATE * 4, max_delay_frames=RATE // 5,
                      translated_ring_frames=RATE * 2, max_block_frames=4_096, record_capacity=8_192,
                      rendered_ring_frames=int(RATE * rendered_seconds), fade_frames=240)
    e.set_render_output(render)
    return e


def _capture(engine: native.Engine, seconds: float, sink=None) -> np.ndarray:
    """Drains the capture ring for `seconds`; returns stereo samples when no sink is given."""
    chunks = []
    buf = np.empty(4_096 * CHANNELS, dtype=np.float32)
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        n = engine.capture.read_into(buf)
        if n:
            block = buf[: n * CHANNELS].copy()
            if sink is not None:
                sink.append(block)
            else:
                chunks.append(block)
        else:
            time.sleep(0.005)
    return np.concatenate(chunks) if chunks else np.zeros(0, np.float32)


def _rms_db(x: np.ndarray) -> float:
    return signal.dbfs(signal.rms(x)) if len(x) else float("-inf")


def _emit(result: dict, path: str | None) -> None:
    print(json.dumps(result, indent=1, default=str))
    if path:
        with open(path, "w") as fh:
            json.dump(result, fh, indent=1, default=str)


def cmd_list(_args) -> int:
    for s in sorted(win.audio_sessions(), key=lambda s: (s.endpoint, s.pid)):
        print(f"{s.pid:>7}  {s.state:<8} {'muted' if s.muted else '     '} vol {s.volume:7.5f}  "
              f"{s.exe or s.display_name or '-':<28} [{s.endpoint}]")
    return 0


def cmd_devices(_args) -> int:
    for e in win.endpoints():
        print(f"{'*' if e.is_default else ' '} {e.name:<48} {e.id}")
    return 0


def cmd_capture(args) -> int:
    source = _source(args)
    silencer = win.SourceSilencer(source) if args.duck else None
    engine = _engine(render=False)
    writer = WAVStreamWriter(args.out, RATE, CHANNELS)
    session = None
    try:
        if silencer:
            win.spawn_guard()
            silencer.refresh()
        session = win.WasapiSession(engine, source.root, exclude_tree=args.exclude, render=False,
                                    capture_gain=silencer.capture_gain if silencer else 1.0)
        print(f"capturing {source.label}{' (excluded)' if args.exclude else ''} for {args.seconds} s", file=sys.stderr)
        _capture(engine, args.seconds, writer)
    finally:
        stats = session.stats() if session else {}
        if session:
            session.stop()
        if silencer:
            silencer.restore()
        writer.close()
    print(json.dumps({"source": source.label, "frames": writer.frames_written, "ducked": bool(silencer),
                      "stats": stats}, default=str))
    return 0


def cmd_fidelity(args) -> int:
    """A1 fidelity on Windows: align a capture with the file the app played and correlate
    them over the whole overlap and in 10 s windows."""
    ref = wav.read(args.reference)
    cap = wav.read(args.capture)
    r = ref.mono()
    c = cap.mono()
    if ref.sample_rate != cap.sample_rate:
        raise SystemExit(f"rates differ: reference {ref.sample_rate}, capture {cap.sample_rate}")
    rate = ref.sample_rate
    # The capture may start inside the file: search the reference for the capture's opening.
    probe_len = min(len(c) // 2, rate * 2)
    start_cap = rate // 2
    lag, corr = signal.best_lag(c, r, range(start_cap, start_cap + probe_len), max_lag=len(r) - probe_len - rate)
    start_ref = start_cap + lag  # r[start_cap + lag] lines up with c[start_cap]
    n = min(len(r) - start_ref, len(c) - start_cap)
    a, b = r[start_ref:start_ref + n], c[start_cap:start_cap + n]
    windows = [signal.correlation(a[i:i + rate * 10], b[i:i + rate * 10]) for i in range(0, n - rate * 10 + 1, rate * 10)]
    gain_db = signal.dbfs(signal.rms(b)) - signal.dbfs(signal.rms(a))
    result = {"aligned_frames": int(n), "reference_offset_frames": int(start_ref - start_cap),
              "correlation": signal.correlation(a, b), "worst_10s_window": min(windows) if windows else None,
              "windows": len(windows), "level_difference_db": gain_db,
              "zero_runs_over_10ms": len(signal.zero_runs(b, rate // 100)), "probe_correlation": corr}
    _emit(result, args.json)
    return 0 if result["correlation"] > 0.99 else 1


def cmd_mute_probe(args) -> int:
    """Captures unmuted, muted, then unmuted again (steady-state windows) and reports level
    and the fraction of exactly-zero samples in each."""
    source = _source(args)
    engine = _engine(render=False)
    silencer = win.SourceSilencer(source, mode="mute")
    session = win.WasapiSession(engine, source.root, render=False)
    levels, zeros = {}, {}

    def window(name: str) -> None:
        _capture(engine, 0.5)  # let audio buffered before the change drain
        x = _capture(engine, args.seconds)
        levels[name] = _rms_db(x)
        zeros[name] = float(np.mean(x == 0)) if len(x) else 1.0

    try:
        window("unmuted_before")
        changed = silencer.refresh()
        window("muted")
        restored = silencer.restore()
        window("unmuted_after")
    finally:
        silencer.restore()
        stats = session.stats()
        session.stop()
    _emit({"source": source.label, "sessions_muted": changed, "sessions_restored": restored, "rms_dbfs": levels,
           "zero_sample_fraction": zeros, "capture_survives_session_mute": levels["muted"] > levels["unmuted_before"] - 6,
           "stats": stats}, args.json)
    return 0


def cmd_volume_probe(args) -> int:
    """Captured level against session volume. A linear law (gain = 20·log10(level)) means a
    deeply ducked source is captured and restored exactly by the inverse gain."""
    source = _source(args)
    sessions = [s for s in win.audio_sessions() if s.pid in source.members]
    if not sessions:
        raise SystemExit(f"{source.label} has no audio session; start playback first")
    original = {s.pid: s.volume for s in sessions}
    engine = _engine(render=False)
    session = win.WasapiSession(engine, source.root, render=False)
    rows = []
    try:
        reference = None
        for level in (1.0, 0.5, 0.1, 0.01, 0.001, 0.0001):
            win.set_volume(set(original), level)
            _capture(engine, 0.5)
            db = _rms_db(_capture(engine, args.seconds))
            reference = db if reference is None else reference
            expected = 20 * np.log10(level)
            rows.append({"level": level, "rms_dbfs": db, "gain_db": db - reference, "expected_db": expected,
                         "error_db": db - reference - expected})
    finally:
        for pid, level in original.items():
            win.set_volume({pid}, level)
        session.stop()
    _emit({"source": source.label, "rows": rows, "linear_gain_law": all(abs(r["error_db"]) < 0.1 for r in rows)},
          args.json)
    return 0


def cmd_monitor(args) -> int:
    source = _source(args)
    endpoint = win.resolve_endpoint(args.output_device)
    silencer = win.SourceSilencer(source) if args.duck else None
    engine = _engine(render=True, rendered_seconds=4 if args.rendered_out else 0)
    engine.set_delay_frames(int(args.delay_ms * RATE / 1000))
    writer = WAVStreamWriter(args.rendered_out, RATE, CHANNELS) if args.rendered_out else None
    if writer:
        engine.set_capture_rendered(True)
    session = None
    try:
        if silencer:
            win.spawn_guard()
            silencer.refresh()
        session = win.WasapiSession(engine, source.root, render=True,
                                    capture_gain=silencer.capture_gain if silencer else 1.0,
                                    render_device_id=endpoint.id if endpoint else None)
        print(f"monitoring {source.label} on {session.stats()['render_device']}", file=sys.stderr)
        deadline = time.perf_counter() + args.seconds
        last = 0.0
        buf = np.empty(4_096 * CHANNELS, dtype=np.float32)
        while time.perf_counter() < deadline:
            engine.capture.skip(1 << 20)  # the original bus reads the delay line, not this ring
            if writer:
                while (n := engine.rendered.read_into(buf)):
                    writer.append(buf[: n * CHANNELS])
            if silencer and time.perf_counter() - last >= 0.25:
                silencer.refresh()
                last = time.perf_counter()
            if not session.running:
                print(f"stopped: {session.stats()['message']}", file=sys.stderr)
                break
            time.sleep(0.02)
    finally:
        stats = session.stats() if session else {}
        if session:
            session.stop()
        if silencer:
            silencer.restore()
        if writer:
            writer.close()
    print(json.dumps({"source": source.label, "ducked": bool(silencer), "stats": stats,
                      "engine": {k: engine.counter(k) for k in ("callbacks", "captured_frames", "output_frames",
                                                                "rejected_blocks")}}, default=str))
    return 0


def cmd_restore(_args) -> int:
    print(json.dumps({"restored": win.restore_stranded(all_hosts=True), "pending": win.pending_repairs()}))
    return 0


def cmd_guard(args) -> int:
    win.wait_for_exit(args.host_pid)
    time.sleep(0.2)
    win.restore_stranded()
    return 0


def _target(p) -> None:
    p.add_argument("--pid", type=int)
    p.add_argument("--exe")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="a2a-wintap", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    sub.add_parser("devices")
    p = sub.add_parser("capture")
    _target(p)
    p.add_argument("--seconds", type=float, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--exclude", action="store_true")
    p.add_argument("--duck", action="store_true")
    p = sub.add_parser("fidelity")
    p.add_argument("--reference", required=True)
    p.add_argument("--capture", required=True)
    p.add_argument("--json")
    for name in ("mute-probe", "volume-probe"):
        p = sub.add_parser(name)
        _target(p)
        p.add_argument("--seconds", type=float, default=2.0)
        p.add_argument("--json")
    p = sub.add_parser("monitor")
    _target(p)
    p.add_argument("--seconds", type=float, required=True)
    p.add_argument("--duck", action="store_true")
    p.add_argument("--delay-ms", type=float, default=30.0)
    p.add_argument("--output-device")
    p.add_argument("--rendered-out")
    sub.add_parser("restore")
    p = sub.add_parser("guard")
    p.add_argument("--host-pid", type=int, required=True)
    args = parser.parse_args(argv)
    if args.cmd not in ("guard", "restore", "fidelity"):
        win.restore_stranded()
    handlers = {"list": cmd_list, "devices": cmd_devices, "capture": cmd_capture, "fidelity": cmd_fidelity,
                "mute-probe": cmd_mute_probe, "volume-probe": cmd_volume_probe, "monitor": cmd_monitor,
                "restore": cmd_restore, "guard": cmd_guard}
    return handlers[args.cmd](args)


def entry() -> None:
    try:
        code = main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else (0 if exit_.code is None else 2)
    sys.stdout.flush()
    os._exit(code)


if __name__ == "__main__":
    entry()
