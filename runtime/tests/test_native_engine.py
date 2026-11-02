"""Port of apple/Tests/A2AAudioTests/RTShimTests.swift and the portable parts of
AudioBoundaryTests.swift, run against the same C sources compiled for this platform."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from a2a_runtime import native

pytestmark = pytest.mark.skipif(not native.library_path().exists(), reason="native library not built")


def make_engine(channels=1, max_block=512, max_delay=4_800, fade=0) -> native.Engine:
    return native.Engine(channels=channels, capture_ring_frames=8_192, max_delay_frames=max_delay,
                         translated_ring_frames=8_192, max_block_frames=max_block, record_capacity=1_024,
                         rendered_ring_frames=8_192, fade_frames=fade)


def render(e: native.Engine, blocks: int, frames: int = 64) -> np.ndarray:
    return np.concatenate([e.process(None, frames) for _ in range(blocks)])


def max_step(x: np.ndarray) -> float:
    return float(np.max(np.abs(np.diff(x)))) if len(x) > 1 else 0.0


def timestamp(host: int) -> native.AudioTimeStamp:
    ts = native.AudioTimeStamp()
    ts.mHostTime = host
    ts.mFlags = native.HOST_TIME_VALID
    return ts


def test_ring_wraps_around_and_counts_overflow():
    L = native.lib()
    ring = native.Ring(L.a2a_ring_create(5, 2))
    try:
        assert ring.capacity == 8
        assert ring.write(np.arange(12, dtype=np.float32)) == 6
        assert ring.read(4).tolist() == list(range(8))
        assert ring.write(np.arange(100, 116, dtype=np.float32)) == 6
        assert ring.overflow_frames == 2
        all_ = ring.read(8)
        assert all_[:4].tolist() == [8, 9, 10, 11] and all_[4:].tolist() == list(range(100, 112))
        assert ring.total_written == 12 and ring.total_read == 12
    finally:
        L.a2a_ring_destroy(ring.h)


def test_concurrent_producer_consumer_preserves_sequence():
    L = native.lib()
    ring = native.Ring(L.a2a_ring_create(1_024, 1))
    total = 200_000

    def produce():
        nxt = 0
        while nxt < total:
            n = min(256, total - nxt, L.a2a_ring_writable(ring.h))
            if n:
                nxt += ring.write(np.arange(nxt, nxt + n, dtype=np.float32))

    t = threading.Thread(target=produce)
    t.start()
    expected, mismatches = 0, 0
    deadline = time.monotonic() + 30
    while expected < total and time.monotonic() < deadline:
        got = ring.read(300)
        mismatches += int(np.sum(got != np.arange(expected, expected + len(got), dtype=np.float32)))
        expected += len(got)
    t.join()
    assert expected == total and mismatches == 0 and ring.overflow_frames == 0
    L.a2a_ring_destroy(ring.h)


def test_captures_into_ring_and_records_timing():
    e = make_engine()
    e.process(np.full(256, 0.5, np.float32), 0, in_ts=timestamp(1_000))
    assert e.capture.readable == 256
    rec = e.pop_records()
    assert len(rec) == 1 and rec[0].kind == native.REC_CAPTURE and rec[0].frames == 256 and rec[0].host_ns > 0


def test_invalid_host_time_is_reported_not_fabricated():
    e = make_engine()
    e.process(np.zeros(64, np.float32), 0, in_ts=native.AudioTimeStamp())
    rec = e.pop_records()
    assert len(rec) == 2 and rec[0].host_ns == 0 and rec[1].kind == native.REC_TIMESTAMP_INVALID


def test_renders_captured_original_with_configured_delay():
    e = make_engine()
    e.set_render_output(True)
    e.set_delay_frames(128)
    produced, nxt = [], 1.0
    for _ in range(8):
        block = np.arange(nxt, nxt + 64, dtype=np.float32)
        nxt += 64
        produced.append(e.process(block, 64))
    produced = np.concatenate(produced)
    assert np.all(produced[:128] == 0)
    assert produced[128:136].tolist() == list(range(1, 9))
    assert produced[-1] == 512 - 128


def test_capture_only_mode_outputs_silence():
    e = make_engine()
    assert np.all(e.process(np.full(64, 0.7, np.float32), 64) == 0)


def test_gain_crossfade_is_smooth_and_lands_on_target():
    e = make_engine()
    e.set_render_output(True)
    assert e.translated.write(np.full(4_800, -1, np.float32)) == 4_800
    e.set_gains(0, 1, 1_440)
    series = np.concatenate([e.process(np.ones(480, np.float32), 480) for _ in range(5)])
    assert max_step(series) < 0.002
    assert abs(series[-1] + 1) < 1e-6
    assert e.gains == (0.0, 1.0)


def test_flush_discards_queued_translated_audio():
    e = make_engine()
    e.translated.write(np.full(2_000, 0.25, np.float32))
    e.flush_translated()
    e.process(None, 64)
    assert e.translated.readable == 0 and e.counter("translated_consumed") == 2_000


def test_starvation_is_counted_only_while_translated_bus_is_active():
    e = make_engine()
    e.set_render_output(True)
    e.process(None, 64)
    assert e.counter("translated_starved_frames") == 0
    e.set_gains(0, 1, 0)
    e.translated.write(np.full(32, 0.1, np.float32))
    e.process(None, 64)
    assert e.counter("translated_starved_frames") == 32


def test_oversized_blocks_are_rejected_with_silence():
    e = make_engine(max_block=128)
    out = e.process(np.ones(256, np.float32), 256)
    assert e.counter("rejected_blocks") == 1 and np.all(out == 0)


def test_rendered_monitor_captures_actual_gains_and_excludes_flushed_pcm():
    e = make_engine()
    e.set_render_output(True)
    source = np.full(64, 0.8, np.float32)
    translated = np.full(64, -0.4, np.float32)
    e.set_gains(0, 0.5, 0)
    e.process(source, 64)
    assert e.rendered.readable == 0  # opt-in only
    e.set_capture_rendered(True)
    e.translated.write(translated)
    out = e.process(source, 64)
    monitored = e.rendered.read(64)
    assert np.array_equal(monitored, out) and np.all(np.abs(monitored + 0.2) < 1e-6)
    e.translated.write(translated)
    e.flush_translated()
    e.set_gains(0, 0, 0)
    e.process(source, 64)
    assert np.all(e.rendered.read(64) == 0)


def test_mono_translated_bus_feeds_every_channel():
    e = make_engine(channels=2)
    e.set_render_output(True)
    e.set_gains(0, 1, 0)
    pcm = (np.arange(64) / 100).astype(np.float32)
    e.translated.write(pcm)
    out = render(e, 1)
    assert np.array_equal(out[0::2], pcm) and np.array_equal(out[1::2], pcm)


def test_flush_fades_out_instead_of_cutting():
    fade = 48
    e = make_engine(fade=fade)
    e.set_render_output(True)
    e.set_gains(0, 1, 0)
    e.translated.write(np.full(2_000, 0.5, np.float32))
    before = render(e, 2)
    assert before[-1] == 0.5
    e.flush_translated()
    after = render(e, 2)
    assert e.translated.readable == 0 and e.counter("translated_consumed") == 2_000
    assert after[0] > 0.45 and np.all(after[fade:] == 0)
    assert max_step(np.concatenate([before, after])) < 0.04


def test_flush_discards_only_audio_queued_before_the_call():
    e = make_engine(fade=32)
    e.set_render_output(True)
    e.set_gains(0, 1, 0)
    e.translated.write(np.full(1_000, 0.25, np.float32))
    e.flush_translated()
    e.translated.write(np.full(100, 0.75, np.float32))
    out = render(e, 4)
    assert e.counter("translated_consumed") == 1_100
    assert np.any(out == np.float32(0.75)) and not np.any(out == np.float32(0.25))


def test_dry_bus_ramps_to_zero_and_resumed_audio_fades_in():
    fade = 32
    e = make_engine(fade=fade)
    e.set_render_output(True)
    e.set_gains(0, 1, 0)
    pcm = np.full(100, 0.6, np.float32)
    e.translated.write(pcm)
    first = render(e, 3)
    assert first[0] < 0.05 and np.all(first[fade:100] == np.float32(0.6))
    assert first[100] > 0.55 and np.all(first[100 + fade:] == 0)
    e.translated.write(pcm)
    resumed = render(e, 1)
    assert resumed[0] < 0.05 and np.all(resumed[fade:64] == np.float32(0.6))
    assert max_step(np.concatenate([first, resumed])) < 0.06
    assert e.counter("translated_starved_frames") == 92


def test_edge_smoothing_survives_repeated_dry_spells_and_flushes():
    e = make_engine(fade=64)
    e.set_render_output(True)
    e.set_gains(0, 1, 0)
    out = []
    for i in range(50):
        e.translated.write(np.full(37 + i, 0.3, np.float32))
        if i % 3 == 0:
            e.flush_translated()
        out.append(render(e, 1 + i % 3))
    out = np.concatenate(out)
    assert np.all(np.isfinite(out)) and np.all(np.abs(out) <= 0.6) and max_step(out) < 0.05


def test_unrepresentable_buffer_sizes_are_rejected():
    L = native.lib()
    assert not L.a2a_ring_create(0xFFFFFFFF, 1)
    assert not L.a2a_record_ring_create(0xFFFFFFFF)
    with pytest.raises(MemoryError):
        native.Engine(channels=1, capture_ring_frames=64, max_delay_frames=0xFFFFFFFF, translated_ring_frames=64,
                      max_block_frames=32, record_capacity=64, rendered_ring_frames=0, fade_frames=0)


@pytest.mark.parametrize("partial_block", [False, True])
def test_short_resumption_keeps_the_existing_fade_tail(partial_block):
    e = native.Engine(channels=1, capture_ring_frames=256, max_delay_frames=0, translated_ring_frames=256,
                      max_block_frames=64, record_capacity=64, rendered_ring_frames=0, fade_frames=32)
    e.set_render_output(True)
    e.set_gains(0, 1, 0)
    signal = np.full(64, 0.6, np.float32)
    e.translated.write(signal)
    output = [e.process(None, 64), e.process(None, 4)]
    e.translated.write(signal[:1])
    output += [e.process(None, 16 if partial_block else 1), e.process(None, 64)]
    output = np.concatenate(output)
    assert max_step(output) < 0.04
    assert np.all(np.isfinite(output)) and np.all(output >= 0) and np.all(output <= 0.601)
    assert np.all(output[-16:] == 0)


def test_now_ns_matches_the_host_clock_base():
    import time as _t
    a = native.now_ns()
    b = _t.perf_counter_ns() if native.sys.platform == "win32" else _t.monotonic_ns()
    assert abs(a - b) < 50_000_000
