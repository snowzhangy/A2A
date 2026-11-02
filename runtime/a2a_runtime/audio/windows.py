"""Windows audio platform layer (ADR-0007): processes, audio sessions, render endpoints,
source resolution, process-loopback sessions, and crash-safe source silencing.
Ordinary threads only.

Capture uses WASAPI process loopback on the source's process tree; render goes through the
shared engine. Muting a session also silences its process-loopback capture (measured with
`a2a-wintap mute-probe`), so translated-only playback instead ducks the source's sessions
to -60 dB and the capture thread multiplies by the exact inverse gain: the audio engine
applies session volume as a float gain before loopback (`a2a-wintap volume-probe`).
Windows persists per-app volume, so every change is journaled to
`.local/state/silenced-sessions.json` before it is applied, and a detached guard, the next
start, or `a2a-wintap restore` repairs what a crashed host left behind.
"""

from __future__ import annotations

import ctypes as C
import ctypes.wintypes as W
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .. import REPO_ROOT, native

if sys.platform != "win32":  # pragma: no cover
    raise ImportError("a2a_runtime.audio.windows is Windows-only")

_k32 = C.WinDLL("kernel32", use_last_error=True)
TH32CS_SNAPPROCESS = 0x2
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
STILL_ACTIVE = 259


class PROCESSENTRY32W(C.Structure):
    _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                ("th32DefaultHeapID", C.c_size_t), ("th32ModuleID", W.DWORD), ("cntThreads", W.DWORD),
                ("th32ParentProcessID", W.DWORD), ("pcPriClassBase", W.LONG), ("dwFlags", W.DWORD),
                ("szExeFile", W.WCHAR * 260)]


_k32.CreateToolhelp32Snapshot.restype = W.HANDLE
_k32.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
_k32.Process32FirstW.argtypes = [W.HANDLE, C.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.argtypes = [W.HANDLE, C.POINTER(PROCESSENTRY32W)]
_k32.OpenProcess.restype = W.HANDLE
_k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
_k32.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
_k32.CloseHandle.argtypes = [W.HANDLE]
_k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
_k32.WaitForSingleObject.restype = W.DWORD


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    exe: str


def processes() -> list[Process]:
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, W.HANDLE(-1).value):
        raise OSError(C.get_last_error(), "CreateToolhelp32Snapshot failed")
    out: list[Process] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = C.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, C.byref(entry))
        while ok:
            out.append(Process(entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            ok = _k32.Process32NextW(snap, C.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return out


def is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = W.DWORD()
        return bool(_k32.GetExitCodeProcess(h, C.byref(code))) and code.value == STILL_ACTIVE
    finally:
        _k32.CloseHandle(h)


def wait_for_exit(pid: int) -> None:
    h = _k32.OpenProcess(SYNCHRONIZE, False, pid)
    if not h:
        return
    try:
        _k32.WaitForSingleObject(h, 0xFFFFFFFF)
    finally:
        _k32.CloseHandle(h)


def process_tree(root: int, procs: list[Process] | None = None) -> set[int]:
    """`root` and every descendant (Chrome and Edge render audio from a child process)."""
    procs = processes() if procs is None else procs
    children: dict[int, list[int]] = {}
    for p in procs:
        if p.pid != p.ppid:
            children.setdefault(p.ppid, []).append(p.pid)
    tree, stack = {root}, [root]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in tree:
                tree.add(c)
                stack.append(c)
    return tree


@dataclass(frozen=True)
class AudioSession:
    pid: int
    state: str
    muted: bool
    volume: float
    display_name: str
    endpoint: str
    exe: str


_SESSION_STATES = {0: "inactive", 1: "active", 2: "expired"}


def audio_sessions() -> list[AudioSession]:
    buf = (native.SessionInfo * 256)()
    n = native.lib().a2a_wasapi_list_sessions(buf, 256)
    if n < 0:
        raise OSError(f"audio session enumeration failed (HRESULT 0x{n & 0xFFFFFFFF:08X})")
    names = {p.pid: p.exe for p in processes()}
    return [AudioSession(s.pid, _SESSION_STATES.get(s.state, str(s.state)), bool(s.muted), s.volume,
                         s.display_name.decode("utf-8", "replace"), s.endpoint.decode("utf-8", "replace"),
                         names.get(s.pid, "")) for s in buf[:n]]


@dataclass(frozen=True)
class Source:
    """A tap target: the root process and the tree whose audio is captured and silenced."""

    root: int
    exe: str
    members: frozenset[int]

    @property
    def label(self) -> str:
        return f"{self.exe} (pid {self.root}, {len(self.members)} process{'es' if len(self.members) != 1 else ''})"


class SourceError(RuntimeError):
    pass


def resolve(pid: int | None = None, exe: str | None = None) -> Source:
    """Resolve a tap target by PID or executable name. A name resolves to the topmost
    process of that name (a browser's main process), never to this runtime itself, and an
    ambiguous name (several unrelated trees) is rejected rather than guessed."""
    procs = processes()
    by_pid = {p.pid: p for p in procs}
    own = process_tree(os.getpid(), procs) | {os.getppid()}
    if pid is not None:
        p = by_pid.get(pid)
        if p is None:
            raise SourceError(f"no process with pid {pid}")
        if pid in own:
            raise SourceError("refusing to tap this runtime's own process tree")
        return Source(pid, p.exe, frozenset(process_tree(pid, procs)))
    if not exe:
        raise SourceError("pass a process ID or an executable name")
    want = exe.lower() if exe.lower().endswith(".exe") else exe.lower() + ".exe"
    matches = [p for p in procs if p.exe.lower() == want and p.pid not in own]
    if not matches:
        raise SourceError(f"{want} is not running")
    roots = [p for p in matches if by_pid.get(p.ppid) is None or by_pid[p.ppid].exe.lower() != want]
    if len(roots) > 1:
        # Prefer the single tree that owns an audio session; otherwise refuse to guess.
        with_audio = {s.pid for s in audio_sessions()}
        audible = [r for r in roots if process_tree(r.pid, procs) & with_audio]
        if len(audible) == 1:
            roots = audible
        else:
            raise SourceError(f"{want} has {len(roots)} separate process trees "
                              f"(pids {', '.join(str(r.pid) for r in roots)}); pass --tap-pid")
    root = roots[0]
    return Source(root.pid, root.exe, frozenset(process_tree(root.pid, procs)))


# MARK: Render endpoints


@dataclass(frozen=True)
class Endpoint:
    id: str
    name: str
    is_default: bool


def endpoints() -> list[Endpoint]:
    buf = (native.EndpointInfo * 64)()
    n = native.lib().a2a_wasapi_list_endpoints(buf, 64)
    if n < 0:
        raise OSError(f"endpoint enumeration failed (HRESULT 0x{n & 0xFFFFFFFF:08X})")
    return [Endpoint(e.id.decode("utf-8", "replace"), e.name.decode("utf-8", "replace"), bool(e.is_default))
            for e in buf[:n]]


def resolve_endpoint(selector: str | None) -> Endpoint | None:
    """None selects the default endpoint; otherwise an exact ID, an ID suffix (the GUID), or
    a unique case-insensitive substring of the device name."""
    if not selector:
        return None
    eps = endpoints()
    for e in eps:
        if e.id == selector or e.id.lower().endswith(selector.lower().strip("{}") + "}"):
            return e
    matches = [e for e in eps if selector.lower() in e.name.lower()]
    if len(matches) == 1:
        return matches[0]
    raise SourceError(f"output device {selector!r} matches {len(matches)} endpoints: "
                      + "; ".join(f"{e.name} [{e.id}]" for e in eps))


def default_render_id() -> str:
    buf = C.create_string_buffer(1024)
    native.lib().a2a_wasapi_default_render_id(buf, 1024)
    return buf.value.decode("utf-8", "replace")


# MARK: Process-loopback session


class WasapiSession:
    """Owns one a2a_wasapi engine thread over an existing native.Engine."""

    def __init__(self, engine: native.Engine, target_pid: int, *, exclude_tree: bool = False, render: bool = True,
                 sample_rate: int = 48_000, capture_buffer_ms: int = 200, render_buffer_ms: int = 20,
                 capture_gain: float = 1.0, render_device_id: str | None = None) -> None:
        cfg = native.WasapiConfig(target_pid, native.LOOPBACK_EXCLUDE_TREE if exclude_tree else native.LOOPBACK_INCLUDE_TREE,
                                  1 if render else 0, sample_rate, engine.channels, capture_buffer_ms, render_buffer_ms,
                                  capture_gain, (render_device_id or "").encode("utf-8"))
        self.engine = engine
        self.h = native.lib().a2a_wasapi_start(engine.h, C.byref(cfg))
        if not self.h:
            raise OSError("could not start the WASAPI engine thread")
        stats = self.stats()
        if stats["state"] != native.WASAPI_RUNNING:
            message = stats["message"] or "audio clients did not start"
            self.stop()
            raise OSError(message)

    def stats(self) -> dict:
        s = native.WasapiStats()
        native.lib().a2a_wasapi_get_stats(self.h, C.byref(s))
        out = {name: getattr(s, name) for name, _ in native.WasapiStats._fields_}
        out["message"] = s.message.decode("utf-8", "replace")
        out["render_device"] = s.render_device.decode("utf-8", "replace")
        return out

    def set_capture_gain(self, gain: float) -> None:
        native.lib().a2a_wasapi_set_capture_gain(self.h, float(gain))

    @property
    def running(self) -> bool:
        return self.h is not None and self.stats()["state"] == native.WASAPI_RUNNING

    def stop(self) -> None:
        if self.h:
            native.lib().a2a_wasapi_stop(self.h)
            self.h = None


# MARK: Crash-safe source silencing

JOURNAL = REPO_ROOT / ".local" / "state" / "silenced-sessions.json"
DEFAULT_DUCK_DB = -60.0


def _write_journal(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"entries": entries}, indent=1))
    os.replace(tmp, path)


def _read_journal(path: Path) -> list[dict]:
    try:
        return list(json.loads(path.read_text()).get("entries", []))
    except (OSError, ValueError):
        return []


def _pid_array(pids):
    pids = sorted(pids)
    return (C.c_uint32 * max(1, len(pids)))(*pids), len(pids)


def set_mute(pids: set[int] | frozenset[int], mute: bool) -> int:
    arr, n = _pid_array(pids)
    r = native.lib().a2a_wasapi_set_session_mute(arr, n, 1 if mute else 0)
    if r < 0:
        raise OSError(f"session mute failed (HRESULT 0x{r & 0xFFFFFFFF:08X})")
    return r


def set_volume(pids: set[int] | frozenset[int], level: float) -> int:
    arr, n = _pid_array(pids)
    r = native.lib().a2a_wasapi_set_session_volume(arr, n, float(level))
    if r < 0:
        raise OSError(f"session volume failed (HRESULT 0x{r & 0xFFFFFFFF:08X})")
    return r


class SourceSilencer:
    """Keeps the source tree inaudible while it is tapped, and restores it afterwards.

    `duck` (default) lowers each session to `duck_db`; `capture_gain` undoes it exactly.
    `mute` silences the sessions, which also silences process-loopback capture, so it is
    only for probes. Every change is journaled (pid, exe, original value) before it is
    applied. Sessions the app opens later, and volume changes made by the app or the user,
    are caught by `refresh`, which the driver calls every 250 ms.
    """

    def __init__(self, source: Source, mode: str = "duck", duck_db: float = DEFAULT_DUCK_DB,
                 journal: Path = JOURNAL) -> None:
        if mode not in ("duck", "mute"):
            raise ValueError("mode is duck or mute")
        self.source = source
        self.mode = mode
        self.level = 10 ** (duck_db / 20)
        self.journal = journal
        self._changed: dict[int, dict] = {}
        self.foreign_changes = 0

    @property
    def capture_gain(self) -> float:
        return 1.0 / self.level if self.mode == "duck" else 1.0

    def refresh(self) -> int:
        """Silence sessions of the tree not silenced yet and re-assert ones that moved.
        Returns the number of sessions changed."""
        members = set(self.source.members)
        if is_alive(self.source.root):
            members |= process_tree(self.source.root)
        sessions = [s for s in audio_sessions() if s.pid in members]
        new = []
        for s in sessions:
            prior = self._changed.get(s.pid)
            if self.mode == "duck":
                if prior is None:
                    new.append({"pid": s.pid, "exe": s.exe, "kind": "duck", "volume": s.volume, "level": self.level})
                elif abs(s.volume - self.level) > 1e-6:
                    self.foreign_changes += 1  # an app or mixer volume control moved it
                    set_volume({s.pid}, self.level)
            elif prior is None and not s.muted:
                new.append({"pid": s.pid, "exe": s.exe, "kind": "mute", "volume": s.volume})
        if not new:
            return 0
        stamp = {"host_pid": os.getpid(), "at": time.time()}
        pids = {e["pid"] for e in new}
        entries = [e for e in _read_journal(self.journal) if e.get("pid") not in pids]
        _write_journal(self.journal, entries + [{**e, **stamp} for e in new])  # journal before changing
        changed = set_volume(pids, self.level) if self.mode == "duck" else set_mute(pids, True)
        for e in new:
            self._changed[e["pid"]] = e
        return changed

    def restore(self) -> int:
        if not self._changed:
            return 0
        restored = 0
        for pid, e in self._changed.items():
            restored += set_volume({pid}, e["volume"]) if e["kind"] == "duck" else set_mute({pid}, False)
        live = {s.pid for s in audio_sessions()}
        # A session whose process already exited keeps its entry for repair on the next launch.
        done = {pid for pid in self._changed if pid in live or is_alive(pid)}
        _write_journal(self.journal, [e for e in _read_journal(self.journal) if e.get("pid") not in done])
        self._changed.clear()
        return restored


def restore_stranded(journal: Path = JOURNAL, all_hosts: bool = False) -> list[dict]:
    """Undo silencing left by a host that is no longer running. An entry whose process has
    exited is matched by exe name to a session still at the duck level (Windows restores the
    persisted per-app volume when the app starts again)."""
    entries = _read_journal(journal)
    if not entries:
        return []
    sessions = audio_sessions()
    by_pid = {s.pid: s for s in sessions}
    repaired, keep = [], []
    for e in entries:
        host = int(e.get("host_pid", 0))
        if not all_hosts and host != os.getpid() and is_alive(host):
            keep.append(e)
            continue
        pid = int(e["pid"])
        if e.get("kind") == "mute":
            targets = {pid} if pid in by_pid else {s.pid for s in sessions if s.exe == e.get("exe") and s.muted}
            if targets:
                set_mute(targets, False)
        else:
            level = float(e.get("level", 0))
            targets = {pid} if pid in by_pid else {
                s.pid for s in sessions if s.exe == e.get("exe") and abs(s.volume - level) < 1e-6}
            if targets:
                set_volume(targets, float(e.get("volume", 1.0)))
        if not targets and not is_alive(pid):
            keep.append({**e, "host_pid": 0})  # not running now: repair its next launch
            continue
        repaired.append(e)
    _write_journal(journal, keep)
    return repaired


def pending_repairs(journal: Path = JOURNAL) -> list[dict]:
    return _read_journal(journal)


def spawn_guard() -> None:
    """Start a detached watcher that restores silenced sessions if this host dies. A dead
    host cannot repair itself; the guard waits on its process handle (cf. A3 on macOS)."""
    import subprocess

    flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen([sys.executable, "-m", "a2a_runtime.tools.wintap", "guard", "--host-pid", str(os.getpid())],
                     creationflags=flags, close_fds=True, cwd=str(REPO_ROOT / "runtime"),
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
