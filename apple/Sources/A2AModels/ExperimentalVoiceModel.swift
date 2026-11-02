import Foundation
import Synchronization

/// Stable setup/worker keys. Artifact provenance stays in models/manifest.yaml.
public enum ExperimentalVoiceModel: String, CaseIterable, Hashable, Sendable {
    case qwen
    case audio8
    case omnivoice
    case chatterbox

    public var name: String {
        switch self {
        case .qwen: "Qwen3-TTS"
        case .audio8: "Audio8 Realistic"
        case .omnivoice: "OmniVoice"
        case .chatterbox: "Chatterbox Multilingual"
        }
    }

    public var playbackDetail: String {
        switch self {
        case .qwen: "Streams speech as it generates; audio-only voice reference."
        case .audio8, .omnivoice: "Generates each phrase before playback. A short speaker reference is transcribed locally once per speaker."
        case .chatterbox: "Generates each phrase before playback; audio-only voice reference."
        }
    }
}

public struct ExperimentalVoiceInstallation: Decodable, Sendable {
    public let key: String
    public let installed: Bool
    public let bytes: Int64

    public init(key: String, installed: Bool, bytes: Int64) {
        self.key = key
        self.installed = installed
        self.bytes = bytes
    }
}

public enum ExperimentalVoiceSetup {
    /// Runs the lightweight catalog/setup tool off the UI thread. Only `install` downloads.
    public static func run(python: String, script: String, install: ExperimentalVoiceModel? = nil) async throws
        -> [ExperimentalVoiceInstallation] {
        try await Task.detached {
            let process = Process()
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = [script] + (install.map { ["--install", $0.rawValue] } ?? ["--list"])
            let output = Pipe(), errors = Pipe()
            let errorBytes = Mutex(Data())
            errors.fileHandleForReading.readabilityHandler = { handle in
                let data = handle.availableData
                if data.isEmpty { handle.readabilityHandler = nil; return }
                errorBytes.withLock {
                    $0.append(data)
                    if $0.count > 8_192 { $0.removeFirst($0.count - 8_192) }
                }
            }
            defer { errors.fileHandleForReading.readabilityHandler = nil }
            process.standardInput = FileHandle.nullDevice
            process.standardOutput = output
            process.standardError = errors
            try process.run()
            let data = output.fileHandleForReading.readData(ofLength: 65_537)
            guard data.count <= 65_536 else {
                if process.isRunning { process.terminate() }
                process.waitUntilExit()
                throw AdapterError.unsupported("voice catalog exceeded its size limit")
            }
            process.waitUntilExit()
            guard process.terminationStatus == 0 else {
                let detail = errorBytes.withLock { String(decoding: $0, as: UTF8.self) }
                throw AdapterError.unsupported("voice model setup failed: \(detail)")
            }
            return try JSONDecoder().decode([ExperimentalVoiceInstallation].self, from: data)
        }.value
    }
}
