"""Hy direct-audio worker protocol tests using the shared standard-library fake worker."""

import struct
import sys

import numpy as np
import pytest

from a2a_runtime import REPO_ROOT
from a2a_runtime.core.types import UnitID
from a2a_runtime.models.base import AdapterError
from a2a_runtime.workers.hy import HyLocalTranslator, HySimulTranslator

FAKE = REPO_ROOT / "research" / "hy_worker" / "fake_worker.py"


def local(*extra):
    return HyLocalTranslator([sys.executable, str(FAKE), *extra], "zh-en", ready_timeout=5)


def test_streamed_span_answer_is_correlated_and_includes_worker_stats():
    worker = local()
    try:
        assert worker.info.streams_spans
        worker.stream(np.ones(28_800, dtype=np.float32))
        ticket = worker.close(np.ones(19_200, dtype=np.float32), 48_000)
        answer = worker.answer(ticket, UnitID(1))
        assert answer.text == "1.0 s zh-en"
        assert answer.worker_stats.turns == 1 and answer.worker_stats.after_close_ms == 0
        whole = worker.translate(UnitID(2), np.ones(24_000, dtype=np.float32))
        assert whole.text == "0.5 s zh-en"
        assert worker.translate(UnitID(3), np.zeros(4_800, dtype=np.float32)).text == ""
    finally:
        worker.shutdown()


def test_discard_and_protocol_one_capability_are_explicit():
    worker = local()
    try:
        worker.stream(np.ones(48_000, dtype=np.float32))
        worker.discard()
        ticket = worker.close(np.ones(4_800, dtype=np.float32), 4_800)
        assert worker.answer(ticket, UnitID(1)).text == "0.1 s zh-en"
    finally:
        worker.shutdown()
    old = local("--protocol-1")
    try:
        assert not old.info.streams_spans
    finally:
        old.shutdown()


def test_worker_disconnect_and_answer_timeout_are_failures():
    worker = local("--die-on-flush")
    try:
        ticket = worker.close(np.ones(4_800, dtype=np.float32), 4_800)
        with pytest.raises(AdapterError):
            worker.answer(ticket, UnitID(1))
        assert worker.failure
    finally:
        worker.shutdown()
    slow = local("--slow", "0.2")
    try:
        ticket = slow.close(np.ones(4_800, dtype=np.float32), 4_800)
        with pytest.raises(AdapterError, match="timed out"):
            slow.answer(ticket, UnitID(1), timeout=0.01)
    finally:
        slow.shutdown()


def test_malformed_worker_frame_fails_startup(tmp_path):
    script = tmp_path / "bad_worker.py"
    script.write_text("""import struct,sys,time
sys.stdout.buffer.write(struct.pack('<II', 2147483647, 0))
sys.stdout.buffer.flush()
time.sleep(1)
""", encoding="utf-8")
    with pytest.raises(AdapterError):
        HyLocalTranslator([sys.executable, str(script)], "zh-en", ready_timeout=2)


def test_remote_event_parser_accepts_deltas_final_text_and_empty_answers():
    candidate = HySimulTranslator.parse_events(
        'data: {"delta":"Hello"}\ndata: {"delta":"world","done":true}\n', UnitID(1))
    assert candidate.text == "Hello world"
    final = HySimulTranslator.parse_events('data: {"done":true,"text":"Finished"}\n', UnitID(2))
    assert final.text == "Finished"
    empty = HySimulTranslator.parse_events('data: {"done":true,"text":""}\n', UnitID(3))
    assert empty.text == ""
    with pytest.raises(AdapterError, match="no completed"):
        HySimulTranslator.parse_events('data: {"delta":"unfinished"}\n', UnitID(4))
    with pytest.raises(AdapterError, match="malformed"):
        HySimulTranslator.parse_events('data: nope\n', UnitID(5))
