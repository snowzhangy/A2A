// Bounded FIFO of compressed video frames waiting for their delayed presentation time.
// Pure and clock-free so its admission and eviction rules are unit-tested (ADR-0012).

/// One compressed frame. `presentationNs` is mach host time in nanoseconds, the clock the
/// audio pipeline anchors capture to, so a frame and the speech captured with it share a slot.
public struct DelayedFrame<Payload> {
    public let presentationNs: UInt64
    public let bytes: Int
    /// A sync sample: decoding can start here without any earlier frame.
    public let isKeyframe: Bool
    public let payload: Payload

    public init(presentationNs: UInt64, bytes: Int, isKeyframe: Bool, payload: Payload) {
        self.presentationNs = presentationNs
        self.bytes = bytes
        self.isKeyframe = isKeyframe
        self.payload = payload
    }
}

public struct DelayedFrameQueue<Payload> {
    public let maxBytes: Int
    public private(set) var bytes = 0
    /// Frames evicted to stay within `maxBytes`, always as whole groups of pictures.
    public private(set) var droppedFrames = 0
    private var frames: [DelayedFrame<Payload>] = []
    private var head = 0

    public init(maxBytes: Int) {
        self.maxBytes = max(1, maxBytes)
    }

    public var count: Int { frames.count - head }
    public var isEmpty: Bool { count == 0 }
    public var oldestNs: UInt64? { isEmpty ? nil : frames[head].presentationNs }
    public var newestNs: UInt64? { isEmpty ? nil : frames.last?.presentationNs }

    /// Seconds of picture held, oldest to newest.
    public var spanSeconds: Double {
        guard let oldest = oldestNs, let newest = newestNs, newest > oldest else { return 0 }
        return Double(newest - oldest) / 1e9
    }

    /// Appends a frame in decode order. A queue that must start decoding needs a keyframe first,
    /// so leading delta frames are refused. Over budget, the oldest groups of pictures go first:
    /// cutting inside one would leave frames the decoder cannot rebuild.
    @discardableResult
    public mutating func push(_ frame: DelayedFrame<Payload>) -> Bool {
        if isEmpty && !frame.isKeyframe { return false }
        frames.append(frame)
        bytes += frame.bytes
        while bytes > maxBytes, count > 1 {
            dropOldestGroup()
        }
        compact()
        return true
    }

    /// The next frame if it is due by `throughNs` (presentation time minus the delay, plus any
    /// lookahead the renderer needs to decode it in time).
    public func peekDue(throughNs: UInt64) -> DelayedFrame<Payload>? {
        guard !isEmpty, frames[head].presentationNs <= throughNs else { return nil }
        return frames[head]
    }

    @discardableResult
    public mutating func popFirst() -> DelayedFrame<Payload>? {
        guard !isEmpty else { return nil }
        let frame = frames[head]
        head += 1
        bytes -= frame.bytes
        compact()
        return frame
    }

    public mutating func removeAll() {
        frames.removeAll()
        head = 0
        bytes = 0
    }

    private mutating func dropOldestGroup() {
        // Drop the head keyframe and every delta frame after it, up to the next keyframe.
        repeat {
            bytes -= frames[head].bytes
            head += 1
            droppedFrames += 1
        } while head < frames.count && !frames[head].isKeyframe
        if isEmpty { removeAll() }
    }

    private mutating func compact() {
        if head > 256 && head * 2 > frames.count {
            frames.removeFirst(head)
            head = 0
        }
    }
}
