// Reference-conditioned synthesis through the resident worker process (research/tts_worker.py).
//
// The worker keeps Qwen3-TTS resident on MLX and streams 24 kHz PCM per unit over a
// length-prefixed stdin/stdout protocol (see the worker's header). Reference audio and
// text travel in memory only. Closing the worker's stdin ends it, so a host that exits or
// is killed never leaves the model resident. The model entry is research-only in
// models/manifest.yaml and identity is unqualified: reports must not count these runs as
// identity or gate evidence.

import A2ACore
import A2AMedia
import Darwin
import Foundation
import Synchronization

public enum CloneConditioning: String, Sendable, CaseIterable {
    /// Speaker embedding plus reference codec tokens and transcript (in-context learning).
    case icl
    /// Audio-only input from the pipeline. Qwen uses a speaker embedding; backends
    /// that require text transcribe the reference separately during preparation.
    case xvector
}

/// Worker-side pacing of one completed unit: numbers only, no text or audio.
public struct CloneSynthesisStats: Sendable, Equatable {
    /// WSOLA pacing applied to fit the source span: above 1 a long take was sped up, below 1 a
    /// short one was slowed to end with the actor (fill), 1 = no pacing.
    public let pace: Double
    /// Shaped audio before pacing and quiet-run compaction.
    public let nativeSeconds: Double
    /// Audio actually sent to the host.
    public let audioSeconds: Double
    public let quietRemovedSeconds: Double
    /// Character-count duration budget the worker enforced, and whether it stopped generation.
    public let budgetSeconds: Double
    public let truncated: Bool
    /// Takes discarded because they produced no voiced audio before the worker's silent-take limit.
    public let retries: Int

    public init(pace: Double, nativeSeconds: Double, audioSeconds: Double, quietRemovedSeconds: Double,
                budgetSeconds: Double = 0, truncated: Bool = false, retries: Int = 0) {
        self.pace = pace
        self.nativeSeconds = nativeSeconds
        self.audioSeconds = audioSeconds
        self.quietRemovedSeconds = quietRemovedSeconds
        self.budgetSeconds = budgetSeconds
        self.truncated = truncated
        self.retries = retries
    }
}

public struct CloneWorkerInfo: Sendable {
    public let model: String
    public let loadSeconds: Double
    public let warmupSeconds: Double
    public let streamsAudio: Bool
}

public final class CloneVoiceSynthesizer: VoiceSynthesizer, @unchecked Sendable {
    public static let sampleRate = 24_000

    public let conditioning: CloneConditioning
    public let targetLanguage: TranslationLanguage
    public private(set) var info: CloneWorkerInfo?
    private let process: Process
    private let stdinPipe = Pipe()
    private let stdoutPipe = Pipe()
    private let inputFD: Int32
    /// Serializes frames and records closure, so no write can reach a reused descriptor.
    private let input = Mutex(false)
    private let shared = Shared()

    private final class Shared: Sendable {
        let state = Mutex(State())
    }

    private struct State {
        var nextID = 0
        var streams: [Int: AsyncThrowingStream<SynthesizedAudio, Error>.Continuation] = [:]
        var units: [Int: UnitID] = [:]
        var stats: [UnitID: CloneSynthesisStats] = [:]
        var prepares: [String: CheckedContinuation<Void, Error>] = [:]
        var ready: CheckedContinuation<CloneWorkerInfo, Error>?
        var readyInfo: CloneWorkerInfo?
        var failure: String?
    }

    /// Starts the worker and waits until its model is loaded and warmed up.
    public init(executable: URL, arguments: [String], conditioning: CloneConditioning = .icl,
                targetLanguage: TranslationLanguage = .english,
                readyTimeout: Duration = .seconds(180)) async throws {
        self.conditioning = conditioning
        self.targetLanguage = targetLanguage
        inputFD = stdinPipe.fileHandleForWriting.fileDescriptor
        process = Process()
        process.executableURL = executable
        process.arguments = arguments
        process.standardInput = stdinPipe
        process.standardOutput = stdoutPipe
        process.standardError = FileHandle.standardError
        // A dead worker must surface as an error, not SIGPIPE in the host.
        _ = fcntl(inputFD, F_SETNOSIGPIPE, 1)
        try process.run()
        let fd = stdoutPipe.fileHandleForReading.fileDescriptor
        let reader = Thread { [shared] in Self.readLoop(fd: fd, shared: shared) }
        reader.name = "a2a.clone-worker.reader"
        reader.start()
        let timeout = Task { [shared] in
            try await Task.sleep(for: readyTimeout)
            Self.fail(shared, "worker did not become ready within \(readyTimeout)")
        }
        defer { timeout.cancel() }
        do {
            info = try await withCheckedThrowingContinuation { continuation in
                shared.state.withLock { s in
                    if let ready = s.readyInfo { continuation.resume(returning: ready) }
                    else if let failure = s.failure { continuation.resume(throwing: AdapterError.unsupported(failure)) }
                    else { s.ready = continuation }
                }
            }
        } catch {
            shutdown()
            if process.isRunning { process.terminate() }
            throw error
        }
    }

    deinit { shutdown() }

    /// Closes the worker's stdin; it exits on EOF without finishing queued work.
    public func shutdown() {
        input.withLock { closed in
            guard !closed else { return }
            closed = true
            try? stdinPipe.fileHandleForWriting.close()
        }
    }

    /// Set once the worker exits or breaks protocol; every later request fails.
    public var failure: String? { shared.state.withLock { $0.failure } }

    public var capabilities: SynthesisCapabilities {
        SynthesisCapabilities(audioOutputStreaming: info?.streamsAudio ?? true, incrementalTextInput: false,
                              continuationAcrossUnits: false, preservesSpeakerIdentity: true)
    }

    public func prepare(_ profile: VoiceProfile) async throws -> PreparedVoice {
        var pcm = profile.referencePCM
        if profile.channelCount > 1 {
            pcm = WAVAudio(sampleRate: profile.sampleRate, channels: profile.channelCount, samples: pcm).mono()
        }
        if profile.sampleRate != Self.sampleRate {
            pcm = StreamingResampler.resample(pcm, from: profile.sampleRate, to: Self.sampleRate)
        }
        let key = Self.key(profile.speakerID, profile.version)
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            let failure = shared.state.withLock { s -> String? in
                if let failure = s.failure { return failure }
                s.prepares[key]?.resume(throwing: CancellationError())
                s.prepares[key] = continuation
                return nil
            }
            if let failure { continuation.resume(throwing: AdapterError.unsupported(failure)); return }
            send(["op": "prepare", "voice": key, "text": profile.referenceText, "mode": conditioning.rawValue,
                  "rate": Self.sampleRate, "language": targetLanguage.cloneLanguage], payload: pcm)
        }
        return PreparedVoice(speakerID: profile.speakerID, profileVersion: profile.version, handle: profile.version)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: nil, allowFill: false)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: targetSeconds, allowFill: false)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?,
                           allowFill: Bool) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        // About 80 s of 0.32 s chunks; the driver forwards each chunk as it arrives.
        AsyncThrowingStream(bufferingPolicy: .bufferingOldest(256)) { continuation in
            let (id, failure) = shared.state.withLock { s -> (Int, String?) in
                s.nextID += 1
                if s.failure == nil {
                    s.streams[s.nextID] = continuation
                    s.units[s.nextID] = unit
                }
                return (s.nextID, s.failure)
            }
            if let failure {
                continuation.finish(throwing: AdapterError.unsupported(failure))
                return
            }
            continuation.onTermination = { [weak self] _ in
                guard let self else { return }
                // Still registered means the consumer stopped early: cancel in the worker.
                if self.shared.state.withLock({ s in
                    s.units[id] = nil
                    return s.streams.removeValue(forKey: id)
                }) != nil {
                    self.send(["op": "cancel", "id": id])
                }
            }
            var request: [String: Any] = ["op": "synth", "id": id,
                "voice": Self.key(voice.speakerID, voice.profileVersion), "text": text]
            if let targetSeconds, targetSeconds.isFinite, targetSeconds > 0 {
                request["target_s"] = targetSeconds
                // Only a slotted host may slow a take; see the protocol comment in A2ACore.
                if allowFill { request["fill"] = true }
            }
            send(request)
        }
    }

    /// Pacing of a unit whose synthesis completed, once; nil when canceled or unknown.
    public func takeStats(for unit: UnitID) -> CloneSynthesisStats? {
        shared.state.withLock { $0.stats.removeValue(forKey: unit) }
    }

    // MARK: Protocol

    private static func key(_ speaker: SpeakerID, _ version: Int) -> String { "\(speaker.rawValue)/\(version)" }

    private func send(_ header: [String: Any], payload: [Float] = []) {
        guard let json = try? JSONSerialization.data(withJSONObject: header) else { return }
        var frame = Data(capacity: 8 + json.count + payload.count * 4)
        withUnsafeBytes(of: UInt32(json.count).littleEndian) { frame.append(contentsOf: $0) }
        withUnsafeBytes(of: UInt32(payload.count * 4).littleEndian) { frame.append(contentsOf: $0) }
        frame.append(json)
        payload.withUnsafeBytes { frame.append(contentsOf: $0) }
        let fd = inputFD
        let ok = input.withLock { closed in
            guard !closed else { return false }
            return frame.withUnsafeBytes { raw -> Bool in
                var offset = 0
                while offset < raw.count {
                    let n = write(fd, raw.baseAddress! + offset, raw.count - offset)
                    if n < 0, errno == EINTR { continue }
                    if n <= 0 { return false }
                    offset += n
                }
                return true
            }
        }
        if !ok { Self.fail(shared, "worker input closed") }
    }

    private static func readLoop(fd: Int32, shared: Shared) {
        func readExact(_ count: Int) -> [UInt8]? {
            var buffer = [UInt8](repeating: 0, count: count)
            var offset = 0
            while offset < count {
                let n = buffer.withUnsafeMutableBytes { read(fd, $0.baseAddress! + offset, count - offset) }
                if n < 0, errno == EINTR { continue }
                if n <= 0 { return nil }
                offset += n
            }
            return buffer
        }
        while true {
            guard let prefix = readExact(8) else { break }
            let headerLength = Int(prefix[0..<4].withUnsafeBytes { $0.loadUnaligned(as: UInt32.self).littleEndian })
            let payloadLength = Int(prefix[4..<8].withUnsafeBytes { $0.loadUnaligned(as: UInt32.self).littleEndian })
            guard headerLength <= 1 << 20, payloadLength <= 64 << 20, payloadLength % 4 == 0,
                  let header = readExact(headerLength), let payload = payloadLength > 0 ? readExact(payloadLength) : [] else {
                fail(shared, "worker protocol error")
                return
            }
            // Foundation objects from JSON parsing: drain per frame on this long-lived thread.
            let ok = autoreleasepool { handle(header: header, payload: payload, shared: shared) }
            if !ok {
                fail(shared, "worker protocol error")
                return
            }
        }
        fail(shared, "worker exited")
    }

    private static func handle(header: [UInt8], payload: [UInt8], shared: Shared) -> Bool {
        guard let event = try? JSONSerialization.jsonObject(with: Data(header)) as? [String: Any],
              let kind = event["ev"] as? String else { return false }
        let id = (event["id"] as? NSNumber)?.intValue
        switch kind {
        case "ready":
            let info = CloneWorkerInfo(model: event["model"] as? String ?? "",
                                       loadSeconds: (event["load_s"] as? NSNumber)?.doubleValue ?? 0,
                                       warmupSeconds: (event["warmup_s"] as? NSNumber)?.doubleValue ?? 0,
                                       streamsAudio: event["streaming"] as? Bool ?? true)
            let waiter = shared.state.withLock { s -> CheckedContinuation<CloneWorkerInfo, Error>? in
                s.readyInfo = info
                defer { s.ready = nil }
                return s.ready
            }
            waiter?.resume(returning: info)
        case "prepared":
            guard let voice = event["voice"] as? String else { return false }
            shared.state.withLock { $0.prepares.removeValue(forKey: voice) }?.resume()
        case "chunk":
            guard let id else { return false }
            // f32le payload; Apple silicon is little-endian, so one copy decodes it.
            let samples = [Float](unsafeUninitializedCapacity: payload.count / 4) { buffer, count in
                payload.withUnsafeBytes { raw in
                    UnsafeMutableRawBufferPointer(buffer).copyMemory(from: UnsafeRawBufferPointer(rebasing: raw[..<(buffer.count * 4)]))
                }
                count = buffer.count
            }
            guard let continuation = shared.state.withLock({ $0.streams[id] }) else { return true }   // canceled
            if case .dropped = continuation.yield(SynthesizedAudio(samples: samples, format: PCMFormat(sampleRate: sampleRate, channelCount: 1))) {
                shared.state.withLock { _ = $0.streams.removeValue(forKey: id) }
                continuation.finish(throwing: AdapterError.conversion("synthesis consumer fell behind"))
            }
        case "done":
            guard let id else { return false }
            let number = { (key: String) in (event[key] as? NSNumber)?.doubleValue ?? 0 }
            shared.state.withLock { s -> AsyncThrowingStream<SynthesizedAudio, Error>.Continuation? in
                let unit = s.units.removeValue(forKey: id)
                guard let continuation = s.streams.removeValue(forKey: id) else { return nil }
                if let unit, event["canceled"] as? Bool != true {
                    // Bounded: the driver takes each entry when the unit finishes.
                    if s.stats.count >= 64 { s.stats.removeAll() }
                    s.stats[unit] = CloneSynthesisStats(
                        pace: (event["pace"] as? NSNumber)?.doubleValue ?? 1, nativeSeconds: number("native_s"),
                        audioSeconds: number("audio_s"),
                        quietRemovedSeconds: number("quiet_samples_removed") / Double(sampleRate),
                        budgetSeconds: number("budget_s"), truncated: event["truncated"] as? Bool ?? false,
                        retries: (event["retries"] as? NSNumber)?.intValue ?? 0)
                }
                return continuation
            }?.finish()
        case "error":
            let message = event["message"] as? String ?? "worker error"
            if let id {
                shared.state.withLock { s in
                    s.units[id] = nil
                    return s.streams.removeValue(forKey: id)
                }?.finish(throwing: AdapterError.unsupported(message))
            } else if let voice = event["voice"] as? String {
                shared.state.withLock { $0.prepares.removeValue(forKey: voice) }?.resume(throwing: AdapterError.unsupported(message))
            } else {
                fail(shared, message)
            }
        default:
            return false
        }
        return true
    }

    /// Fails everything waiting on the worker and every later request.
    private static func fail(_ shared: Shared, _ message: String) {
        let (ready, prepares, streams) = shared.state.withLock { s in
            if s.failure == nil { s.failure = message }
            defer { s.ready = nil; s.prepares.removeAll(); s.streams.removeAll(); s.units.removeAll() }
            return (s.ready, Array(s.prepares.values), Array(s.streams.values))
        }
        let error = AdapterError.unsupported(message)
        ready?.resume(throwing: error)
        prepares.forEach { $0.resume(throwing: error) }
        streams.forEach { $0.finish(throwing: error) }
    }
}
