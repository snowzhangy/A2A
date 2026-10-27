// Reject recognizer hallucinations whose time ranges contain no detected speech.
// The history is bounded in source time; delayed finals can still match a completed VAD span.
public struct SpeechEvidence: Sendable {
    private var spans: [SourceSpan] = []
    private var activeStart: Int64?
    private var frontier: Int64 = 0
    private let historySamples: Int64

    public init(historySeconds: Int64 = 60) {
        historySamples = max(1, historySeconds) * canonicalSampleRate
    }

    public mutating func advance(to sample: Int64) {
        frontier = max(frontier, sample)
        spans.removeAll { $0.end < frontier - historySamples }
    }

    public mutating func process(_ event: VADEvent) {
        switch event {
        case .speechStart(let sample): activeStart = sample
        case .speechEnd(let sample):
            if let start = activeStart, sample > start { spans.append(SourceSpan(start: start, end: sample)) }
            activeStart = nil
        }
    }

    public func accepts(_ hypothesis: ASRHypothesis) -> Bool {
        let timed = hypothesis.tokens.compactMap(\.span).filter { !$0.isEmpty }
        // Untimed adapters cannot establish overlap; require recent speech instead.
        if timed.isEmpty {
            return activeStart != nil || spans.last.map { hypothesis.audioFrontier - $0.end <= canonicalSampleRate } == true
        }
        var evidence = spans
        if let start = activeStart, frontier > start { evidence.append(SourceSpan(start: start, end: frontier)) }
        return timed.contains { token in
            evidence.contains { $0.start < token.end && token.start < $0.end }
        }
    }
}
