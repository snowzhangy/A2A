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
import msvcrt
import os
import sys
import threading
import time
from contextlib import contextmanager
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


class FILETIME(C.Structure):
    _fields_ = [("dwLowDateTime", W.DWORD), ("dwHighDateTime", W.DWORD)]


_k32.GetProcessTimes.argtypes = [W.HANDLE, C.POINTER(FILETIME), C.POINTER(FILETIME),
                                C.POINTER(FILETIME), C.POINTER(FILETIME)]
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


def process_start_100ns(pid: int) -> int | None:
    """Process creation timestamp, used to distinguish a reused PID from the original process."""
    if pid <= 0:
        return None
    h = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if not _k32.GetProcessTimes(h, C.byref(created), C.byref(exited), C.byref(kernel), C.byref(user)):
            return None
        return (created.dwHighDateTime << 32) | created.dwLowDateTime
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
    session_id: str


_SESSION_STATES = {0: "inactive", 1: "active", 2: "expired"}


def audio_sessions() -> list[AudioSession]:
    buf = (native.SessionInfo * 256)()
    n = native.lib().a2a_wasapi_list_sessions(buf, 256)
    if n < 0:
        raise OSError(f"audio session enumeration failed (HRESULT 0x{n & 0xFFFFFFFF:08X})")
    names = {p.pid: p.exe for p in processes()}
    return [AudioSession(s.pid, _SESSION_STATES.get(s.state, str(s.state)), bool(s.muted), s.volume,
                         s.display_name.decode("utf-8", "replace"), s.endpoint.decode("utf-8", "replace"),
                         names.get(s.pid, ""), s.session_id.decode("utf-8", "replace")) for s in buf[:n]]


@dataclass(frozen=True)
class Source:
    """A tap target: the root process and the tree whose audio is captured and silenced.

    `global_mix` is the whole output mix (the Swift driver's `--tap-global`): `root` is this
    runtime, whose tree is the one excluded from capture and never silenced."""

    root: int
    exe: str
    members: frozenset[int]
    global_mix: bool = False

    @property
    def label(self) -> str:
        if self.global_mix:
            return "the whole output mix (excluding this runtime)"
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


def system_audio() -> Source:
    """Every process's audio except this runtime's own tree (its playback and its workers)."""
    return Source(os.getpid(), "system audio", frozenset(), global_mix=True)


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
    """Atomically replace the journal. Callers hold `_journal_lock` for read/modify/write cycles."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps({"entries": entries}, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def _journal_lock(path: Path):
    """Serialize hosts and guards on Windows; the lock file itself is never replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "a+b") as lock:
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _read_journal(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as error:
        raise SourceError(f"cannot read audio recovery journal {path}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list) or not all(
        isinstance(entry, dict) for entry in data["entries"]
    ):
        raise SourceError(f"invalid audio recovery journal {path}; preserve it for manual recovery")
    return data["entries"]


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


def set_session_volume(session_id: str, level: float) -> int:
    r = native.lib().a2a_wasapi_set_session_volume_by_id(session_id.encode("utf-8"), float(level))
    if r < 0:
        raise OSError(f"session volume failed (HRESULT 0x{r & 0xFFFFFFFF:08X})")
    return r


def set_session_mute(session_id: str, mute: bool) -> int:
    r = native.lib().a2a_wasapi_set_session_mute_by_id(session_id.encode("utf-8"), 1 if mute else 0)
    if r < 0:
        raise OSError(f"session mute failed (HRESULT 0x{r & 0xFFFFFFFF:08X})")
    return r


def _same_process(entry: dict, pid: int) -> bool:
    # The Windows system-sounds audio session has PID 0 and no process handle.
    # Its endpoint-specific session identifier is the only available identity.
    if pid == 0:
        return entry.get("pid") == 0 and bool(entry.get("session_id"))
    if not is_alive(pid):
        return False
    recorded = entry.get("process_start_100ns")
    return recorded is None or process_start_100ns(pid) == recorded


class SourceSilencer:
    """Duck and restore exact audio-session identities, with a locked crash-recovery journal.

    A PID is not an identity: Windows can reuse it, and one process can own sessions on several
    endpoints. Every entry therefore carries the native session identifier and process creation
    time. Missing sessions stay journaled for the guard instead of being mistaken for restored.
    """

    def __init__(self, source: Source, mode: str = "duck", duck_db: float = DEFAULT_DUCK_DB,
                 journal: Path = JOURNAL) -> None:
        if mode not in ("duck", "mute"):
            raise ValueError("mode is duck or mute")
        self.source = source
        self.mode = mode
        self.level = 10 ** (duck_db / 20)
        self.journal = journal
        self._changed: dict[str, dict] = {}
        self.foreign_changes = 0

    @property
    def capture_gain(self) -> float:
        return 1.0 / self.level if self.mode == "duck" else 1.0

    def _sessions(self) -> list[AudioSession]:
        if self.source.global_mix:
            own = process_tree(self.source.root) | {os.getppid()}
            return [s for s in audio_sessions() if s.pid != 0 and s.pid not in own]
        members = set(self.source.members)
        if is_alive(self.source.root):
            members |= process_tree(self.source.root)
        return [s for s in audio_sessions() if s.pid in members]

    def refresh(self) -> int:
        """Duck new sessions and re-assert external changes without touching another session."""
        new = []
        for s in self._sessions():
            if not s.session_id:
                continue  # an unidentifiable session is never changed by PID guesswork
            prior = self._changed.get(s.session_id)
            if self.mode == "duck":
                if prior is None:
                    new.append({"pid": s.pid, "exe": s.exe, "kind": "duck", "volume": s.volume,
                                "level": self.level, "endpoint": s.endpoint, "session_id": s.session_id,
                                "process_start_100ns": process_start_100ns(s.pid)})
                elif abs(s.volume - self.level) > 1e-6:
                    self.foreign_changes += 1
                    set_session_volume(s.session_id, self.level)
            elif prior is None and not s.muted:
                new.append({"pid": s.pid, "exe": s.exe, "kind": "mute", "volume": s.volume,
                            "endpoint": s.endpoint, "session_id": s.session_id,
                            "process_start_100ns": process_start_100ns(s.pid)})
        if not new:
            return 0
        stamp = {"host_pid": os.getpid(), "at": time.time()}
        new = [{**entry, **stamp} for entry in new]
        new_ids = {entry["session_id"] for entry in new}
        with _journal_lock(self.journal):
            entries = _read_journal(self.journal)
            for entry in entries:
                host = int(entry.get("host_pid", 0))
                if entry.get("session_id") in new_ids and host != os.getpid() and is_alive(host):
                    raise SourceError("another running A2A host already owns this audio session")
            entries = [entry for entry in entries if entry.get("session_id") not in new_ids]
            _write_journal(self.journal, entries + new)  # journal before changing
        changed = 0
        for entry in new:
            session_id = entry["session_id"]
            # Include the attempted change even if the native setter reports an error after
            # changing the session, so normal cleanup can still try to restore it.
            self._changed[session_id] = entry
            if self.mode == "duck":
                changed += set_session_volume(session_id, self.level)
            else:
                changed += set_session_mute(session_id, True)
        return changed

    def restore(self) -> int:
        if not self._changed:
            return 0
        restored = 0
        done = set()
        current = {s.session_id: s for s in audio_sessions()}
        for session_id, entry in self._changed.items():
            session = current.get(session_id)
            if session is None or not _same_process(entry, session.pid):
                continue  # the detached guard repairs a later session/relaunch
            if entry["kind"] == "duck":
                if abs(session.volume - float(entry["level"])) > 1e-6:
                    done.add(session_id)  # the user or another application changed it
                    continue
                changed = set_session_volume(session_id, entry["volume"])
            else:
                if not session.muted:
                    done.add(session_id)
                    continue
                changed = set_session_mute(session_id, False)
            restored += changed
            if changed > 0:
                done.add(session_id)
        with _journal_lock(self.journal):
            entries = [entry for entry in _read_journal(self.journal)
                       if not (entry.get("session_id") in done and int(entry.get("host_pid", 0)) == os.getpid())]
            _write_journal(self.journal, entries)
        self._changed.clear()
        return restored


def restore_stranded(journal: Path = JOURNAL, all_hosts: bool = False) -> list[dict]:
    """Repair dead hosts without restoring a reused PID or dropping an unresolved entry."""
    with _journal_lock(journal):
        entries = _read_journal(journal)
        if not entries:
            return []
        sessions = audio_sessions()
        by_id = {s.session_id: s for s in sessions if s.session_id}
        by_pid = {s.pid: s for s in sessions}
        repaired, keep = [], []
        for entry in entries:
            host = int(entry.get("host_pid", 0))
            if not all_hosts and host != os.getpid() and is_alive(host):
                keep.append(entry)
                continue
            pid = int(entry["pid"])
            pid_alive = is_alive(pid)
            original_alive = pid_alive and _same_process(entry, pid)
            target = by_id.get(entry.get("session_id", ""))
            if target is not None and (target.pid != pid or not _same_process(entry, target.pid)):
                target = None  # session identifiers are only valid with their original process
            if target is None and original_alive and not entry.get("session_id"):
                candidate = by_pid.get(pid)
                if candidate is not None and candidate.session_id:
                    target = candidate  # legacy journal entry, guarded by process creation time
            candidates = [target] if target is not None else []
            if not candidates and not pid_alive:
                # Windows persists per-app volume across relaunch. Only a session still at our
                # exact duck/mute state is eligible; another app at normal volume is never touched.
                level = float(entry.get("level", 0))
                candidates = [s for s in sessions if s.exe == entry.get("exe") and s.session_id
                              and (s.muted if entry.get("kind") == "mute"
                                   else abs(s.volume - level) < 1e-6)]
                # An executable may have multiple sessions with different original levels.
                # Guessing which is which could overwrite an unrelated session's volume.
                matching_entries = [other for other in entries if other.get("exe") == entry.get("exe")
                                    and other.get("kind") == entry.get("kind")]
                if len(candidates) != 1 or len(matching_entries) != 1:
                    candidates = []
            if target is not None:
                still_ducked = (target.muted if entry.get("kind") == "mute"
                                else abs(target.volume - float(entry.get("level", 0))) < 1e-6)
                if not still_ducked:
                    repaired.append(entry)  # already restored, or the user changed it after the host died
                    continue
            changed = 0
            for session in candidates:
                if entry.get("kind") == "mute":
                    if session.muted:
                        changed += set_session_mute(session.session_id, False)
                elif abs(session.volume - float(entry.get("level", 0))) < 1e-6:
                    changed += set_session_volume(session.session_id, float(entry.get("volume", 1.0)))
            if changed > 0:
                repaired.append(entry)
            else:
                # Alive but sessionless, already exited, or reused PID: keep repair pending.
                keep.append(entry if original_alive else {**entry, "host_pid": 0})
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
