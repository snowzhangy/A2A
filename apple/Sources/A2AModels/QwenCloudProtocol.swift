import Foundation

/// Wire formats shared by the optional Qwen cloud driver and its deterministic tests.
/// The caller owns all audio and must not put these payloads in diagnostics.
public enum QwenCloudProtocol {
    public static func sessionUpdate(target: String) -> [String: Any] {
        ["type": "session.update", "session": [
            "output_modalities": ["text", "audio"], "voice": "default",
            "translation": ["language": target], "enable_voice_clone": true,
            "voice_clone_options": ["frequency": "always"]
        ]]
    }

    public static func encodePCM16(_ samples: [Float]) -> Data {
        var pcm = Data(capacity: samples.count * 2)
        for sample in samples {
            let safe = sample.isFinite ? min(1, max(-1, sample)) : 0
            let bits = UInt16(bitPattern: Int16((safe * 32767).rounded()))
            pcm.append(UInt8(truncatingIfNeeded: bits))
            pcm.append(UInt8(truncatingIfNeeded: bits >> 8))
        }
        return pcm
    }

    public static func decodePCM16(_ pcm: Data) -> [Float]? {
        guard pcm.count.isMultiple(of: 2), pcm.count <= 1_048_576 else { return nil }
        let bytes = [UInt8](pcm)
        var samples: [Float] = []
        samples.reserveCapacity(bytes.count / 2)
        for i in stride(from: 0, to: bytes.count, by: 2) {
            let bits = UInt16(bytes[i]) | (UInt16(bytes[i + 1]) << 8)
            samples.append(Float(Int16(bitPattern: bits)) / 32768)
        }
        return samples
    }
}
