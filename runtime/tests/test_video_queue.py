from a2a_runtime.video.queue import DelayedFrame, DelayedFrameQueue


def frame(seconds, key=False, size=100):
    return DelayedFrame(int(seconds * 1e9), size, key, int(seconds * 1000))


def test_frames_leave_only_once_delayed_time_has_come():
    queue = DelayedFrameQueue(10_000)
    queue.push(frame(1, True))
    queue.push(frame(1.5))
    queue.push(frame(2))
    assert queue.peek_due(1_400_000_000).payload == 1000
    queue.pop_first()
    assert queue.peek_due(1_400_000_000) is None
    assert queue.peek_due(1_500_000_000).payload == 1500 and queue.count == 2


def test_decoding_starts_on_keyframe():
    queue = DelayedFrameQueue(10_000)
    assert not queue.push(frame(0.5)) and queue.is_empty
    assert queue.push(frame(1, True)) and queue.push(frame(1.1)) and queue.count == 2


def test_budget_evicts_whole_groups():
    queue = DelayedFrameQueue(500)
    for at, key in [(0, True), (0.1, False), (0.2, False), (1, True), (1.1, False), (1.2, False)]:
        queue.push(frame(at, key))
    assert queue.dropped_frames == 3 and queue.bytes == 300
    assert queue.peek_due(2**63).is_keyframe and queue.oldest_ns == 1_000_000_000
    assert abs(queue.span_seconds - 0.2) < 1e-9


def test_single_oversized_group_is_kept():
    queue = DelayedFrameQueue(150)
    queue.push(frame(0, True, 200))
    assert queue.count == 1
