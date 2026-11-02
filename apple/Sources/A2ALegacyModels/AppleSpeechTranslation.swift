// Legacy Apple speech and translation adapters for offline research tools only.
import A2ACore
import A2AModels
import AVFoundation
import CoreMedia
import Foundation
import Speech
import Translation

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

/// Closed-segment baseline. No context or prefix support. Glossary terms replace their
/// source spellings with the agreed target term before translation, which the framework
/// passes through. Requests are serialized because TranslationSession is not documented
/// as safe for overlapping calls.
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
        let source = TermMap(req.glossary).apply(req.uncommittedSource)
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

