// Optional cloud speech-to-speech path. All network and conversion work runs off
// the Core Audio callback. Audio and captions are never written to diagnostics.
import A2AAudio
import A2ACore
import A2AMedia
import A2AModels
import A2ARTShim
import A2ATrace
import Darwin
import Foundation

func readCloudKey() -> String {
    var bytes: [UInt8] = []
    var byte: UInt8 = 0
    while bytes.count <= 4096 {
        let n = read(STDIN_FILENO, &byte, 1)
        if n < 0 && errno == EINTR { continue }
        guard n == 1 else { fail("Qwen Cloud API key was not provided") }
        if byte == 10 { break }
        bytes.append(byte)
    }
    guard bytes.count <= 4096, let key = String(bytes: bytes, encoding: .utf8), !key.isEmpty else {
        fail("Invalid Qwen Cloud API key")
    }
    return key
}

@MainActor final class QwenCloudStream {
    private let socket: URLSessionWebSocketTask
    private let session: URLSession
    private var receiver: Task<Void, Never>?
    private var failure: String?
    private var textByItem: [String: String] = [:]
    private var outputResampler: StreamingResampler?
    private var pipeline: SessionPipeline?

    private init(session: URLSession, socket: URLSessionWebSocketTask) {
        self.session = session
        self.socket = socket
    }

    static func connect(key: String, region: String, direction: TranslationDirection) async throws -> QwenCloudStream {
        let url = URL(string: "wss://\(region).maas.qwencloudapi.com/api-ws/v1/realtime?model=qwen3.8-livetranslate-flash-realtime")!
        var request = URLRequest(url: url, timeoutInterval: 20)
        request.setValue("Bearer \(key)", forHTTPHeaderField: "Authorization")
        let config = URLSessionConfiguration.ephemeral
        config.timeoutIntervalForRequest = 20
        let session = URLSession(configuration: config)
        let socket = session.webSocketTask(with: request)
        socket.maximumMessageSize = 2_097_152
        socket.resume()
        let stream = QwenCloudStream(session: session, socket: socket)
        do {
            try await stream.waitFor("session.created")
            try await stream.send(QwenCloudProtocol.sessionUpdate(target: direction == .chineseToEnglish ? "en" : "zh"))
            try await stream.waitFor("session.updated")
            return stream
        } catch {
            socket.cancel(with: .goingAway, reason: nil)
            session.invalidateAndCancel()
            throw CloudError.connection
        }
    }

    private func send(_ object: [String: Any]) async throws {
        let data = try JSONSerialization.data(withJSONObject: object)
        try await socket.send(.string(String(decoding: data, as: UTF8.self)))
    }

    private func receive() async throws -> [String: Any] {
        let message = try await socket.receive()
        let data: Data
        switch message {
        case .string(let value): data = Data(value.utf8)
        case .data(let value): data = value
        @unknown default: throw CloudError.protocolError
        }
        guard let event = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = event["type"] as? String else { throw CloudError.protocolError }
        if type == "error" || event["code"] != nil { throw CloudError.service }
        return event
    }

    private func waitFor(_ expected: String) async throws {
        for _ in 0..<20 {
            if try await receive()["type"] as? String == expected { return }
        }
        throw CloudError.protocolError
    }

    func run(tap: TapSession, source: Source, controls: PlaybackControls?, initialMode: PlaybackMode, direction: TranslationDirection, tracePath: String) async throws {
        defer {
            receiver?.cancel()
            socket.cancel(with: .goingAway, reason: nil)
            session.invalidateAndCancel()
        }
        var config = PipelineConfig(session: "qwen-cloud", direction: direction)
        config.providerStreaming = true
        config.playbackMode = initialMode
        pipeline = SessionPipeline(config: config)
        apply(.arm, tap: tap)
        outputResampler = StreamingResampler(inputRate: 24_000, outputRate: Int(tap.sampleRate))
        let trace = try TraceWriter(url: URL(fileURLWithPath: tracePath))
        let records = tap.recordRing.map { RTRecordDrain(ring: $0, reportStarvation: false) }
        try tap.start()
        source.startLive(seconds: nil)
        receiver = Task { [weak self] in
            guard let self else { return }
            do {
                while !Task.isCancelled {
                    let event = try await self.receive()
                    try self.consume(event, tap: tap)
                    if event["type"] as? String == "session.finished" { throw CloudError.service }
                }
            } catch {
                if !Task.isCancelled { self.failure = "Qwen Cloud connection failed or returned invalid audio."; source.stopRequested.withLock { $0 = true } }
            }
        }
        let inputResampler = StreamingResampler(inputRate: canonicalRate, outputRate: 16_000)
        while let chunk = await source.nextLive() {
            if let records {
                _ = records.drain(context: RTTraceContext(session: "qwen-cloud", epoch: 1, generation: 1,
                                                          deviceRate: tap.sampleRate), into: trace)
                if records.invalidTimestamps > 0 { throw CloudError.capture }
            }
            if let mode = controls?.takeLatest() {
                apply(.playbackMode(mode), tap: tap)
            }
            if tap.counters.captureOverflowFrames > 0 { throw CloudError.capture }
            let samples = inputResampler.process(chunk)
            let pcm = QwenCloudProtocol.encodePCM16(samples)
            if !pcm.isEmpty { try await send(["type": "input_audio_buffer.append", "audio": pcm.base64EncodedString()]) }
        }
        if let failure { throw CloudError.message(failure) }
        if let failure = source.failure { throw CloudError.message(failure) }
        // Release the muted source before waiting on a potentially slow network send.
        // The app's Stop action intentionally discards the final in-flight response.
        receiver?.cancel()
        apply(.stop, tap: tap)
        tap.teardown()
        try? await send(["type": "session.finish"])
    }

    private func consume(_ event: [String: Any], tap: TapSession) throws {
        switch event["type"] as? String {
        case "response.audio.delta":
            guard let encoded = event["delta"] as? String, let pcm = Data(base64Encoded: encoded),
                  let samples = QwenCloudProtocol.decodePCM16(pcm) else { throw CloudError.protocolError }
            guard pipeline?.handle(.providerPCM, at: HostClock.nowNs()).contains(.enqueueProviderPCM) == true else { return }
            let output = outputResampler?.process(samples) ?? []
            guard tap.enqueueTranslated(output) == output.count else { throw CloudError.output }
        case "response.audio_transcript.delta":
            guard let id = event["item_id"] as? String, id.utf8.count <= 256,
                  let delta = event["delta"] as? String else { throw CloudError.protocolError }
            if textByItem[id] == nil && textByItem.count >= 100 { textByItem.removeAll() }
            let text = String(((textByItem[id] ?? "") + delta).prefix(4_000))
            textByItem[id] = text
            emit(["caption": "target.delta", "id": id, "text": text])
        case "response.done":
            if let response = event["response"] as? [String: Any], let status = response["status"] as? String, status != "completed" {
                throw CloudError.service
            }
        case "session.finished": break
        default: break
        }
    }

    private func emit(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object), data.count < 32_768 else { return }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([10]))
    }

    private func apply(_ input: PipelineInput, tap: TapSession) {
        for effect in pipeline?.handle(input, at: HostClock.nowNs()) ?? [] {
            switch effect {
            case .flushTranslatedBus:
                tap.flushTranslated()
                outputResampler = StreamingResampler(inputRate: 24_000, outputRate: Int(tap.sampleRate))
            case .rampGains(let gains, let milliseconds):
                tap.setGains(original: gains.original, translated: gains.translated, rampMs: milliseconds)
            default: break
            }
        }
    }
}

private enum CloudError: Error, CustomStringConvertible {
    case connection, protocolError, service, capture, output, message(String)
    var description: String {
        switch self {
        case .connection: "Qwen Cloud connection failed. Check your API key, region, and internet connection."
        case .protocolError: "Qwen Cloud returned an invalid response."
        case .service: "Qwen Cloud reported a translation error."
        case .capture: "Audio capture fell behind; the session stopped."
        case .output: "Translated audio could not keep up; the session stopped."
        case .message(let value): value
        }
    }
}
