// Minimal RIFF/WAVE reader and writer: PCM 16/24/32-bit integer and 32-bit float.
// Used by CLIs and tests; never on a real-time thread.

import Foundation

public struct WAVAudio: Sendable {
    public let sampleRate: Int
    public let channels: Int
    /// Interleaved samples in [-1, 1].
    public var samples: [Float]

    public init(sampleRate: Int, channels: Int, samples: [Float]) {
        self.sampleRate = sampleRate
        self.channels = channels
        self.samples = samples
    }

    public var frameCount: Int { channels == 0 ? 0 : samples.count / channels }
    public var durationSeconds: Double { Double(frameCount) / Double(sampleRate) }

    /// Average of all channels.
    public func mono() -> [Float] {
        guard channels > 1 else { return samples }
        var out = [Float](repeating: 0, count: frameCount)
        let scale = 1 / Float(channels)
        for f in 0..<frameCount {
            var acc: Float = 0
            for c in 0..<channels { acc += samples[f * channels + c] }
            out[f] = acc * scale
        }
        return out
    }
}

public enum WAVError: Error, CustomStringConvertible {
    case notRIFF
    case missingChunk(String)
    case malformed(String)
    case unsupportedFormat(tag: UInt16, bits: UInt16)

    public var description: String {
        switch self {
        case .notRIFF: "not a RIFF/WAVE file"
        case .missingChunk(let c): "missing \(c) chunk"
        case .malformed(let reason): "malformed WAV: \(reason)"
        case .unsupportedFormat(let tag, let bits): "unsupported WAV format tag \(tag) with \(bits) bits"
        }
    }
}

public enum WAV {
    public static func read(_ url: URL) throws -> WAVAudio {
        try decode(Data(contentsOf: url))
    }

    public static func decode(_ data: Data) throws -> WAVAudio {
        // Decode in place: copying a long fixture into [UInt8] would double peak memory.
        try data.withUnsafeBytes { raw in try decode(raw.bindMemory(to: UInt8.self)) }
    }

    static func decode(_ bytes: UnsafeBufferPointer<UInt8>) throws -> WAVAudio {
        guard bytes.count >= 12, String(decoding: bytes[0..<4], as: UTF8.self) == "RIFF",
              String(decoding: bytes[8..<12], as: UTF8.self) == "WAVE" else { throw WAVError.notRIFF }
        var offset = 12
        var tag: UInt16 = 0, channels = 0, rate = 0, bits: UInt16 = 0
        var payload: Range<Int>?
        while offset + 8 <= bytes.count {
            let id = String(decoding: bytes[offset..<offset + 4], as: UTF8.self)
            let size = Int(u32(bytes, offset + 4))
            let body = offset + 8
            guard size <= bytes.count - body else { throw WAVError.malformed("truncated \(id) chunk") }
            let end = body + size
            switch id {
            case "fmt ":
                guard size >= 16 else { throw WAVError.malformed("fmt chunk is shorter than 16 bytes") }
                tag = u16(bytes, body)
                channels = Int(u16(bytes, body + 2))
                rate = Int(u32(bytes, body + 4))
                bits = u16(bytes, body + 14)
                if tag == 0xFFFE {
                    guard size >= 40 else { throw WAVError.malformed("truncated extensible fmt chunk") }
                    tag = u16(bytes, body + 24)   // WAVE_FORMAT_EXTENSIBLE
                }
            case "data":
                payload = body..<end
            default:
                break
            }
            offset = body + size + (size & 1)
        }
        guard channels > 0, rate > 0 else { throw WAVError.missingChunk("fmt") }
        guard let payload else { throw WAVError.missingChunk("data") }
        if (tag == 1 && [16, 24, 32].contains(bits)) || (tag == 3 && bits == 32) {
            guard payload.count % (channels * Int(bits / 8)) == 0 else {
                throw WAVError.malformed("data chunk contains an incomplete audio frame")
            }
        }
        let samples: [Float]
        switch (tag, bits) {
        case (1, 16):
            samples = stride(from: payload.lowerBound, to: payload.upperBound - 1, by: 2).map {
                Float(Int16(bitPattern: u16(bytes, $0))) / 32_768
            }
        case (1, 24):
            samples = stride(from: payload.lowerBound, to: payload.upperBound - 2, by: 3).map {
                let v = Int32(bytes[$0]) | Int32(bytes[$0 + 1]) << 8 | Int32(Int8(bitPattern: bytes[$0 + 2])) << 16
                return Float(v) / 8_388_608
            }
        case (1, 32):
            samples = stride(from: payload.lowerBound, to: payload.upperBound - 3, by: 4).map {
                Float(Int32(bitPattern: u32(bytes, $0))) / 2_147_483_648
            }
        case (3, 32):
            samples = stride(from: payload.lowerBound, to: payload.upperBound - 3, by: 4).map {
                Float(bitPattern: u32(bytes, $0))
            }
        default:
            throw WAVError.unsupportedFormat(tag: tag, bits: bits)
        }
        return WAVAudio(sampleRate: rate, channels: channels, samples: samples)
    }

    public static func encodeFloat32(_ audio: WAVAudio) -> Data {
        var d = Data()
        let dataBytes = audio.samples.count * 4
        d.append(contentsOf: Array("RIFF".utf8)); append32(&d, UInt32(36 + dataBytes))
        d.append(contentsOf: Array("WAVE".utf8))
        d.append(contentsOf: Array("fmt ".utf8)); append32(&d, 16)
        append16(&d, 3); append16(&d, UInt16(audio.channels))
        append32(&d, UInt32(audio.sampleRate)); append32(&d, UInt32(audio.sampleRate * audio.channels * 4))
        append16(&d, UInt16(audio.channels * 4)); append16(&d, 32)
        d.append(contentsOf: Array("data".utf8)); append32(&d, UInt32(dataBytes))
        audio.samples.withUnsafeBufferPointer { d.append(Data(buffer: $0)) }
        return d
    }

    public static func write(_ audio: WAVAudio, to url: URL) throws {
        try encodeFloat32(audio).write(to: url, options: .atomic)
    }

    private static func u16(_ b: UnsafeBufferPointer<UInt8>, _ o: Int) -> UInt16 { UInt16(b[o]) | UInt16(b[o + 1]) << 8 }
    private static func u32(_ b: UnsafeBufferPointer<UInt8>, _ o: Int) -> UInt32 {
        UInt32(b[o]) | UInt32(b[o + 1]) << 8 | UInt32(b[o + 2]) << 16 | UInt32(b[o + 3]) << 24
    }
    static func append16(_ d: inout Data, _ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
    static func append32(_ d: inout Data, _ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { d.append(contentsOf: $0) } }
}

/// Streams float32 frames to a WAV file and patches the header on close, so a long
/// capture never holds the whole recording in memory.
public final class WAVStreamWriter {
    private let handle: FileHandle
    public let sampleRate: Int
    public let channels: Int
    public private(set) var framesWritten = 0

    public init(url: URL, sampleRate: Int, channels: Int) throws {
        FileManager.default.createFile(atPath: url.path, contents: nil)
        handle = try FileHandle(forWritingTo: url)
        self.sampleRate = sampleRate
        self.channels = channels
        handle.write(WAV.encodeFloat32(WAVAudio(sampleRate: sampleRate, channels: channels, samples: [])))
    }

    public func append(_ interleaved: UnsafeBufferPointer<Float>) {
        guard !interleaved.isEmpty else { return }
        handle.write(Data(buffer: interleaved))
        framesWritten += interleaved.count / channels
    }

    public func append(_ interleaved: [Float]) {
        interleaved.withUnsafeBufferPointer { append($0) }
    }

    public func close() throws {
        let dataBytes = UInt32(framesWritten * channels * 4)
        var riff = Data(); WAV.append32(&riff, 36 + dataBytes)
        var size = Data(); WAV.append32(&size, dataBytes)
        try handle.seek(toOffset: 4); handle.write(riff)
        try handle.seek(toOffset: 40); handle.write(size)
        try handle.close()
    }
}
