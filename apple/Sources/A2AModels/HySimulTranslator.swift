import A2ACore
import A2AMedia
import Foundation

/// File-based speech translation on a user-selected LAN server. Audio remains in memory.
public struct HySimulTranslator: Sendable {
    private let endpoint: URL
    private let direction: String

    public init(server: URL, direction: String) throws {
        guard ["http", "https"].contains(server.scheme?.lowercased() ?? ""), server.host != nil,
              ["zh-en", "en-zh"].contains(direction) else {
            throw AdapterError.unsupported("invalid Hy-SimulMT server URL or direction")
        }
        self.endpoint = server.appending(path: "v1/audio/translations")
        self.direction = direction
    }

    public func translate(unit: UnitID, samples: [Float]) async throws -> TranslationCandidate {
        guard !samples.isEmpty, samples.count <= 48_000 * 30 else {
            throw AdapterError.unsupported("Hy-SimulMT segment is empty or exceeds 30 seconds")
        }
        let boundary = "A2A-\(UUID().uuidString)"
        var body = Data()
        func append(_ text: String) { body.append(contentsOf: text.utf8) }
        append("--\(boundary)\r\nContent-Disposition: form-data; name=\"direction\"\r\n\r\n\(direction)\r\n")
        append("--\(boundary)\r\nContent-Disposition: form-data; name=\"file\"; filename=\"segment.wav\"\r\nContent-Type: audio/wav\r\n\r\n")
        body.append(WAV.encodeFloat32(WAVAudio(sampleRate: 48_000, channels: 1, samples: samples)))
        append("\r\n--\(boundary)--\r\n")
        var components = URLComponents(url: endpoint, resolvingAgainstBaseURL: false)!
        components.queryItems = [URLQueryItem(name: "stream", value: "true")]
        let timeout = Self.requestTimeout(samples: samples.count)
        var request = URLRequest(url: components.url!)
        request.httpMethod = "POST"
        request.timeoutInterval = timeout
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let configuration = URLSessionConfiguration.ephemeral
        configuration.timeoutIntervalForRequest = timeout
        configuration.timeoutIntervalForResource = timeout
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let (data, response) = try await session.upload(for: request, from: body)
        let http = response as? HTTPURLResponse
        // The status belongs in the message: a server answering 404 here is a wrong address or
        // a different service, which is not the same problem as a malformed translation stream.
        guard (200..<300).contains(http?.statusCode ?? -1) else {
            throw AdapterError.unsupported("Hy-SimulMT server rejected the request with HTTP \(http?.statusCode ?? -1)")
        }
        guard data.count <= 1_048_576, let stream = String(data: data, encoding: .utf8) else {
            throw AdapterError.unsupported("Hy-SimulMT server returned an invalid response")
        }
        return try Self.parseEvents(stream, unit: unit)
    }

    /// A 3 s span translates in about 1 s, so a request still open after twice the span's
    /// length is a dead or hung server. Failing it reports `mt_error` and plays the original;
    /// waiting longer left the serial queue stuck and every later span skipped as catch-up with
    /// no error (1 November, 10:38 session).
    static func requestTimeout(samples: Int) -> TimeInterval {
        max(4, 2 * Double(samples) / 48_000)
    }

    static func parseEvents(_ stream: String, unit: UnitID) throws -> TranslationCandidate {
        var parts: [String] = []
        var finalText: String?
        var completed = false
        for line in stream.components(separatedBy: .newlines) where line.hasPrefix("data:") {
            let json = Data(line.dropFirst(5).trimmingCharacters(in: .whitespaces).utf8)
            guard let event = try? JSONSerialization.jsonObject(with: json) as? [String: Any] else {
                throw AdapterError.unsupported("Hy-SimulMT returned malformed events")
            }
            if event["error"] != nil || ((event["code"] as? Int).map { $0 != 0 } ?? false) {
                throw AdapterError.unsupported("Hy-SimulMT server failed to translate audio")
            }
            if let delta = event["delta"] as? String, !delta.hasPrefix("run_turn_stream:") { parts.append(delta) }
            if event["done"] as? Bool == true {
                completed = true
                if let text = event["text"] as? String, !text.hasPrefix("run_turn_stream:") { finalText = text }
            }
        }
        var text = parts.joined(separator: " ").trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty, let finalText {
            // A server that reports only the finished line still translates.
            text = finalText.components(separatedBy: "run_turn_stream:").first?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        }
        guard completed else { throw AdapterError.unsupported("Hy-SimulMT returned no completed translation") }
        // A completed but empty answer means the audio held no speech (music, laughter, a
        // pause). Hand it to the reducer as an empty candidate: it reports silence, which is
        // not a translation failure and must not fall back to the original bus.
        return TranslationCandidate(unitID: unit, text: text)
    }

    /// Model state a server reports in `/health`.
    public enum Residency: Equatable, Sendable { case ready, loading, failed }

    /// A server that loads its model in the background answers its root before the model is
    /// resident, and a span sent then waits and expires. `/health` with a `resident` field says
    /// when the model is in memory; a server without that field (or without `/health`) is ready
    /// once its root answers.
    public static func residency(health body: Data?) -> Residency {
        guard let body, let object = try? JSONSerialization.jsonObject(with: body) as? [String: Any],
              let resident = object["resident"] else { return .ready }
        switch resident as? String {
        case "ready": return .ready
        case "failed": return .failed
        default: return .loading
        }
    }
}
