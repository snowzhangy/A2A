/// Bounded speech spans for a direct audio translator. This policy does not attempt to
/// infer words or sentence boundaries; the maximum bounds latency during continuous speech.
public struct AudioSegmentPolicy: Sendable {
    public var maxSamples: Int64
    public var minSamples: Int64
    private var start: Int64?

    public init(maxSeconds: Double = 3.0, minSeconds: Double = 0.25) {
        precondition(maxSeconds > 0 && minSeconds >= 0 && minSeconds < maxSeconds)
        maxSamples = Int64(maxSeconds * Double(canonicalSampleRate))
        minSamples = Int64(minSeconds * Double(canonicalSampleRate))
    }

    /// Start of the span being spoken, if any; it closes at a speech end or at `maxSamples`.
    public var openStart: Int64? { start }

    public mutating func process(events: [VADEvent], frontier: Int64) -> [SourceSpan] {
        var result: [SourceSpan] = []
        for event in events {
            switch event {
            case .speechStart(let sample):
                if start == nil { start = sample }
            case .speechEnd(let sample):
                if let s = start, sample - s >= minSamples { result.append(SourceSpan(start: s, end: sample)) }
                start = nil
            }
        }
        while let s = start, frontier - s >= maxSamples {
            let end = s + maxSamples
            result.append(SourceSpan(start: s, end: end))
            start = end
        }
        return result
    }
}

/// What a streaming audio translator may hear of a span before the span closes, so the turns it can
/// answer are processed while the speaker is still talking and only the tail waits for the commit.
///
/// Audio is offered up to the detector's last voiced sample only: a later speech end reports that
/// sample or a later one, so nothing streamed ever lies past the end of the span it belongs to. Every
/// closed span is delivered whole, in order, as appends that tile it exactly, then `close`; a span the
/// segmenter drops (too short) is withdrawn with `discard`. Feed it the policy's output each step.
public struct AudioStreamFeed: Sendable {
    public enum Action: Sendable, Equatable {
        /// Send this audio now.
        case append(SourceSpan)
        /// The span is committed and its audio complete: ask for the answer.
        case close(SourceSpan)
        /// The open span will never close: forget what was sent for it.
        case discard
    }

    public let maxSamples: Int64
    /// Smallest append while a span is open (the translator consumes 800 ms turns).
    public let minAppendSamples: Int64
    private var open: (start: Int64, sent: Int64)?

    public init(maxSamples: Int64, minAppendSeconds: Double = 0.1) {
        precondition(maxSamples > 0 && minAppendSeconds >= 0)
        self.maxSamples = maxSamples
        minAppendSamples = Int64(minAppendSeconds * Double(canonicalSampleRate))
    }

    /// `closed` and `openStart` come from `AudioSegmentPolicy.process` and `openStart` after it;
    /// `voicedEnd` is the detector's last voiced sample while it is in speech, nil otherwise.
    public mutating func update(closed: [SourceSpan], openStart: Int64?, voicedEnd: Int64?) -> [Action] {
        var actions: [Action] = []
        for span in closed {
            var from = span.start
            if let o = open {
                if o.start == span.start { from = min(o.sent, span.end) } else { actions.append(.discard) }
                open = nil
            }
            if from < span.end { actions.append(.append(SourceSpan(start: from, end: span.end))) }
            actions.append(.close(span))
        }
        guard let start = openStart else {
            if open != nil { actions.append(.discard) }
            open = nil
            return actions
        }
        if let o = open, o.start != start {
            actions.append(.discard)
            open = nil
        }
        var o = open ?? (start: start, sent: start)
        if let voicedEnd {
            let safe = min(voicedEnd, start + maxSamples)
            if safe - o.sent >= max(1, minAppendSamples) {
                actions.append(.append(SourceSpan(start: o.sent, end: safe)))
                o.sent = safe
            }
        }
        open = o
        return actions
    }
}
