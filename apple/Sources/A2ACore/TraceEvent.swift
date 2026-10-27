// Trace format v0 (section 4.5). Events are pure values; A2ATrace writes them.
// Serialization is deterministic (fixed header order, fields in insertion order)
// so golden traces compare byte-for-byte.

public enum TraceKind: String, CaseIterable, Sendable {
    case sessionStart = "session.start"
    case bufCapture = "buf.capture"
    case bufOutput = "buf.output"
    case stateChange = "state.change"
    case vadStart = "vad.start"
    case vadEnd = "vad.end"
    case spkAssign = "spk.assign"
    case spkChange = "spk.change"
    case profileReady = "profile.ready"
    case asrPartial = "asr.partial"
    case asrFinal = "asr.final"
    case commitSource = "commit.source"
    case mtRequest = "mt.request"
    case mtResult = "mt.result"
    case commitTarget = "commit.target"
    case ttsRequest = "tts.request"
    case ttsFirstChunk = "tts.first_chunk"
    case ttsChunk = "tts.chunk"
    case playScheduled = "play.scheduled"
    case playStart = "play.start"
    case playEnd = "play.end"
    case unitCancel = "unit.cancel"
    case sourceDisposition = "source.disposition"
    case queueDepth = "queue.depth"
    case clockDrift = "clock.drift"
    case resourceSample = "resource.sample"
    case fallbackEnter = "fallback.enter"
    case fallbackExit = "fallback.exit"
    case traceDrop = "trace.drop"
    case error = "error"
    // Extensions beyond the required set; the schema allows them explicitly.
    case commitHold = "commit.hold"
    case commitRevision = "commit.revision"
    case pcmReject = "pcm.reject"
    case gainRamp = "gain.ramp"
    case finalizeRequest = "asr.finalize_request"
    case catchUp = "catch_up"
    case ttsDone = "tts.done"
}

public enum TraceValue: Hashable, Sendable {
    case int(Int64)
    case double(Double)
    case string(String)
    case bool(Bool)
    case span(SourceSpan)
    case null
}

public struct TraceEvent: Hashable, Sendable {
    public static let schemaVersion = 0

    public let tNs: UInt64
    public let session: String
    public let epoch: SessionEpoch
    public let gen: PlayoutGeneration
    public let kind: TraceKind
    public let fields: [Field]

    public struct Field: Hashable, Sendable {
        public let key: String
        public let value: TraceValue
        public init(_ key: String, _ value: TraceValue) {
            self.key = key
            self.value = value
        }
    }

    public init(tNs: UInt64, session: String, epoch: SessionEpoch, gen: PlayoutGeneration,
                kind: TraceKind, fields: [Field] = []) {
        self.tNs = tNs
        self.session = session
        self.epoch = epoch
        self.gen = gen
        self.kind = kind
        self.fields = fields
    }

    public func value(_ key: String) -> TraceValue? {
        fields.first { $0.key == key }?.value
    }

    public func jsonLine() -> String {
        var out = "{\"v\":\(Self.schemaVersion),\"t_ns\":\(tNs),\"session\":"
        JSON.appendString(session, to: &out)
        out += ",\"epoch\":\(epoch),\"gen\":\(gen),\"ev\":"
        JSON.appendString(kind.rawValue, to: &out)
        for field in fields {
            out += ","
            JSON.appendString(field.key, to: &out)
            out += ":"
            JSON.appendValue(field.value, to: &out)
        }
        out += "}"
        return out
    }
}

/// Builder sugar so call sites stay readable.
public extension TraceEvent.Field {
    static func int(_ key: String, _ v: Int) -> Self { .init(key, .int(Int64(v))) }
    static func int64(_ key: String, _ v: Int64) -> Self { .init(key, .int(v)) }
    static func double(_ key: String, _ v: Double) -> Self { .init(key, .double(v)) }
    static func string(_ key: String, _ v: String) -> Self { .init(key, .string(v)) }
    static func bool(_ key: String, _ v: Bool) -> Self { .init(key, .bool(v)) }
    static func span(_ v: SourceSpan) -> Self { .init("span", .span(v)) }
    static func unit(_ v: UnitID) -> Self { .init("unit", .string(v.description)) }
    static func speaker(_ v: SpeakerID) -> Self { .init("spk", .string(v.rawValue)) }
}

enum JSON {
    static func appendValue(_ value: TraceValue, to out: inout String) {
        switch value {
        case .int(let v): out += String(v)
        case .double(let v):
            if v.isFinite {
                // Shortest round-trip representation; integral values keep a decimal point.
                out += v == v.rounded() && abs(v) < 1e15 ? "\(Int64(v)).0" : "\(v)"
            } else {
                out += "null"
            }
        case .string(let s): appendString(s, to: &out)
        case .bool(let b): out += b ? "true" : "false"
        case .span(let s): out += "[\(s.start),\(s.end)]"
        case .null: out += "null"
        }
    }

    static func appendString(_ s: String, to out: inout String) {
        out += "\""
        for scalar in s.unicodeScalars {
            switch scalar {
            case "\"": out += "\\\""
            case "\\": out += "\\\\"
            case "\n": out += "\\n"
            case "\r": out += "\\r"
            case "\t": out += "\\t"
            default:
                if scalar.value < 0x20 {
                    let hex = Swift.String(scalar.value, radix: 16)
                    out += "\\u" + Swift.String(repeating: "0", count: 4 - hex.count) + hex
                } else {
                    out.unicodeScalars.append(scalar)
                }
            }
        }
        out += "\""
    }
}
