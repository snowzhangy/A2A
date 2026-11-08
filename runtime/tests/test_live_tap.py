"""LiveTap's health checks: the OS polling that costs tens of milliseconds (process snapshot,
audio-session enumeration, default device) runs on the watcher thread, never on the driver
thread that drains the capture ring. Fake OS calls; no devices."""

from __future__ import annotations

import sys
import threading
import time
from types import SimpleNamespace

import pytest

from a2a_runtime import native

pytestmark = [pytest.mark.skipif(sys.platform != "win32", reason="Windows live host"),
              pytest.mark.skipif(not native.library_path().exists(), reason="native library not built")]


class FakeOS:
    """The parts of audio.windows LiveTap calls. Slow calls record their thread."""

    def __init__(self) -> None:
        self.slow_threads: set[str] = set()
        self.default = "{device-a}"
        self.alive = {10: True, 99: True}
        self.sessions_started: list[str | None] = []
        self.excluded: list[bool] = []
        self.refreshes = 0
        fake = self

        class Silencer:
            capture_gain = 1000.0
            level = 0.001
            foreign_changes = 0

            def __init__(self, source, duck_db=-60):
                self.restored = False

            def refresh(self):
                fake._slow()
                fake.refreshes += 1

            def restore(self):
                self.restored = True

        class Session:
            def __init__(self, engine, root, render, capture_gain, render_device_id=None, exclude_tree=False):
                fake.sessions_started.append(fake.default)
                fake.excluded.append(exclude_tree)
                self.frames = 0

            def stats(self):
                self.frames += 480
                return {"state": native.WASAPI_RUNNING, "message": "", "capture_frames": self.frames,
                        "render_device": fake.default}

            def stop(self):
                pass

        self.SourceSilencer = Silencer
        self.WasapiSession = Session

    def _slow(self) -> None:
        self.slow_threads.add(threading.current_thread().name)
        time.sleep(0.03)

    def resolve_endpoint(self, name):
        return None

    def spawn_guard(self):
        pass

    def default_render_id(self):
        self._slow()
        return self.default

    def is_alive(self, pid):
        return self.alive.get(pid, False)


@pytest.fixture
def tap(monkeypatch):
    import a2a_runtime.audio
    from a2a_runtime.audio import live

    fake = FakeOS()
    monkeypatch.setattr(a2a_runtime.audio, "windows", fake, raising=False)
    monkeypatch.setitem(sys.modules, "a2a_runtime.audio.windows", fake)
    t = live.LiveTap(SimpleNamespace(root=10, members={10}, exe="player.exe"), render=True, silence=True,
                     duck_db=-60, output_device=None, parent_pid=99, translated_ring_seconds=1.0)
    assert t.win is fake
    t.open()
    yield t, fake
    t.stop()


def poll(t, seconds: float) -> list:
    reasons, deadline = [], time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        reason = t.health_check()
        if reason:
            reasons.append(reason)
        time.sleep(0.005)
    return reasons


def test_enumeration_runs_on_the_watcher_thread_only(tap):
    t, fake = tap
    fake.slow_threads.clear()
    started = time.perf_counter()
    calls = 0
    while time.perf_counter() - started < 0.6:
        t0 = time.perf_counter()
        assert t.health_check() is None
        assert time.perf_counter() - t0 < 0.01  # never the 30 ms fake enumeration
        calls += 1
        time.sleep(0.002)
    assert calls > 100
    assert fake.slow_threads == {"a2a.tap-watch"}
    assert fake.refreshes >= 4  # new source sessions keep being ducked


def test_watcher_findings_stop_or_redirect_the_session(tap):
    t, fake = tap
    fake.default = "{device-b}"
    assert poll(t, 0.7) == []
    assert fake.sessions_started == ["{device-a}", "{device-b}"] and t.device_changes == 1
    assert t.take_discontinuity().startswith("output device changed")
    fake.alive[10] = False
    assert any("source app exited" in r for r in poll(t, 0.7))


def test_stop_joins_the_watcher_before_restoring(tap):
    t, fake = tap
    t.stop()
    assert t._watcher is None and t.silencer.restored
    count = fake.refreshes
    time.sleep(0.25)
    assert fake.refreshes == count  # no duck after the restore


def test_the_whole_mix_excludes_this_runtime_and_has_no_source_app_to_lose(monkeypatch):
    import a2a_runtime.audio
    from a2a_runtime.audio import live

    fake = FakeOS()
    monkeypatch.setattr(a2a_runtime.audio, "windows", fake, raising=False)
    monkeypatch.setitem(sys.modules, "a2a_runtime.audio.windows", fake)
    mix = SimpleNamespace(root=10, members=frozenset(), exe="system audio", global_mix=True)
    t = live.LiveTap(mix, render=True, silence=True, duck_db=-60, output_device=None, parent_pid=99,
                     translated_ring_seconds=1.0)
    t.open()
    try:
        assert fake.excluded == [True]  # exclude-tree loopback of our own pid: everything else
        fake.alive[10] = False  # nothing in an empty member set can exit
        assert poll(t, 0.5) == []
        t._last_capture -= 5  # a paused video leaves the mix without frames: not a fault
        assert not t.capture_stalled()
        assert t.summary()["global"] is True
    finally:
        t.stop()
