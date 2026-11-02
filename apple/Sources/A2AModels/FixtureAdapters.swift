// Deterministic adapters for CI, golden replay, and plumbing tests. They model latency
// but no model quality; reports must never cite them as model results.

import A2ACore
import Foundation
import Synchronization

/// Modeled compute time, applied by drivers either virtually or by sleeping.
public struct LatencyModel: Sendable, Codable {
    public var fixedMs: Double
    public var perUnitMs: Double

    public init(fixedMs: Double = 0, perUnitMs: Double = 0) {
        self.fixedMs = fixedMs
        self.perUnitMs = perUnitMs
    }

    public func nanoseconds(units: Int) -> UInt64 {
        UInt64(max(0, fixedMs + perUnitMs * Double(units)) * 1_000_000)
    }
}

// MARK: Scripted recognizer

/// `{"hypotheses":[{"at_sample":96000,"utt":1,"text":"我们","final":false,"tokens":[...]}]}`
public struct ASRScript: Codable, Sendable {
    public struct Token: Codable, Sendable {
        public let text: String
        public let start: Int64?
        public let end: Int64?

        public init(text: String, start: Int64?, end: Int64?) {
            self.text = text
            self.start = start
            self.end = end
        }
    }

    public struct Entry: Codable, Sendable {
        /// Canonical source sample at which the recognizer may emit this hypothesis.
        public let atSample: Int64
        public let utt: Int
        public let text: String
        public let final: Bool
        public let tokens: [Token]?

        enum CodingKeys: String, CodingKey {
            case atSample = "at_sample", utt, text, final, tokens
        }

        public init(atSample: Int64, utt: Int, text: String, final: Bool, tokens: [Token]?) {
            self.atSample = atSample
            self.utt = utt
            self.text = text
            self.final = final
            self.tokens = tokens
        }
    }

    public let hypotheses: [Entry]

    public init(hypotheses: [Entry]) {
        self.hypotheses = hypotheses
    }

    public static func load(_ url: URL) throws -> ASRScript {
        try JSONDecoder().decode(ASRScript.self, from: Data(contentsOf: url))
    }
}

/// Replays logged hypotheses once the accepted audio frontier reaches their time.
/// It cannot emit a hypothesis for audio it has not received.
public final class ScriptedRecognizer: StreamingRecognizer, @unchecked Sendable {
    private let entries: [ASRScript.Entry]
    private var next = 0
    private var frontier: Int64 = 0
    private var pending: [ASRHypothesis] = []
    public private(set) var finalizeRequests = 0

    public init(script: ASRScript) {
        entries = script.hypotheses.sorted { $0.atSample < $1.atSample }
    }

    public func accept(_ frame: AudioFrame16k) {
        frontier = max(frontier, frame.span.end)
        while next < entries.count, entries[next].atSample <= frontier {
            let e = entries[next]
            let tokens: [ASRToken] = e.tokens.map { list in
                list.map { t in
                    ASRToken(text: t.text, span: t.start.flatMap { s in t.end.map { SourceSpan(start: s, end: $0) } })
                }
            } ?? [ASRToken(text: e.text)]
            pending.append(ASRHypothesis(utterance: e.utt, tokens: tokens, isFinal: e.final, audioFrontier: frontier))
            next += 1
        }
    }

    public func requestFinalize() { finalizeRequests += 1 }

    public func drainHypotheses() -> [ASRHypothesis] {
        defer { pending.removeAll() }
        return pending
    }

    public func reset() {
        pending.removeAll()
    }
}

// MARK: Dictionary translator

/// Looks up committed source text; unknown text gets a deterministic English placeholder.
public struct DictionaryTranslator: IncrementalTranslator {
    public let table: [String: String]
    public let latency: LatencyModel
    public var supportsPrefix: Bool { false }
    public var supportsContext: Bool { false }

    public init(table: [String: String], latency: LatencyModel = .init(fixedMs: 120, perUnitMs: 8)) {
        self.table = table
        self.latency = latency
    }

    public static func load(_ url: URL, latency: LatencyModel = .init(fixedMs: 120, perUnitMs: 8)) throws -> DictionaryTranslator {
        DictionaryTranslator(table: try JSONDecoder().decode([String: String].self, from: Data(contentsOf: url)),
                             latency: latency)
    }

    public func translate(_ req: TranslationRequest) async throws -> TranslationCandidate {
        let key = req.uncommittedSource.trimmingCharacters(in: .whitespaces)
        let text = table[key] ?? "Segment \(req.unitID.rawValue) with \(key.count) characters."
        return TranslationCandidate(unitID: req.unitID, text: text)
    }
}

// MARK: Tone synthesizer

/// Emits a pitched tone whose length follows the English text, in 100 ms chunks.
/// Plumbing only: it preserves no speaker identity.
public struct ToneSynthesizer: VoiceSynthesizer {
    public let sampleRate: Int
    public let secondsPerWord: Double
    public let chunkSeconds: Double
    public let firstChunk: LatencyModel
    /// Generation wall time ÷ generated audio duration, for modeled streaming.
    public let realTimeFactor: Double

    public init(sampleRate: Int = 24_000, secondsPerWord: Double = 0.32, chunkSeconds: Double = 0.1,
                firstChunk: LatencyModel = .init(fixedMs: 180), realTimeFactor: Double = 0.4) {
        self.sampleRate = sampleRate
        self.secondsPerWord = secondsPerWord
        self.chunkSeconds = chunkSeconds
        self.firstChunk = firstChunk
        self.realTimeFactor = realTimeFactor
    }

    public var capabilities: SynthesisCapabilities {
        SynthesisCapabilities(audioOutputStreaming: true, incrementalTextInput: false,
                              continuationAcrossUnits: false, preservesSpeakerIdentity: false)
    }

    public func prepare(_ profile: VoiceProfile) async throws -> PreparedVoice {
        PreparedVoice(speakerID: profile.speakerID, profileVersion: profile.version, handle: 0)
    }

    public func duration(of text: String) -> Double {
        let words = max(1, text.split(whereSeparator: \.isWhitespace).count)
        return max(0.3, Double(words) * secondsPerWord)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        let total = Int(duration(of: text) * Double(sampleRate))
        let chunk = Int(chunkSeconds * Double(sampleRate))
        let format = PCMFormat(sampleRate: sampleRate, channelCount: 1)
        let rate = Double(sampleRate)
        let f0 = 140.0 + Double(unit.rawValue % 5) * 20
        return AsyncThrowingStream { continuation in
            var offset = 0
            while offset < total {
                let n = min(chunk, total - offset)
                var block = [Float](repeating: 0, count: n)
                for i in 0..<n {
                    let t = Double(offset + i) / rate
                    let envelope = min(1, min(t, Double(total) / rate - t) / 0.02)
                    block[i] = Float(0.2 * envelope * sin(2 * .pi * f0 * t))
                }
                continuation.yield(SynthesizedAudio(samples: block, format: format))
                offset += n
            }
            continuation.finish()
        }
    }
}

// MARK: Energy VAD

/// RMS threshold VAD with hangover on 20 ms frames. A placeholder for Silero VAD; its
/// events are canonical source samples.
public struct EnergyVAD: VoiceActivityDetector {
    public var thresholdDB: Float
    public var minSpeechMs: Int
    public var hangoverMs: Int
    private var inSpeech = false
    private var speechRunMs = 0
    private var silenceRunMs = 0
    private var runStart: Int64 = 0
    private var lastVoiced: Int64 = 0

    public init(thresholdDB: Float = -40, minSpeechMs: Int = 60, hangoverMs: Int = 300) {
        self.thresholdDB = thresholdDB
        self.minSpeechMs = minSpeechMs
        self.hangoverMs = hangoverMs
    }

    /// End of the last voiced frame while in speech, nil otherwise. The speech end this run will
    /// report is this sample or a later one.
    public var voicedEnd: Int64? { inSpeech ? lastVoiced : nil }

    public mutating func process(_ frame: AudioFrame16k) -> [VADEvent] {
        guard !frame.samples.isEmpty else { return [] }
        var acc: Float = 0
        for v in frame.samples { acc += v * v }
        let rms = (acc / Float(frame.samples.count)).squareRoot()
        let db = rms > 0 ? 20 * log10(rms) : -120
        let frameMs = Int(frame.samples.count * 1000 / 16_000)
        var events: [VADEvent] = []
        if db >= thresholdDB {
            if speechRunMs == 0 { runStart = frame.span.start }
            speechRunMs += frameMs
            silenceRunMs = 0
            lastVoiced = frame.span.end
            if !inSpeech, speechRunMs >= minSpeechMs {
                inSpeech = true
                events.append(.speechStart(runStart))
            }
        } else {
            speechRunMs = 0
            if inSpeech {
                silenceRunMs += frameMs
                if silenceRunMs >= hangoverMs {
                    inSpeech = false
                    events.append(.speechEnd(lastVoiced))
                }
            }
        }
        return events
    }
}

/// Thread-safe inbox used by drivers to hand adapter results back to the reducer.
public final class Inbox<Element: Sendable>: Sendable {
    private let items = Mutex<[Element]>([])
    public init() {}
    public func post(_ e: Element) { items.withLock { $0.append(e) } }
    public func drain() -> [Element] { items.withLock { defer { $0.removeAll() }; return $0 } }
    public var isEmpty: Bool { items.withLock { $0.isEmpty } }
}

/// Reference box around a Mutex so state can be shared with tasks and callbacks.
public final class Locked<Value: Sendable>: Sendable {
    private let mutex: Mutex<Value>
    public init(_ value: Value) { mutex = Mutex(value) }
    public func withLock<R: Sendable>(_ body: (inout sending Value) throws -> sending R) rethrows -> R {
        try mutex.withLock(body)
    }
}
