// Non-RT trace writer: serializes TraceEvent values to JSON Lines.
// RT callbacks never call this; they push fixed-size records into A2ARTShim rings,
// and RTRecordDrain converts those on an ordinary thread.

import A2ACore
import A2ARTShim
import Foundation
import Synchronization

/// Host monotonic time (mach_absolute_time in ns), the trace `t_ns` base.
public enum HostClock {
    public static func nowNs() -> UInt64 {
        a2a_time_init()
        return a2a_now_ns()
    }
}

public protocol TraceSink: Sendable {
    func emit(_ event: TraceEvent)
}

public final class TraceWriter: TraceSink, @unchecked Sendable {
    private let handle: FileHandle
    private let lock = Mutex<[String]>([])
    private let flushEvery: Int
    private let state = Mutex<(events: Int, closed: Bool)>((0, false))

    public init(url: URL, flushEvery: Int = 64) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        handle = try FileHandle(forWritingTo: url)
        self.flushEvery = flushEvery
    }

    /// Writes to an already open handle (stdout/stderr or a pipe).
    public init(handle: FileHandle, flushEvery: Int = 1) {
        self.handle = handle
        self.flushEvery = flushEvery
    }

    public func emit(_ event: TraceEvent) {
        let line = event.jsonLine()
        let pending: [String]? = lock.withLock { buffer in
            buffer.append(line)
            guard buffer.count >= flushEvery else { return nil }
            defer { buffer.removeAll(keepingCapacity: true) }
            return buffer
        }
        state.withLock { $0.events += 1 }
        if let pending { write(pending) }
    }

    public var eventCount: Int { state.withLock { $0.events } }

    public func flush() {
        let pending = lock.withLock { buffer -> [String] in
            defer { buffer.removeAll(keepingCapacity: true) }
            return buffer
        }
        write(pending)
    }

    public func close() {
        flush()
        let shouldClose = state.withLock { s -> Bool in
            defer { s.closed = true }
            return !s.closed
        }
        if shouldClose { try? handle.synchronize() }
    }

    private func write(_ lines: [String]) {
        guard !lines.isEmpty else { return }
        var data = Data()
        for line in lines {
            data.append(contentsOf: line.utf8)
            data.append(0x0A)
        }
        handle.write(data)
    }
}

/// Collects events in memory (tests, golden traces).
public final class MemoryTraceSink: TraceSink, @unchecked Sendable {
    private let events = Mutex<[TraceEvent]>([])
    public init() {}
    public func emit(_ event: TraceEvent) { events.withLock { $0.append(event) } }
    public var all: [TraceEvent] { events.withLock { $0 } }
}
