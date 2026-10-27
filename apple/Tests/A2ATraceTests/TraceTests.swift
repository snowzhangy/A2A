import A2ACore
import A2ARTShim
@testable import A2ATrace
import Foundation
import Testing

@Suite struct TraceEncodingTests {
    @Test func headerOrderAndFieldOrderAreDeterministic() {
        let e = TraceEvent(tNs: 3_100_000_000, session: "a1f3", epoch: 1, gen: 1, kind: .asrPartial,
                           fields: [.speaker(SpeakerID("S1")), .span(SourceSpan(start: 96_000, end: 141_120)),
                                    .string("text", "我们下周开会"), .int("stable", 2)])
        #expect(e.jsonLine() == #"{"v":0,"t_ns":3100000000,"session":"a1f3","epoch":1,"gen":1,"ev":"asr.partial","spk":"S1","span":[96000,141120],"text":"我们下周开会","stable":2}"#)
    }

    @Test func stringsAreEscapedAndNonFiniteDoublesAreNull() throws {
        let e = TraceEvent(tNs: 1, session: "s", epoch: 1, gen: 1, kind: .error,
                           fields: [.string("message", "a\"b\\c\n\u{01}"), .double("x", .nan), .double("y", 2),
                                    .double("z", 0.25), .bool("ok", false)])
        let line = e.jsonLine()
        #expect(line.contains(#""message":"a\"b\\c\n\u0001""#))
        #expect(line.contains(#""x":null,"y":2.0,"z":0.25,"ok":false"#))
        // Must parse as JSON.
        let obj = try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any]
        #expect(obj?["message"] as? String == "a\"b\\c\n\u{01}")
    }

    @Test func writerProducesOneLinePerEvent() throws {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("trace-\(UUID()).jsonl")
        defer { try? FileManager.default.removeItem(at: url) }
        let writer = try TraceWriter(url: url, flushEvery: 3)
        for i in 0..<10 {
            writer.emit(TraceEvent(tNs: UInt64(i), session: "s", epoch: 1, gen: 1, kind: .queueDepth,
                                   fields: [.int("debt_ms", i)]))
        }
        writer.close()
        let lines = try String(contentsOf: url, encoding: .utf8).split(separator: "\n")
        #expect(lines.count == 10)
        #expect(writer.eventCount == 10)
    }
}

@Suite struct RTRecordDrainTests {
    @Test func convertsCaptureAndOutputRecordsAndReportsDrops() throws {
        let ring = try #require(a2a_record_ring_create(4))
        defer { a2a_record_ring_destroy(ring) }
        var cap = a2a_rt_record()
        cap.kind = UInt32(A2A_REC_CAPTURE.rawValue)
        cap.host_ns = 5_000
        cap.sample_index = 480
        cap.frames = 480
        cap.flags = 2
        var out = a2a_rt_record()
        out.kind = UInt32(A2A_REC_OUTPUT.rawValue)
        out.host_ns = 6_000
        out.sample_index = 0
        out.frames = 480
        out.aux64 = 960
        #expect(a2a_record_ring_push(ring, &cap))
        #expect(a2a_record_ring_push(ring, &out))
        #expect(a2a_record_ring_push(ring, &out))
        #expect(a2a_record_ring_push(ring, &out))
        #expect(!a2a_record_ring_push(ring, &out))   // full: counted as a drop

        let sink = MemoryTraceSink()
        let drain = RTRecordDrain(ring: ring)
        let n = drain.drain(context: RTTraceContext(session: "s", deviceRate: 24_000), into: sink)
        #expect(n == 4)
        let events = sink.all
        #expect(events.first?.kind == .bufCapture)
        // 24 kHz device frames map to canonical 48 kHz samples.
        #expect(events.first?.value("span") == .span(SourceSpan(start: 960, end: 1_920)))
        #expect(events.first?.tNs == 5_000)
        #expect(events[1].kind == .bufOutput)
        #expect(events[1].value("translated_consumed") == .int(960))
        #expect(events[1].value("rate") == .int(24_000))
        #expect(events.last?.kind == .traceDrop)
        #expect(events.last?.value("count") == .int(1))
        #expect(drain.captureBlocks == 1 && drain.outputBlocks == 3)
    }

    @Test func starvationCanBeCountedWithoutErrorEvents() throws {
        let ring = try #require(a2a_record_ring_create(4))
        defer { a2a_record_ring_destroy(ring) }
        var starved = a2a_rt_record()
        starved.kind = UInt32(A2A_REC_TRANSLATED_STARVED.rawValue)
        starved.frames = 512
        #expect(a2a_record_ring_push(ring, &starved))
        #expect(a2a_record_ring_push(ring, &starved))
        let sink = MemoryTraceSink()
        let drain = RTRecordDrain(ring: ring, reportStarvation: false)
        #expect(drain.drain(context: RTTraceContext(session: "s", deviceRate: 48_000), into: sink) == 2)
        #expect(sink.all.isEmpty)
        #expect(drain.starvedFrames == 1_024)
    }
}
