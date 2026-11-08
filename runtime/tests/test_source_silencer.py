"""SourceSilencer recovery identity and journal races; fake OS calls, no audio devices."""

import json
import os
import sys
import threading

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows audio recovery")

if sys.platform == "win32":
    from a2a_runtime.audio import windows as w


def session(pid=10, session_id="session-a", volume=0.8, exe="player.exe", muted=False):
    return w.AudioSession(pid, "active", muted, volume, "", "endpoint-a", exe, session_id)


def source():
    return w.Source(10, "player.exe", frozenset({10}))


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    state = {"sessions": [session()], "alive": {10: True}, "starts": {10: 100}, "writes": []}
    monkeypatch.setattr(w, "audio_sessions", lambda: list(state["sessions"]))
    monkeypatch.setattr(w, "process_tree", lambda root, procs=None: {root})
    monkeypatch.setattr(w, "is_alive", lambda pid: state["alive"].get(pid, False))
    monkeypatch.setattr(w, "process_start_100ns", lambda pid: state["starts"].get(pid))
    monkeypatch.setattr(w, "set_session_volume", lambda session_id, level: state["writes"].append((session_id, level)) or 1)
    monkeypatch.setattr(w, "set_session_mute", lambda session_id, mute: state["writes"].append((session_id, mute)) or 1)
    return state, tmp_path / "journal.json"


def entries(path):
    return json.loads(path.read_text(encoding="utf-8"))["entries"] if path.exists() else []


def test_missing_session_is_not_mistaken_for_restored(recovery):
    state, journal = recovery
    silencer = w.SourceSilencer(source(), journal=journal)
    assert silencer.refresh() == 1
    state["sessions"] = []  # process alive, but its audio session has not reappeared yet
    assert silencer.restore() == 0
    assert len(entries(journal)) == 1
    assert entries(journal)[0]["session_id"] == "session-a"


def test_reused_pid_never_restores_the_new_process(recovery):
    state, journal = recovery
    w._write_journal(journal, [{"host_pid": 0, "pid": 10, "exe": "player.exe", "kind": "duck",
                                "volume": 0.8, "level": 0.001, "session_id": "old-session",
                                "process_start_100ns": 100}])
    state["sessions"] = [session(session_id="new-session", volume=0.001)]
    state["starts"][10] = 200
    assert w.restore_stranded(journal) == []
    assert state["writes"] == []
    assert len(entries(journal)) == 1


def test_already_restored_session_closes_journal_without_overwriting_volume(recovery):
    state, journal = recovery
    w._write_journal(journal, [{"host_pid": 0, "pid": 10, "exe": "player.exe", "kind": "duck",
                                "volume": 0.8, "level": 0.001, "session_id": "session-a",
                                "process_start_100ns": 100}])
    assert len(w.restore_stranded(journal)) == 1
    assert state["writes"] == [] and entries(journal) == []


def test_relaunch_at_the_duck_level_is_restored_by_session_id(recovery):
    state, journal = recovery
    w._write_journal(journal, [{"host_pid": 0, "pid": 10, "exe": "player.exe", "kind": "duck",
                                "volume": 0.8, "level": 0.001, "session_id": "old-session",
                                "process_start_100ns": 100}])
    state["alive"][10] = False
    state["sessions"] = [session(pid=20, session_id="new-session", volume=0.001)]
    state["alive"][20] = True
    state["starts"][20] = 200
    repaired = w.restore_stranded(journal)
    assert len(repaired) == 1 and state["writes"] == [("new-session", 0.8)]
    assert entries(journal) == []


def test_running_other_host_is_preserved_and_blocks_conflicting_duck(recovery, monkeypatch):
    state, journal = recovery
    other_host = os.getpid() + 1000
    state["alive"][other_host] = True
    w._write_journal(journal, [{"host_pid": other_host, "pid": 10, "exe": "player.exe", "kind": "duck",
                                "volume": 0.8, "level": 0.001, "session_id": "session-a"}])
    assert w.restore_stranded(journal) == []
    assert len(entries(journal)) == 1
    with pytest.raises(w.SourceError, match="another running A2A host"):
        w.SourceSilencer(source(), journal=journal).refresh()
    assert state["writes"] == []


def test_concurrent_refreshes_do_not_lose_journal_entries(recovery):
    state, journal = recovery
    state["sessions"] = [session(session_id="session-a"), session(pid=11, session_id="session-b")]
    state["alive"][11] = True
    state["starts"][11] = 110
    first = w.SourceSilencer(source(), journal=journal)
    second = w.SourceSilencer(w.Source(11, "player.exe", frozenset({11})), journal=journal)
    threads = [threading.Thread(target=s.refresh) for s in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {entry["session_id"] for entry in entries(journal)} == {"session-a", "session-b"}


def test_missing_exact_session_does_not_restore_another_session_of_same_process(recovery):
    state, journal = recovery
    w._write_journal(journal, [{"host_pid": 0, "pid": 10, "exe": "player.exe", "kind": "duck",
                                "volume": 0.8, "level": 0.001, "session_id": "old-session",
                                "process_start_100ns": 100}])
    state["sessions"] = [session(session_id="other-session", volume=0.001)]
    assert w.restore_stranded(journal) == []
    assert state["writes"] == [] and len(entries(journal)) == 1


def test_normal_restore_preserves_external_volume_change(recovery):
    state, journal = recovery
    silencer = w.SourceSilencer(source(), journal=journal)
    silencer.refresh()
    state["writes"].clear()
    state["sessions"] = [session(volume=0.5)]
    assert silencer.restore() == 0
    assert state["writes"] == [] and entries(journal) == []


def test_ambiguous_relaunch_does_not_guess_a_session(recovery):
    state, journal = recovery
    w._write_journal(journal, [{"host_pid": 0, "pid": 10, "exe": "player.exe", "kind": "duck",
                                "volume": 0.8, "level": 0.001, "session_id": "old-session",
                                "process_start_100ns": 100}])
    state["alive"][10] = False
    state["sessions"] = [session(pid=20, session_id="new-a", volume=0.001),
                         session(pid=21, session_id="new-b", volume=0.001)]
    assert w.restore_stranded(journal) == []
    assert state["writes"] == [] and len(entries(journal)) == 1


def test_corrupt_journal_blocks_ducking_without_overwriting_evidence(recovery):
    state, journal = recovery
    journal.write_text("{broken", encoding="utf-8")
    with pytest.raises(w.SourceError, match="recovery journal"):
        w.SourceSilencer(source(), journal=journal).refresh()
    assert journal.read_text(encoding="utf-8") == "{broken"
    assert state["writes"] == []


def test_setter_failure_after_changing_session_can_still_be_restored(recovery, monkeypatch):
    state, journal = recovery
    silencer = w.SourceSilencer(source(), journal=journal)

    def fail_after_duck(session_id, level):
        state["sessions"] = [session(session_id=session_id, volume=level)]
        raise OSError("setter failed after changing session")

    monkeypatch.setattr(w, "set_session_volume", fail_after_duck)
    with pytest.raises(OSError, match="setter failed"):
        silencer.refresh()
    monkeypatch.setattr(w, "set_session_volume", lambda session_id, level: state["writes"].append((session_id, level)) or 1)
    assert silencer.restore() == 1
    assert state["writes"] == [("session-a", 0.8)] and entries(journal) == []


def test_stranded_system_sound_session_restores_exact_identity(recovery):
    state, journal = recovery
    w._write_journal(journal, [{"host_pid": 0, "pid": 0, "exe": "[System Process]", "kind": "duck",
                                "volume": 1.0, "level": 0.001, "session_id": "system-sounds",
                                "process_start_100ns": None}])
    state["sessions"] = [session(pid=0, session_id="system-sounds", volume=0.001, exe="[System Process]")]
    assert len(w.restore_stranded(journal)) == 1
    assert state["writes"] == [("system-sounds", 1.0)] and entries(journal) == []


def test_global_mix_does_not_duck_system_sounds(recovery):
    state, journal = recovery
    state["sessions"] = [session(pid=0, session_id="system-sounds", exe="[System Process]")]
    silencer = w.SourceSilencer(w.Source(os.getpid(), "python.exe", frozenset({os.getpid()}), global_mix=True),
                                journal=journal)
    assert silencer.refresh() == 0
    assert state["writes"] == [] and entries(journal) == []
