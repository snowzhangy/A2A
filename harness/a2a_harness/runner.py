"""Replay runner (H1). One command plus pinned artifacts reproduces a run directory.

Modes:
  offline  pipeline reads the WAV itself on a virtual clock (deterministic, CI).
  paced    the harness streams 20 ms frames over stdin at real-time pace; the pipeline
           never has access to future samples (verified from the feed log).
  tap      FixturePlayer + native capture on the provisioned interactive Mac (pending).
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import CANONICAL_RATE, REPO_ROOT, fixture as fixture_mod, metrics, perfetto, trace
from .audio import read_wav

CHUNK_FRAMES = CANONICAL_RATE // 50  # 20 ms


def host_ns() -> int:
    """The pipeline's t_ns base: mach_absolute_time on macOS, the QPC counter on Windows
    (what perf_counter_ns reads, and a2a_now_ns in the portable runtime), CLOCK_MONOTONIC
    elsewhere."""
    if sys.platform == "darwin":
        return time.clock_gettime_ns(time.CLOCK_UPTIME_RAW)
    if sys.platform == "win32":
        return time.perf_counter_ns()
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


@dataclass
class RunConfig:
    fixture: Path
    pipeline: Path
    out_dir: Path
    mode: str = "offline"
    speed: float = 1.0
    asr: str = "scripted"
    mt: str = "dictionary"
    tts: str = "tone"
    profile_ready_ms: int = 0
    research_text: bool = True
    session: str | None = None
    extra_args: list[str] = field(default_factory=list)


def _sha256(path: Path) -> str | None:
    if not path or not Path(path).exists():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _cmd(args: list[str]) -> str | None:
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=20).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _windows_ram_bytes() -> int:
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MemoryStatus()
    status.dwLength = ctypes.sizeof(MemoryStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
    return int(status.ullTotalPhys)


def host_metadata() -> dict:
    common = {
        "python": sys.version.split()[0],
        "git_rev": _cmd(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"]),
        "gpu": _cmd(["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"]),
    }
    if sys.platform == "darwin":
        return {
            "platform": "macOS",
            "chip": _cmd(["sysctl", "-n", "machdep.cpu.brand_string"]),
            "ram_bytes": int(_cmd(["sysctl", "-n", "hw.memsize"]) or 0),
            "model": _cmd(["sysctl", "-n", "hw.model"]),
            "os": platform.mac_ver()[0],
            "os_build": _cmd(["sysctl", "-n", "kern.osversion"]),
            "power": (_cmd(["pmset", "-g", "batt"]) or "").split("\n")[0],
            "low_power_mode": (_cmd(["pmset", "-g"]) or "").find("lowpowermode         1") >= 0,
            "swift": (_cmd(["swift", "--version"]) or "").split("\n")[0],
            "developer_dir": os.environ.get("DEVELOPER_DIR"),
            **common,
        }
    if sys.platform == "win32":
        build = platform.version()
        return {
            "platform": "Windows",
            "chip": _cmd(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"])
            or platform.processor(),
            "ram_bytes": _windows_ram_bytes(),
            "model": _cmd(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_ComputerSystem).Model"]),
            "os": platform.release(),
            "os_build": build,
            **common,
        }
    return {"platform": platform.system(), "chip": platform.processor(), "ram_bytes": 0, "model": None,
            "os": platform.release(), "os_build": platform.version(), **common}


def _pipeline_args(cfg: RunConfig, fx: fixture_mod.Fixture, out: Path) -> list[str]:
    args = [str(cfg.pipeline)]
    if cfg.mode == "offline":
        args += ["--input", str(fx.audio), "--clock", "virtual"]
    else:
        args += ["--stdin", "--rate", str(CANONICAL_RATE), "--clock", "host"]
    args += ["--asr", cfg.asr, "--mt", cfg.mt, "--tts", cfg.tts, "--profile-ready-ms", str(cfg.profile_ready_ms),
             "--trace", str(out / "trace.jsonl"), "--out", str(out / "out.wav"), "--stems", str(out / "stems.wav"),
             "--session", cfg.session or fx.id[:16]]
    if cfg.asr == "scripted" and fx.asr_script:
        args += ["--asr-script", str(fx.asr_script)]
    if cfg.mt == "dictionary" and fx.mt_dictionary:
        args += ["--mt-dictionary", str(fx.mt_dictionary)]
    if cfg.research_text:
        args.append("--research-text")
    return args + cfg.extra_args


def _feed(proc: subprocess.Popen, audio: np.ndarray, speed: float, log: list[dict]) -> None:
    interval = CHUNK_FRAMES / CANONICAL_RATE / speed
    start = host_ns()
    sent = 0
    try:
        for k in range(0, len(audio), CHUNK_FRAMES):
            target = start + int((k // CHUNK_FRAMES) * interval * 1e9)
            delay = (target - host_ns()) / 1e9
            if delay > 0:
                time.sleep(delay)
            block = audio[k : k + CHUNK_FRAMES]
            t_start = host_ns()
            proc.stdin.write(block.astype("<f4").tobytes())
            proc.stdin.flush()
            log.append({"t_ns": t_start, "start_frames": sent, "frames": sent + len(block), "t_done_ns": host_ns()})
            sent += len(block)
    except (BrokenPipeError, ValueError):
        pass
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass


def run(cfg: RunConfig) -> dict:
    fx = fixture_mod.load(cfg.fixture)
    if cfg.mode == "tap":
        raise SystemExit("tap mode needs the provisioned interactive Mac with FixturePlayer and capture "
                         "permission (A1/E1); use a2a-tapctl there. Not available in this runner yet.")
    out = Path(cfg.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    args = _pipeline_args(cfg, fx, out)
    feed_log: list[dict] = []
    t_wall = time.time()
    if cfg.mode == "offline":
        proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", timeout=600)
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    elif cfg.mode == "paced":
        audio, rate = read_wav(fx.audio)
        if rate != CANONICAL_RATE:
            raise SystemExit(f"paced mode expects {CANONICAL_RATE} Hz fixtures, got {rate}")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        captured: dict[str, bytes] = {}
        readers = [threading.Thread(target=lambda n=n, s=s: captured.__setitem__(n, s.read()), daemon=True)
                   for n, s in (("stdout", proc.stdout), ("stderr", proc.stderr))]
        for r in readers:
            r.start()
        feeder = threading.Thread(target=_feed, args=(proc, audio, cfg.speed, feed_log), daemon=True)
        feeder.start()
        feeder.join(timeout=fx.duration_s / cfg.speed + 30)
        code = proc.wait(timeout=60)
        for r in readers:
            r.join(timeout=10)
        stdout, stderr = captured.get("stdout", b"").decode(), captured.get("stderr", b"").decode()
        with open(out / "feed_log.jsonl", "w", encoding="utf-8") as fh:
            for e in feed_log:
                fh.write(json.dumps(e) + "\n")
    else:
        raise SystemExit(f"unknown mode {cfg.mode}")
    (out / "pipeline.stderr.txt").write_text(stderr, encoding="utf-8")
    if code != 0:
        raise RuntimeError(f"pipeline exited {code}: {stderr.strip()[-2000:]}")
    summary = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}

    events = trace.load(out / "trace.jsonl")
    validation = trace.validate(events)
    m = metrics.compute(events, fx, feed_log=feed_log or None)
    gate = metrics.streaming_gate(m)
    violations = metrics.future_access_violations(events, feed_log) if cfg.mode == "paced" else []
    plumbing_only = cfg.tts == "tone" or not summary.get("tts_preserves_speaker_identity", False)
    # A cloning synthesizer attempts identity; only a passed identity study (M2) qualifies it.
    identity_qualified = bool(summary.get("tts_identity_qualified", False))
    gate_valid = (cfg.mode == "paced" and cfg.speed == 1.0 and not plumbing_only and identity_qualified
                  and validation.measurements_valid and not violations)

    metadata = {
        "fixture": {"id": fx.id, "kind": fx.kind, "license": fx.license, "path": str(fx.path),
                    "audio_sha256": _sha256(fx.audio), "duration_s": fx.duration_s},
        "pipeline": {"path": str(cfg.pipeline), "sha256": _sha256(cfg.pipeline), "args": args, "summary": summary},
        "mode": cfg.mode, "speed": cfg.speed, "adapters": {"asr": cfg.asr, "mt": cfg.mt, "tts": cfg.tts},
        "warm_status": "prepared profile" if cfg.profile_ready_ms == 0 else f"profile ready at {cfg.profile_ready_ms} ms",
        "trace_schema_version": 0,
        "host": host_metadata(),
        "started_unix": t_wall,
        "quantization": None, "decoding": None, "seeds": None,
    }
    result = {
        "metadata": metadata,
        "validation": {"ok": validation.ok, "errors": validation.errors[:50], "events": validation.events,
                       "trace_drops": validation.trace_drops, "kinds": validation.kinds},
        "future_access_violations": violations[:50],
        "metrics": m,
        "streaming_gate": gate,
        "plumbing_only": plumbing_only,
        "identity_qualified": identity_qualified,
        "gate_valid": gate_valid,
    }
    (out / "metrics.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (out / "trace.perfetto.json").write_text(json.dumps(perfetto.convert(events)), encoding="utf-8")
    (out / "report.md").write_text(render_report(result), encoding="utf-8")
    return result


def _fmt(v, unit: str = "") -> str:
    if v is None:
        return "n/a"
    return f"{v:.3f}{unit}" if isinstance(v, float) else f"{v}{unit}"


def render_report(r: dict) -> str:
    m, md = r["metrics"], r["metadata"]
    gate = r["streaming_gate"]
    lines = [
        f"# Replay report: {md['fixture']['id']} ({md['mode']}, speed {md['speed']})",
        "",
    ]
    if r["plumbing_only"]:
        lines += ["> **Plumbing only.** Fixture or system-voice adapters were used; latency reflects modeled",
                  "> compute and no speaker identity is preserved. Not evidence for any model or gate row.", ""]
    elif not r["gate_valid"]:
        lines += ["> Not gate-valid (needs paced mode at speed 1.0, valid trace, no future access).", ""]
    lines += [
        f"- Adapters: ASR `{md['adapters']['asr']}`, MT `{md['adapters']['mt']}`, TTS `{md['adapters']['tts']}`; {md['warm_status']}",
        f"- Host: {md['host'].get('chip')}, {md['host'].get('ram_bytes', 0) >> 30} GB, "
        f"{md['host'].get('platform', 'macOS')} {md['host'].get('os')} ({md['host'].get('os_build')})"
        + (f", GPU {md['host']['gpu']}" if md['host'].get('gpu') else ""),
        f"- Trace: {r['validation']['events']} events, schema {'valid' if r['validation']['ok'] else 'INVALID'}, drops {r['validation']['trace_drops']}",
        f"- Future-audio access: {'none detected' if not r['future_access_violations'] else str(len(r['future_access_violations'])) + ' violations'}"
        + (" (checked against feed log)" if md["mode"] == "paced" else " (offline: reader-enforced)"),
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Units committed / played / aligned | {m['units_committed']} / {m['units_played']} / {m['units_aligned']} |",
        f"| `lag_onset` median / p95 (s) | {_fmt(m['lag_onset_s']['median'])} / {_fmt(m['lag_onset_s']['p95'])} |",
        f"| `lag_onset` char-weighted median (s) | {_fmt(m['lag_onset_s']['char_weighted_median'])} |",
        f"| `lag_live` median (s), diagnostic | {_fmt(m['lag_live_s']['median'])} |",
        f"| `lag_end` median (s), diagnostic | {_fmt(m['lag_end_s']['median'])} |",
        f"| Turn end gap (s): dead air / overrun, median of {m['turn_timing_n']} | "
        f"{_fmt(m['turn_dead_air_s']['median'])} / {_fmt(m['turn_overrun_s']['median'])} |",
        f"| Unit length median / p95 (chars) | {_fmt(m['unit_chars']['median'])} / {_fmt(m['unit_chars']['p95'])} |",
        f"| Translation coverage (warm interval) | {_fmt(m['translation_coverage'])} |",
        f"| Fallbacks | {m['fallbacks']} {m['fallback_reasons']} |",
        f"| Catch-up skips (events / units) | {m['catch_ups']} / {m['catch_up_units']} |",
        f"| Debt max (ms) / slope (ms/min) | {_fmt(m['debt_ms']['max'])} / {_fmt(m['debt_ms']['slope_per_min'])} |",
        f"| Oldest source age max (ms) / slope | {_fmt(m['oldest_source_age_ms']['max'])} / {_fmt(m['oldest_source_age_ms']['slope_per_min'])} |",
        f"| Source disposition | {m['source_disposition']} |",
        f"| Alignment uncertainty (ms) | {m['alignment_uncertainty_ms']} |",
        "",
        f"Streaming row checks (thresholds {gate['thresholds']}): "
        + ", ".join(f"{k}={'pass' if v else 'FAIL'}" for k, v in gate["checks"].items()),
        "",
    ]
    return "\n".join(lines)
