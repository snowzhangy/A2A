// Apple system-framework adapters (macOS 26): SpeechAnalyzer/SpeechTranscriber ASR,
// Translation framework MT, and a system-voice plumbing synthesizer.
//
// Runtime downloads stay disabled: these adapters refuse to start when the required
// language assets are missing. Installation is an explicit setup step (D8).

import A2ACore
import AVFoundation
import CoreMedia
import Foundation
import Speech
import Synchronization
import Translation

public enum AdapterError: Error, CustomStringConvertible {
    case assetsNotInstalled(String)
    case unsupported(String)
    case conversion(String)

    public var description: String {
        switch self {
        case .assetsNotInstalled(let s): "assets not installed: \(s) (run the setup path; runtime downloads are disabled)"
        case .unsupported(let s): "unsupported: \(s)"
        case .conversion(let s): "audio conversion failed: \(s)"
        }
    }
}

// MARK: SpeechTranscriber

/// Streams 16 kHz frames into SpeechAnalyzer. Volatile results become partial hypotheses
/// of the current utterance; each finalized result closes an utterance. Token spans
/// come from the audioTimeRange attribute, expressed in canonical source samples.
public final class AppleSpeechRecognizer: StreamingRecognizer, @unchecked Sendable {
    private let transcriber: SpeechTranscriber
    private let analyzer: SpeechAnalyzer
    private let format: AVAudioFormat
    private let inputFormat: AVAudioFormat
    private let converter: AVAudioConverter?
    private let input: AsyncStream<AnalyzerInput>.Continuation
    private let state = Locked(State())
    private var resultsTask: Task<Void, Never>?

    struct State {
        var pending: [ASRHypothesis] = []
        var utterance = 1
        var frontier: Int64 = 0
        var error: String?
    }

    public static func checkInstalled(locale: Locale = Locale(identifier: "zh-CN")) async throws -> Locale {
        guard let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            throw AdapterError.unsupported("SpeechTranscriber locale \(locale.identifier)")
        }
        let module = SpeechTranscriber(locale: supported, preset: .timeIndexedProgressiveTranscription)
        var status = await AssetInventory.status(forModules: [module])
        if status == .supported {
            // Assets are reserved per app. Reserving a locale whose assets the setup path already
            // installed attaches them to this process without a download; if a download would
            // still be needed, give the reservation back and refuse (D8).
            if (try? await AssetInventory.reserve(locale: supported)) == true {
                status = await AssetInventory.status(forModules: [module])
                if status != .installed { _ = await AssetInventory.release(reservedLocale: supported) }
            }
        }
        guard status == .installed else { throw AdapterError.assetsNotInstalled("SpeechTranscriber \(supported.identifier): \(status)") }
        return supported
    }

    public init(locale requested: Locale = Locale(identifier: "zh-CN")) async throws {
        let locale = try await Self.checkInstalled(locale: requested)
        transcriber = SpeechTranscriber(locale: locale, transcriptionOptions: [],
                                        reportingOptions: [.volatileResults, .fastResults],
                                        attributeOptions: [.audioTimeRange])
        guard let best = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
            throw AdapterError.unsupported("no compatible analyzer audio format")
        }
        format = best
        inputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false)!
        converter = best == inputFormat ? nil : AVAudioConverter(from: inputFormat, to: best)
        analyzer = SpeechAnalyzer(modules: [transcriber],
                                  options: .init(priority: .userInitiated, modelRetention: .processLifetime))
        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream(bufferingPolicy: .bufferingNewest(512))
        input = continuation
        try await analyzer.start(inputSequence: stream)
        let transcriber = self.transcriber
        let state = self.state
        resultsTask = Task {
            do {
                for try await result in transcriber.results {
                    Self.ingest(result, into: state)
                }
            } catch {
                state.withLock { $0.error = "\(error)" }
            }
        }
    }

    deinit {
        input.finish()
        resultsTask?.cancel()
    }

    static func ingest(_ result: SpeechTranscriber.Result, into state: Locked<State>) {
        var tokens: [ASRToken] = []
        for run in result.text.runs {
            let text = String(result.text[run.range].characters)
            var span: SourceSpan?
            if let range = run.audioTimeRange {
                let s = Int64((range.start.seconds * Double(canonicalSampleRate)).rounded())
                let e = Int64((range.end.seconds * Double(canonicalSampleRate)).rounded())
                if e >= s { span = SourceSpan(start: s, end: e) }
            }
            tokens.append(ASRToken(text: text, span: span))
        }
        let isFinal = CMTimeCompare(result.resultsFinalizationTime, result.range.end) >= 0
        state.withLock { s in
            guard s.pending.count < 512 else {
                s.error = "recognition result queue exceeded its limit"
                return
            }
            s.pending.append(ASRHypothesis(utterance: s.utterance, tokens: tokens, isFinal: isFinal,
                                           audioFrontier: s.frontier))
            if isFinal { s.utterance += 1 }
        }
    }

    public func accept(_ frame: AudioFrame16k) {
        guard !frame.samples.isEmpty, lastError == nil else { return }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: inputFormat, frameCapacity: AVAudioFrameCount(frame.samples.count)) else { return }
        buffer.frameLength = AVAudioFrameCount(frame.samples.count)
        frame.samples.withUnsafeBufferPointer { src in
            buffer.floatChannelData![0].update(from: src.baseAddress!, count: src.count)
        }
        let start = CMTime(value: frame.span.start, timescale: CMTimeScale(canonicalSampleRate))
        state.withLock { $0.frontier = max($0.frontier, frame.span.end) }
        if let converter {
            let ratio = format.sampleRate / inputFormat.sampleRate
            guard let out = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(Double(frame.samples.count) * ratio) + 32) else { return }
            var consumed = false
            var error: NSError?
            converter.convert(to: out, error: &error) { _, status in
                if consumed { status.pointee = .noDataNow; return nil }
                consumed = true
                status.pointee = .haveData
                return buffer
            }
            if let error { state.withLock { $0.error = error.localizedDescription } }
            else { enqueue(AnalyzerInput(buffer: out, bufferStartTime: start)) }
        } else {
            enqueue(AnalyzerInput(buffer: buffer, bufferStartTime: start))
        }
    }

    private func enqueue(_ value: AnalyzerInput) {
        if case .dropped = input.yield(value) {
            state.withLock { $0.error = "recognition input queue overflow; audio was not accepted" }
        }
    }

    public func requestFinalize() {
        let analyzer = self.analyzer
        Task { try? await analyzer.finalize(through: nil) }
    }

    public func drainHypotheses() -> [ASRHypothesis] {
        state.withLock { defer { $0.pending.removeAll() }; return $0.pending }
    }

    public var lastError: String? { state.withLock { $0.error } }

    public func reset() {
        state.withLock { $0.pending.removeAll() }
    }

    public func finish() async {
        input.finish()
        try? await analyzer.finalizeAndFinishThroughEndOfInput()
        await resultsTask?.value
    }
}

// MARK: Translation framework

/// Closed-segment baseline. No context, glossary, or prefix support. Requests are
/// serialized because TranslationSession is not documented as safe for overlapping calls.
public actor AppleTranslator: IncrementalTranslator {
    nonisolated(unsafe) private let session: TranslationSession
    private var tail: Task<Void, Never>?
    public nonisolated var supportsPrefix: Bool { false }
    public nonisolated var supportsContext: Bool { false }

    public static func checkInstalled(source: Locale.Language = .init(identifier: "zh-Hans"),
                                      target: Locale.Language = .init(identifier: "en")) async throws {
        let status = await LanguageAvailability().status(from: source, to: target)
        // Availability depends on the SDK version recorded in the executable, not only on
        // what is downloaded: a binary stamped with an older SDK sees `supported`.
        guard status == .installed else {
            throw AdapterError.assetsNotInstalled("Translation \(source.minimalIdentifier)→\(target.minimalIdentifier): \(status). "
                + "If the languages are installed in System Settings, check that this executable records the current SDK "
                + "(otool -l: LC_BUILD_VERSION sdk; build through scripts/env.sh)")
        }
    }

    public init(source: Locale.Language = .init(identifier: "zh-Hans"), target: Locale.Language = .init(identifier: "en")) {
        session = TranslationSession(installedSource: source, target: target)
    }

    public func translate(_ req: TranslationRequest) async throws -> TranslationCandidate {
        try Task.checkCancellation()
        let previous = tail
        nonisolated(unsafe) let session = self.session
        let source = req.uncommittedSource
        let task = Task { () async throws -> String in
            await previous?.value
            try Task.checkCancellation()
            return try await session.translate(source).targetText
        }
        tail = Task { _ = try? await task.value }
        return try await withTaskCancellationHandler {
            let text = try await task.value
            try Task.checkCancellation()
            return TranslationCandidate(unitID: req.unitID, text: text)
        } onCancel: { task.cancel() }
    }
}

// MARK: System voice (plumbing only)

/// AVSpeechSynthesizer output for end-to-end plumbing. It does not preserve the source
/// speaker, so it can never satisfy an identity gate.
///
/// With a target duration (fluent playout), the voice speaks faster natively instead of being
/// time-stretched, and the ~0.2 s of digital silence AVSpeechSynthesizer appends to every
/// utterance is cut to a pause that fits the unit's ending.
///
/// A profile that carries the speaker's pitch (multi-speaker sessions) is cast its own
/// installed voice (`VoiceCasting`), so each speaker in a dialogue sounds different. Without
/// one, every speaker uses the language's default voice.
public final class SystemVoiceSynthesizer: VoiceSynthesizer, @unchecked Sendable {
    private let voiceIdentifier: String?
    private let warmUpText: String
    private let requestedLanguage: String
    private let maxSecondsPerCharacter: Double
    private let initialSecondsPerCharacter: Double
    /// Learned voiced seconds per character at the default rate, per voice, from completed paced units.
    private let secondsPerCharacter = Locked<[String: Double]>([:])
    private let casting: Locked<VoiceCasting>
    /// Cast role per speaker; a PreparedVoice handle is the role index plus one (0 = default voice).
    private let roles = Locked<[SpeakerID: Int]>([:])
    private let warmed = Locked<Set<String>>([])

    /// Speech speed-up for each `AVSpeechUtterance.rate`, measured with the compact en-US
    /// voice on the reference Mac (macOS 27): the same factor for short and long sentences.
    static let speedForRate: [(rate: Float, speed: Double)] = [
        (0.50, 1.0), (0.52, 1.077), (0.54, 1.234), (0.56, 1.354), (0.58, 1.424), (0.60, 1.591),
    ]
    /// Beyond this, faster speech costs more naturalness than the latency it saves; fluent
    /// catch-up skipping handles the rest of the debt.
    public static let maxSpeed = 1.3
    /// Samples below this are silence (AVSpeechSynthesizer pads with exact zeros).
    static let silenceLevel: Float = 1e-4

    private final class Job: @unchecked Sendable {
        let synthesizer = AVSpeechSynthesizer()
        let paced: Bool
        let postRollSeconds: Double
        struct Pending: Sendable {
            var sampleRate = 0
            var samples: [Float] = []
            var received = 0         // samples delivered by the synthesizer so far
            var firstVoiced: Int?    // absolute sample indices, for duration learning
            var lastVoiced = 0
            var stopped = false
        }
        let pending = Mutex(Pending())

        init(paced: Bool, postRollSeconds: Double) {
            self.paced = paced
            self.postRollSeconds = postRollSeconds
        }

        // AVSpeechSynthesizer can burst many tiny buffers before the consumer resumes.
        // Coalesce to 100 ms so the stream limit bounds audio duration, not framework
        // callback frequency. Flush the short final chunk without padding it. Paced jobs
        // hold back trailing silence until voice follows, and keep only a short post-roll.
        func coalesce(_ samples: UnsafeBufferPointer<Float>, sampleRate: Int, final: Bool) -> [SynthesizedAudio] {
            pending.withLock { state in
                state.sampleRate = sampleRate
                let voiced = { (x: Float) in abs(x) >= SystemVoiceSynthesizer.silenceLevel }
                if let last = samples.lastIndex(where: voiced) {
                    if state.firstVoiced == nil { state.firstVoiced = state.received + (samples.firstIndex(where: voiced) ?? last) }
                    state.lastVoiced = state.received + last + 1
                }
                state.received += samples.count
                state.samples.append(contentsOf: samples)
                let voicedEnd = paced ? max(0, state.samples.count - (state.received - state.lastVoiced)) : state.samples.count
                let frames = max(1, sampleRate / 10)
                let format = PCMFormat(sampleRate: sampleRate, channelCount: 1)
                var chunks: [SynthesizedAudio] = []
                var offset = 0
                while voicedEnd - offset >= frames {
                    chunks.append(SynthesizedAudio(samples: Array(state.samples[offset..<offset + frames]), format: format))
                    offset += frames
                }
                if final {
                    let end = paced ? min(state.samples.count, voicedEnd + Int(postRollSeconds * Double(sampleRate)))
                        : state.samples.count
                    if offset < end {
                        chunks.append(SynthesizedAudio(samples: Array(state.samples[offset..<end]), format: format))
                    }
                    offset = state.samples.count
                }
                if offset > 0 { state.samples.removeFirst(offset) }
                return chunks
            }
        }

        /// Voiced duration of a job that ran to completion, or nil.
        var voicedSeconds: Double? {
            pending.withLock { s in
                guard !s.stopped, let first = s.firstVoiced, s.sampleRate > 0, s.lastVoiced > first else { return nil }
                return Double(s.lastVoiced - first) / Double(s.sampleRate)
            }
        }
    }

    public init(language: String = "en-US") {
        voiceIdentifier = AVSpeechSynthesisVoice(language: language)?.identifier
        requestedLanguage = language
        let mandarin = language.lowercased().hasPrefix("zh")
        maxSecondsPerCharacter = mandarin ? 0.6 : 0.2
        warmUpText = mandarin ? TranslationLanguage.mandarin.warmUpText : TranslationLanguage.english.warmUpText
        // Initial estimates only; completed units learn each voice's actual cadence.
        initialSecondsPerCharacter = mandarin ? 0.22 : 0.055
        // Only voices this process can open: the listing can include voices whose assets
        // are not available to it.
        let installed = AVSpeechSynthesisVoice.speechVoices().compactMap { v -> VoiceCasting.InstalledVoice? in
            guard AVSpeechSynthesisVoice(identifier: v.identifier) != nil else { return nil }
            let gender: VoiceCasting.Gender? = switch v.gender {
            case .male: .male
            case .female: .female
            default: nil
            }
            return VoiceCasting.InstalledVoice(identifier: v.identifier, name: v.name, language: v.language,
                                               gender: gender, quality: v.quality.rawValue,
                                               novelty: v.voiceTraits.contains(.isNoveltyVoice),
                                               personal: v.voiceTraits.contains(.isPersonalVoice))
        }
        casting = Locked(VoiceCasting(candidates: VoiceCasting.candidates(from: installed, locale: language)))
    }

    public var capabilities: SynthesisCapabilities {
        SynthesisCapabilities(audioOutputStreaming: true, incrementalTextInput: false,
                              continuationAcrossUnits: false, preservesSpeakerIdentity: false)
    }

    /// With the speaker's pitch, casts (once per speaker) and warms that speaker's voice.
    public func prepare(_ profile: VoiceProfile) async throws -> PreparedVoice {
        var handle = 0
        if let pitch = profile.pitchHz {
            let speaker = profile.speakerID
            let role = roles.withLock { $0[speaker] } ?? casting.withLock { c -> Int? in c.cast(pitchHz: pitch) }
            if let role {
                roles.withLock { $0[speaker] = role }
                handle = role + 1
            }
        }
        let voice = PreparedVoice(speakerID: profile.speakerID, profileVersion: profile.version, handle: handle)
        if handle > 0 { await warmUp(voice) }
        return voice
    }

    /// Name of the installed voice a prepared voice speaks with, for logs.
    public func voiceName(for voice: PreparedVoice) -> String? {
        role(for: voice).flatMap { AVSpeechSynthesisVoice(identifier: $0.identifier)?.name }
    }

    /// Identifier and pitch for a prepared voice; handle 0 is the language's default voice.
    func role(for voice: PreparedVoice) -> (identifier: String, pitchMultiplier: Float)? {
        if voice.handle > 0, let role = casting.withLock({ c in
            voice.handle <= c.roles.count ? c.roles[voice.handle - 1] : nil
        }) {
            return (role.identifier, role.pitchMultiplier)
        }
        return voiceIdentifier.map { ($0, 1) }
    }

    /// Loads the voice before the first unit: its first utterance takes about 0.7 s to start,
    /// against about 0.3 s once warm (reference Mac).
    public func warmUp() async {
        await warmUp(PreparedVoice(speakerID: SpeakerID("warm-up"), profileVersion: 0, handle: 0))
    }

    private func warmUp(_ voice: PreparedVoice) async {
        guard let id = role(for: voice)?.identifier, warmed.withLock({ $0.insert(id).inserted }) else { return }
        do { for try await _ in synthesize(unit: UnitID(0), text: warmUpText, voice: voice) {} } catch {}
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: nil)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice,
                           targetSeconds: Double?) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        guard let role = role(for: voice), let selectedVoice = AVSpeechSynthesisVoice(identifier: role.identifier) else {
            return AsyncThrowingStream { $0.finish(throwing: AdapterError.unsupported("system voice for \(requestedLanguage)")) }
        }
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = selectedVoice
        if role.pitchMultiplier != 1 { utterance.pitchMultiplier = role.pitchMultiplier }
        // A sentence keeps a sentence-length pause; a clause flows into the next one.
        let postRoll = text.last.map { ".!?。！？".contains($0) } == true ? 0.18 : 0.09
        var paced = false
        var speed = 1.0
        let voiceID = role.identifier
        let initial = initialSecondsPerCharacter
        if let targetSeconds, targetSeconds.isFinite, targetSeconds > 0 {
            paced = true
            speed = Self.speed(text: text, targetSeconds: targetSeconds, postRollSeconds: postRoll,
                               secondsPerCharacter: secondsPerCharacter.withLock { $0[voiceID] ?? initial })
            utterance.rate = Self.rate(forSpeed: speed)
        }
        let job = Job(paced: paced, postRollSeconds: postRoll)
        let learned = secondsPerCharacter
        let maxSecondsPerCharacter = self.maxSecondsPerCharacter
        return AsyncThrowingStream(bufferingPolicy: .bufferingOldest(128)) { continuation in
            continuation.onTermination = { _ in
                job.pending.withLock { $0.stopped = true }
                DispatchQueue.main.async { job.synthesizer.stopSpeaking(at: .immediate) }
            }
            job.synthesizer.write(utterance) { buffer in
                guard let pcm = buffer as? AVAudioPCMBuffer else { return }
                let n = Int(pcm.frameLength)
                var samples = [Float](repeating: 0, count: n)
                if n > 0, let f = pcm.floatChannelData {
                    samples.withUnsafeMutableBufferPointer { $0.baseAddress!.update(from: f[0], count: n) }
                } else if let s = pcm.int16ChannelData {
                    for i in 0..<n { samples[i] = Float(s[0][i]) / 32_768 }
                }
                let chunks = samples.withUnsafeBufferPointer {
                    job.coalesce($0, sampleRate: Int(pcm.format.sampleRate), final: n == 0)
                }
                for chunk in chunks {
                    if case .dropped = continuation.yield(chunk) {
                        continuation.finish(throwing: AdapterError.conversion("synthesis consumer exceeded 12.8 s of buffered audio"))
                        return
                    }
                }
                guard n == 0 else { return }
                if paced, let voiced = job.voicedSeconds, !text.isEmpty {
                    let observed = min(maxSecondsPerCharacter, max(0.02, voiced * speed / Double(text.count)))
                    learned.withLock { $0[voiceID] = 0.7 * ($0[voiceID] ?? initial) + 0.3 * observed }
                }
                continuation.finish()
            }
        }
    }

    /// Speed-up that fits the text into its source duration, within `maxSpeed`. Small
    /// overruns are tolerated so short acknowledgements keep their natural cadence.
    static func speed(text: String, targetSeconds: Double, postRollSeconds: Double, secondsPerCharacter: Double) -> Double {
        let natural = Double(text.count) * secondsPerCharacter + postRollSeconds
        return min(maxSpeed, max(1, natural / (max(0.5, targetSeconds) + 0.15)))
    }

    /// Inverse of `speedForRate`, interpolated linearly between measured points.
    static func rate(forSpeed speed: Double) -> Float {
        let table = speedForRate
        guard speed > table[0].speed else { return table[0].rate }
        for (a, b) in zip(table, table.dropFirst()) where speed <= b.speed {
            let f = (speed - a.speed) / (b.speed - a.speed)
            return a.rate + Float(f) * (b.rate - a.rate)
        }
        return table[table.count - 1].rate
    }
}
