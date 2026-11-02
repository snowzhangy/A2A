// The delayed picture's frame ring (ADR-0012): frames leave in order once due, decoding always
// starts on a keyframe, and the memory budget evicts whole groups of pictures.

import A2AVideo
import Testing

private func frame(_ seconds: Double, key: Bool = false, bytes: Int = 100) -> DelayedFrame<Int> {
    DelayedFrame(presentationNs: UInt64(seconds * 1e9), bytes: bytes, isKeyframe: key, payload: Int(seconds * 1000))
}

@Suite struct DelayedFrameQueueTests {
    @Test func framesLeaveOnlyOnceTheirDelayedTimeHasCome() {
        var q = DelayedFrameQueue<Int>(maxBytes: 10_000)
        q.push(frame(1.0, key: true))
        q.push(frame(1.5))
        q.push(frame(2.0))
        // Shown 8 s late: at host 9.4 s the picture is at 1.4 s.
        #expect(q.peekDue(throughNs: 1_400_000_000)?.payload == 1000)
        q.popFirst()
        #expect(q.peekDue(throughNs: 1_400_000_000) == nil)
        #expect(q.peekDue(throughNs: 1_500_000_000)?.payload == 1500)
        #expect(q.count == 2)
    }

    @Test func decodingStartsOnAKeyframe() {
        var q = DelayedFrameQueue<Int>(maxBytes: 10_000)
        let leadingDelta = q.push(frame(0.5))
        #expect(!leadingDelta)
        #expect(q.isEmpty)
        let keyframe = q.push(frame(1.0, key: true))
        let delta = q.push(frame(1.1))
        #expect(keyframe && delta)
        #expect(q.count == 2)
    }

    @Test func theBudgetEvictsWholeGroupsOfPictures() {
        var q = DelayedFrameQueue<Int>(maxBytes: 500)
        q.push(frame(0.0, key: true))
        q.push(frame(0.1))
        q.push(frame(0.2))
        q.push(frame(1.0, key: true))
        q.push(frame(1.1))
        q.push(frame(1.2))   // 600 bytes: the first group of three must go, not just one frame
        #expect(q.droppedFrames == 3)
        #expect(q.bytes == 300)
        #expect(q.peekDue(throughNs: .max)?.isKeyframe == true)
        #expect(q.oldestNs == 1_000_000_000)
        #expect(abs(q.spanSeconds - 0.2) < 1e-9)
    }

    @Test func aSingleOversizedGroupIsKeptRatherThanEmptied() {
        var q = DelayedFrameQueue<Int>(maxBytes: 150)
        q.push(frame(0.0, key: true, bytes: 200))
        #expect(q.count == 1)
    }
}
