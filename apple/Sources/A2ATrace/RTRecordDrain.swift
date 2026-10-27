// Drains A2ARTShim timing records on a non-RT thread and converts them to trace events.
// Capture/output timing pairs frame indices with valid host times; invalid timestamps
// are reported, never replaced with fabricated times (section 4.1).

import A2ACore
import A2ARTShim

public struct RTTraceContext: Sendable {
    public var session: String
    public var epoch: SessionEpoch
    public var generation: PlayoutGeneration
    /// Device frame rate of the engine, used to express capture indices in canonical samples.
    public var deviceRate: Double

    public init(session: String, epoch: SessionEpoch = 1, generation: PlayoutGeneration = 1, deviceRate: Double) {
        self.session = session
        self.epoch = epoch
        self.generation = generation
        self.deviceRate = deviceRate
    }
}

public final class RTRecordDrain: @unchecked Sendable {
    private let ring: OpaquePointer
    private var buffer: [a2a_rt_record]
    private var reportedDrops: UInt64 = 0
    private let reportStarvation: Bool

    /// Totals observed so far (for reports and tests).
    public private(set) var captureBlocks = 0
    public private(set) var outputBlocks = 0
    public private(set) var invalidTimestamps = 0
    public private(set) var overflowFrames: Int64 = 0
    public private(set) var starvedFrames: Int64 = 0

    /// `reportStarvation: false` only counts translated-bus starvation. The engine cannot tell
    /// an idle gap between units from a unit that ran dry, so a driver that tracks units
    /// (and reports per-unit underruns itself) turns the per-callback errors off.
    public init(ring: OpaquePointer, batch: Int = 512, reportStarvation: Bool = true) {
        self.ring = ring
        self.buffer = Array(repeating: a2a_rt_record(), count: batch)
        self.reportStarvation = reportStarvation
    }

    /// Converts all pending records. Call from one non-RT thread only.
    @discardableResult
    public func drain(context: RTTraceContext, into sink: TraceSink) -> Int {
        var total = 0
        while true {
            let n = buffer.withUnsafeMutableBufferPointer { ptr in
                Int(a2a_record_ring_pop(ring, ptr.baseAddress!, UInt32(ptr.count)))
            }
            if n == 0 { break }
            for i in 0..<n { convert(buffer[i], context: context, sink: sink) }
            total += n
        }
        let dropped = a2a_record_ring_dropped(ring)
        if dropped > reportedDrops {
            sink.emit(event(context, .traceDrop, tNs: HostClock.nowNs(),
                            [.int64("count", Int64(dropped - reportedDrops)), .string("ring", "rt_timing")]))
            reportedDrops = dropped
        }
        return total
    }

    private func convert(_ r: a2a_rt_record, context: RTTraceContext, sink: TraceSink) {
        let t = r.host_ns != 0 ? r.host_ns : HostClock.nowNs()
        let scale = Double(canonicalSampleRate) / context.deviceRate
        switch Int32(r.kind) {
        case Int32(A2A_REC_CAPTURE.rawValue):
            captureBlocks += 1
            let start = Int64((Double(r.sample_index) * scale).rounded())
            let end = Int64((Double(r.sample_index + Int64(r.frames)) * scale).rounded())
            sink.emit(event(context, .bufCapture, tNs: t, [
                .span(SourceSpan(start: start, end: end)), .int("frames", Int(r.frames)),
                .int64("device_index", r.sample_index), .int("ts_flags", Int(r.flags)),
                .bool("host_time_valid", r.host_ns != 0),
            ]))
        case Int32(A2A_REC_OUTPUT.rawValue):
            outputBlocks += 1
            sink.emit(event(context, .bufOutput, tNs: t, [
                .int64("out_sample", r.sample_index), .int("frames", Int(r.frames)),
                .int64("translated_consumed", r.aux64), .int("ts_flags", Int(r.flags)),
                .int("rate", Int(context.deviceRate)),
                .bool("host_time_valid", r.host_ns != 0),
            ]))
        case Int32(A2A_REC_CAPTURE_OVERFLOW.rawValue):
            overflowFrames += Int64(r.frames)
            sink.emit(event(context, .error, tNs: t, [
                .string("where", "capture_ring"), .string("message", "overflow"),
                .int("frames", Int(r.frames)), .int64("device_index", r.sample_index),
            ]))
        case Int32(A2A_REC_TRANSLATED_STARVED.rawValue):
            starvedFrames += Int64(r.frames)
            guard reportStarvation else { return }
            sink.emit(event(context, .error, tNs: t, [
                .string("where", "translated_bus"), .string("message", "starved"),
                .int("frames", Int(r.frames)), .int64("out_sample", r.sample_index),
            ]))
        case Int32(A2A_REC_TIMESTAMP_INVALID.rawValue):
            invalidTimestamps += 1
            sink.emit(event(context, .error, tNs: t, [
                .string("where", r.aux == A2A_REC_CAPTURE.rawValue ? "capture_timestamp" : "output_timestamp"),
                .string("message", "host time invalid"), .int("ts_flags", Int(r.flags)),
            ]))
        default:
            sink.emit(event(context, .error, tNs: t, [.string("where", "rt_record"), .int("kind", Int(r.kind))]))
        }
    }

    private func event(_ c: RTTraceContext, _ kind: TraceKind, tNs: UInt64, _ fields: [TraceEvent.Field]) -> TraceEvent {
        TraceEvent(tNs: tNs, session: c.session, epoch: c.epoch, gen: c.generation, kind: kind, fields: fields)
    }
}
