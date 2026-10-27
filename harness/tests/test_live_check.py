"""Live-check helpers: clip alignment in both directions and keyword matching."""

import numpy as np

from a2a_harness.live_check import align, matches
from a2a_harness.metrics import source_map


def _clip(rate=8_000, seconds=6.0, seed=3):
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(rate * seconds)).astype(np.float32) * 0.1
    x[: rate // 2] = 0   # leading silence like a real clip
    return x


def test_align_finds_capture_that_starts_inside_the_clip():
    rate = 8_000
    clip = _clip(rate)
    source = clip[1_234:]
    offset, r = align(source, clip, rate, window_s=3)
    assert round(offset * rate) == 1_234
    assert r > 0.999


def test_align_finds_capture_that_starts_before_playback():
    rate = 8_000
    clip = _clip(rate)
    source = np.concatenate([np.zeros(700, dtype=np.float32), clip])
    offset, r = align(source, clip, rate, window_s=3)
    assert round(offset * rate) == -700
    assert r > 0.999


def test_keyword_groups_need_one_alternative_each():
    groups = [["meeting", "meet"], ["next week"]]
    assert matches("We will meet next week.", groups) == []
    assert matches("We have a meeting.", groups) == [["next week"]]


def test_source_map_prefers_device_capture_records():
    events = [
        {"ev": "buf.capture", "span": [0, 960], "t_ns": 9_000},                       # pipeline delivery
        {"ev": "buf.capture", "span": [0, 512], "t_ns": 1_000, "device_index": 0, "host_time_valid": True},
        {"ev": "buf.capture", "span": [512, 1024], "t_ns": 2_000, "device_index": 512, "host_time_valid": True},
    ]
    m = source_map(events)
    assert m.samples == [512, 1024] and m.times == [1_000, 2_000]
