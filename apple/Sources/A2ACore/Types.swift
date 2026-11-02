// Core value types shared by every stage (IMPLEMENTATION_PLAN.md section 4.1/4.2).
// Pure data: no I/O, no Apple media frameworks.

/// Canonical source sample rate. Every `SourceSpan` is expressed in this rate.
public let canonicalSampleRate: Int64 = 48_000

/// Half-open span of canonical 48 kHz source samples since the current epoch started.
public struct SourceSpan: Hashable, Sendable, CustomStringConvertible {
    public let start: Int64
    public let end: Int64

    public init(start: Int64, end: Int64) {
        precondition(end >= start, "SourceSpan end \(end) precedes start \(start)")
        self.start = start
        self.end = end
    }

    public var isEmpty: Bool { end == start }
    public var count: Int64 { end - start }
    public var durationSeconds: Double { Double(count) / Double(canonicalSampleRate) }

    public func union(_ other: SourceSpan) -> SourceSpan {
        SourceSpan(start: min(start, other.start), end: max(end, other.end))
    }

    public func contains(_ sample: Int64) -> Bool { sample >= start && sample < end }

    public var description: String { "[\(start),\(end))" }
}

public struct UnitID: Hashable, Sendable, Comparable, CustomStringConvertible {
    public let rawValue: Int
    public init(_ rawValue: Int) { self.rawValue = rawValue }
    public var description: String { "u\(rawValue)" }
    public static func < (a: UnitID, b: UnitID) -> Bool { a.rawValue < b.rawValue }
}

public struct SpeakerID: Hashable, Sendable, CustomStringConvertible {
    public let rawValue: String
    public init(_ rawValue: String) { self.rawValue = rawValue }
    public var description: String { rawValue }
}

/// Incremented on source/device discontinuity or restart.
public typealias SessionEpoch = UInt32
/// Incremented on fallback or speaker handoff.
public typealias PlayoutGeneration = UInt32

/// Mono 16 kHz frame for VAD/speaker/ASR. `span` is in canonical 48 kHz samples.
public struct AudioFrame16k: Sendable {
    public let span: SourceSpan
    public let samples: [Float]

    public init(span: SourceSpan, samples: [Float]) {
        self.span = span
        self.samples = samples
    }
}

public enum VADEvent: Hashable, Sendable {
    case speechStart(Int64)
    case speechEnd(Int64)
}

public enum SpeakerEvent: Hashable, Sendable {
    case assign(SpeakerID, SourceSpan)
    case change(SourceSpan)
}

public struct ASRToken: Hashable, Sendable {
    public let text: String
    /// Recognizer-reported timing, when the adapter provides it.
    public let span: SourceSpan?

    public init(text: String, span: SourceSpan? = nil) {
        self.text = text
        self.span = span
    }
}

public struct ASRHypothesis: Hashable, Sendable {
    /// Recognizer utterance counter; comparison state resets when it changes.
    public let utterance: Int
    public let tokens: [ASRToken]
    public let isFinal: Bool
    /// Latest canonical source sample the recognizer had accepted for this hypothesis.
    public let audioFrontier: Int64

    public init(utterance: Int, tokens: [ASRToken], isFinal: Bool, audioFrontier: Int64) {
        self.utterance = utterance
        self.tokens = tokens
        self.isFinal = isFinal
        self.audioFrontier = audioFrontier
    }

    public var text: String { tokens.map(\.text).joined() }
}

public struct CommittedPair: Hashable, Sendable {
    public let source: String
    public let target: String

    public init(source: String, target: String) {
        self.source = source
        self.target = target
    }
}

public struct TranslationRequest: Hashable, Sendable {
    public let unitID: UnitID
    /// Last N committed source/target pairs.
    public let context: [CommittedPair]
    public let glossary: [String: String]
    public let uncommittedSource: String
    /// Audio corresponding to this committed source unit, in canonical sample time.
    public let sourceSpan: SourceSpan?
    /// Prefix within this unit; empty for v0 closed-unit mode.
    public let committedTargetPrefix: String

    public init(unitID: UnitID, context: [CommittedPair], glossary: [String: String],
                uncommittedSource: String, committedTargetPrefix: String = "", sourceSpan: SourceSpan? = nil) {
        self.unitID = unitID
        self.context = context
        self.glossary = glossary
        self.uncommittedSource = uncommittedSource
        self.sourceSpan = sourceSpan
        self.committedTargetPrefix = committedTargetPrefix
    }
}

public struct TranslationCandidate: Hashable, Sendable {
    public let unitID: UnitID
    public let text: String
    /// True when the adapter reports the generation completed rather than hitting a cap.
    public let complete: Bool
    /// Timing a resident translation worker reported for this answer; nil for other adapters.
    public let workerStats: TranslationWorkerStats?

    public init(unitID: UnitID, text: String, complete: Bool = true, workerStats: TranslationWorkerStats? = nil) {
        self.unitID = unitID
        self.text = text
        self.complete = complete
        self.workerStats = workerStats
    }
}

/// What a streaming speech translator spent on one span, numbers only (`mt.result` `worker_*`).
public struct TranslationWorkerStats: Hashable, Sendable {
    /// Worker time after the span closed: what the answer still cost once its audio was complete.
    public let afterCloseMs: Int
    /// Conversation turns the span was fed as.
    public let turns: Int
    /// Unanswered speech went through whole-segment translation at the close.
    public let sentenceFallback: Bool

    public init(afterCloseMs: Int, turns: Int, sentenceFallback: Bool) {
        self.afterCloseMs = afterCloseMs
        self.turns = turns
        self.sentenceFallback = sentenceFallback
    }
}

public struct ReferenceQuality: Hashable, Sendable {
    public let durationSeconds: Double
    public let snrEstimateDB: Double
    public let clippedFraction: Double
    public let overlapSuspected: Bool

    public init(durationSeconds: Double, snrEstimateDB: Double, clippedFraction: Double, overlapSuspected: Bool) {
        self.durationSeconds = durationSeconds
        self.snrEstimateDB = snrEstimateDB
        self.clippedFraction = clippedFraction
        self.overlapSuspected = overlapSuspected
    }
}

public struct VoiceProfile: Sendable {
    public let speakerID: SpeakerID
    public let version: Int
    /// 3–10 s voiced, resampled from the 48 kHz capture branch to the model's native rate.
    public let referencePCM: [Float]
    /// From our own ASR of the reference clip.
    public let referenceText: String
    public let sampleRate: Int
    public let channelCount: Int
    public let quality: ReferenceQuality
    /// Median voiced pitch from the speaker tracker, when known. Plumbing voices are cast by it.
    public let pitchHz: Double?

    public init(speakerID: SpeakerID, version: Int, referencePCM: [Float], referenceText: String,
                sampleRate: Int, channelCount: Int, quality: ReferenceQuality, pitchHz: Double? = nil) {
        self.speakerID = speakerID
        self.version = version
        self.referencePCM = referencePCM
        self.referenceText = referenceText
        self.sampleRate = sampleRate
        self.channelCount = channelCount
        self.quality = quality
        self.pitchHz = pitchHz
    }
}

/// Opaque per-adapter conditioning prepared once per profile version.
public struct PreparedVoice: Sendable {
    public let speakerID: SpeakerID
    public let profileVersion: Int
    public let handle: Int

    public init(speakerID: SpeakerID, profileVersion: Int, handle: Int) {
        self.speakerID = speakerID
        self.profileVersion = profileVersion
        self.handle = handle
    }
}

public struct PCMFormat: Hashable, Sendable {
    public let sampleRate: Int
    public let channelCount: Int

    public init(sampleRate: Int, channelCount: Int) {
        self.sampleRate = sampleRate
        self.channelCount = channelCount
    }
}

/// Identity of a synthesized chunk. The host admits it only through `PlayoutGate`.
public struct PCMChunkHeader: Hashable, Sendable {
    public let unitID: UnitID
    public let speakerID: SpeakerID
    public let profileVersion: Int
    public let sessionEpoch: SessionEpoch
    public let playoutGeneration: PlayoutGeneration
    public let format: PCMFormat
    /// Source span this chunk translates.
    public let span: SourceSpan
    public let isLast: Bool

    public init(unitID: UnitID, speakerID: SpeakerID, profileVersion: Int, sessionEpoch: SessionEpoch,
                playoutGeneration: PlayoutGeneration, format: PCMFormat, span: SourceSpan, isLast: Bool) {
        self.unitID = unitID
        self.speakerID = speakerID
        self.profileVersion = profileVersion
        self.sessionEpoch = sessionEpoch
        self.playoutGeneration = playoutGeneration
        self.format = format
        self.span = span
        self.isLast = isLast
    }
}

