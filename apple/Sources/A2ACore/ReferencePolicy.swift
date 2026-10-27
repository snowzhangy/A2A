// Voice reference acquisition (section 4.4, AcquiringVoice). Pure: the driver measures
// candidate segments and cuts PCM; this policy decides which finalized utterances form
// the reference and when a profile version is due.
//
// A reference-conditioned (ICL) synthesizer needs the transcript to match the audio, so
// only finalized utterances with recognizer token timing qualify, cut on their token spans.
// The first version is due as soon as enough clean speech exists; one upgraded version
// follows at the target length, then acquisition stops.

public struct ReferencePolicyConfig: Sendable {
    /// Voiced audio needed before the first profile version.
    public var minSeconds = 3.0
    /// Voiced audio for the upgraded (final) profile version.
    public var targetSeconds = 8.0
    /// Reference length cap; longer prefill slows every synthesis.
    public var maxSeconds = 10.0
    public var minSegmentSeconds = 0.3
    /// Context kept around the first and last token so onsets and decays are not clipped.
    public var paddingSamples: Int64 = 4_800
    public var minLevelDB: Float = -45
    public var maxClippedFraction = 0.002

    public init() {}
}

public struct ReferenceSegment: Hashable, Sendable {
    public let span: SourceSpan
    public let text: String

    public init(span: SourceSpan, text: String) {
        self.span = span
        self.text = text
    }
}

public struct ReferencePlan: Hashable, Sendable {
    public let speaker: SpeakerID
    public let version: Int
    public let segments: [ReferenceSegment]

    public var text: String { segments.map(\.text).joined(separator: " ") }
    public var seconds: Double { segments.reduce(0) { $0 + $1.span.durationSeconds } }
}

public enum ReferenceRejection: String, Error, Sendable {
    case noTiming = "no_timing"
    case empty
    case tooShort = "too_short"
    case tooLong = "too_long"
    case quiet
    case clipped
    case complete
}

public struct ReferencePolicy: Sendable {
    public let config: ReferencePolicyConfig
    public private(set) var speaker: SpeakerID
    private var segments: [ReferenceSegment] = []
    private var version = 0
    private var issuedSegments = 0

    public init(speaker: SpeakerID, config: ReferencePolicyConfig = .init()) {
        self.speaker = speaker
        self.config = config
    }

    /// True once the upgraded version was issued; later utterances are not needed.
    public var isComplete: Bool { version >= 2 }
    public var hasIssuedProfile: Bool { version > 0 }
    public var acceptedSeconds: Double { segments.reduce(0) { $0 + $1.span.durationSeconds } }

    /// Padded union of a final hypothesis' token spans, or nil without recognizer timing.
    public func speechSpan(of h: ASRHypothesis) -> SourceSpan? {
        guard let span = tokenSpan(of: h) else { return nil }
        return SourceSpan(start: max(0, span.start - config.paddingSamples), end: span.end + config.paddingSamples)
    }

    /// Speaker coverage is measured on speech timing, excluding synthetic padding.
    public func tokenSpan(of h: ASRHypothesis) -> SourceSpan? {
        guard h.isFinal else { return nil }
        let spans = h.tokens.compactMap(\.span).filter { !$0.isEmpty }
        guard let first = spans.map(\.start).min(), let last = spans.map(\.end).max() else { return nil }
        return SourceSpan(start: first, end: last)
    }

    /// Offer one measured, finalized utterance. Returns the plan for a new profile version
    /// when this segment makes one due.
    public mutating func offer(span: SourceSpan, text: String, levelDB: Float,
                               clippedFraction: Double) -> Result<ReferencePlan?, ReferenceRejection> {
        if isComplete { return .failure(.complete) }
        let trimmed = text.trimmingWhitespace()
        guard !trimmed.isEmpty else { return .failure(.empty) }
        guard span.durationSeconds >= config.minSegmentSeconds else { return .failure(.tooShort) }
        guard levelDB.isFinite, levelDB >= config.minLevelDB else { return .failure(.quiet) }
        guard clippedFraction <= config.maxClippedFraction else { return .failure(.clipped) }
        guard acceptedSeconds + span.durationSeconds <= config.maxSeconds else {
            // After the first version, a segment that cannot fit ends acquisition with what
            // was gathered (an upgrade only when it adds audio).
            if version == 1 {
                if segments.count > issuedSegments { return .success(issue()) }
                version = 2
            }
            return .failure(.tooLong)
        }
        segments.append(ReferenceSegment(span: span, text: trimmed))
        let total = acceptedSeconds
        if version == 0, total >= config.minSeconds { return .success(issue()) }
        if version == 1, total >= config.targetSeconds { return .success(issue()) }
        return .success(nil)
    }

    /// A new speaker (or a source discontinuity) starts acquisition over.
    public mutating func reset(speaker: SpeakerID) {
        self.speaker = speaker
        segments.removeAll()
        version = 0
        issuedSegments = 0
    }

    private mutating func issue() -> ReferencePlan {
        version += 1
        issuedSegments = segments.count
        return ReferencePlan(speaker: speaker, version: version, segments: segments)
    }
}
