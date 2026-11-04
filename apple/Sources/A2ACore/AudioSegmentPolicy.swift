/// Bounded speech spans for a direct audio translator. A span closes at the detector's speech end,
/// which is a pause, or once it reaches `maxSamples` of continuous speech. This policy does not
/// read words; the maximum bounds latency during continuous speech.
///
/// Sentence units (ADR-0017): under a picture delay the maximum grows with the delay, so a spoken
/// sentence stays one unit, and a span that reaches it is cut at the quietest frame of its last
/// `dipWindowSamples` (a breath or a pause too short for the detector), not at a fixed sample.
public struct AudioSegmentPolicy: Sendable {
    public var maxSamples: Int64
    public var minSamples: Int64
    /// 0 cuts at exactly `maxSamples`. Otherwise the cut is the quietest frame offered through
    /// `process(level:)` within this many samples before the maximum.
    public var dipWindowSamples: Int64
    private var start: Int64?
    /// Recent frame levels (end sample, dBFS) of the open span, for the dip cut.
    private var levels: [(start: Int64, end: Int64, db: Float)] = []
    /// Spans closed at a speech end and at the maximum, for the run summary.
    public private(set) var pauseCloses = 0
    public private(set) var capCloses = 0

    public init(maxSeconds: Double = 3.0, minSeconds: Double = 0.25, dipWindowSeconds: Double = 0) {
        precondition(maxSeconds > 0 && minSeconds >= 0 && minSeconds < maxSeconds)
        precondition(dipWindowSeconds >= 0 && dipWindowSeconds < maxSeconds - minSeconds)
        maxSamples = Int64(maxSeconds * Double(canonicalSampleRate))
        minSamples = Int64(minSeconds * Double(canonicalSampleRate))
        dipWindowSamples = Int64(dipWindowSeconds * Double(canonicalSampleRate))
    }

    /// Sentence units under picture delay `d` seconds. The longest span leaves 3.5 s of the delay
    /// for the translator's last turn, the voice's first audio and a reserve. Replays needed about
    /// 2.5 s, but in the live app (screen capture, Hy and two voices on one GPU) 5 s units finished
    /// synthesis a p10 of 1 s after their slot. Never less than the 3 s live default, at most 6 s.
    public static func sentences(presentationDelaySeconds d: Double) -> AudioSegmentPolicy {
        AudioSegmentPolicy(maxSeconds: min(6, max(3, d - 3.5)), minSeconds: 0.25, dipWindowSeconds: 1)
    }

    /// Start of the span being spoken, if any; it closes at a speech end or at `maxSamples`.
    public var openStart: Int64? { start }

    /// How far into an open span its audio may be streamed before the span closes: a cut can
    /// fall anywhere in the dip window, so audio there waits for the cut.
    public var streamLimitSamples: Int64 { maxSamples - dipWindowSamples }

    /// `level` is the detector's frame (its span and dBFS) for the dip cut; frames should be in
    /// the same sample coordinates as `events`.
    public mutating func process(events: [VADEvent], frontier: Int64,
                                 level: (span: SourceSpan, db: Float)? = nil) -> [SourceSpan] {
        var result: [SourceSpan] = []
        for event in events {
            switch event {
            case .speechStart(let sample):
                if start == nil { start = sample }
            case .speechEnd(let sample):
                if let s = start, sample - s >= minSamples {
                    result.append(SourceSpan(start: s, end: sample))
                    pauseCloses += 1
                }
                start = nil
            }
        }
        if dipWindowSamples > 0, let level, let s = start, level.span.end > s {
            levels.append((level.span.start, level.span.end, level.db))
        }
        while let s = start, frontier - s >= maxSamples {
            let end = cut(from: s)
            result.append(SourceSpan(start: s, end: end))
            capCloses += 1
            start = end
        }
        if let s = start {
            if let i = levels.firstIndex(where: { $0.end > s }) { levels.removeFirst(i) } else { levels.removeAll() }
        } else {
            levels.removeAll()
        }
        return result
    }

    /// The middle of the quietest frame in the dip window (the latest on a tie), else the maximum.
    private func cut(from s: Int64) -> Int64 {
        let limit = s + maxSamples
        let window = limit - dipWindowSamples
        var best: (sample: Int64, db: Float)?
        for frame in levels where frame.start >= window && frame.end <= limit {
            if best == nil || frame.db <= best!.db { best = ((frame.start + frame.end) / 2, frame.db) }
        }
        return best?.sample ?? limit
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

/// The segmenter's speech threshold (ADR-0017): halfway between the quiet floor and the speech
/// level of the last few seconds, never below a fixed floor. A pause is where the voice returns to
/// its floor, which under a loud scene can sit above any fixed threshold: residue the isolator
/// leaves at -36 dB hid every pause from a fixed -45 dB threshold on replayed drama, and this rule
/// found 35 of 39. Without contrast (less than `minContrastDB` between floor and speech) it
/// keeps the fixed floor.
public struct AdaptiveSpeechThreshold: Sendable {
    public var floorDB: Float
    public var windowSamples: Int64
    public var minContrastDB: Float = 6
    private var frames: [(end: Int64, db: Float)] = []

    public init(floorDB: Float, windowSeconds: Double = 6) {
        precondition(windowSeconds > 0)
        self.floorDB = floorDB
        windowSamples = Int64(windowSeconds * Double(canonicalSampleRate))
    }

    /// Adds one frame's level and returns the threshold for it.
    public mutating func update(span: SourceSpan, db: Float) -> Float {
        frames.append((span.end, db.isFinite ? max(-120, db) : -120))
        if let i = frames.firstIndex(where: { $0.end > span.end - windowSamples }), i > 0 { frames.removeFirst(i) }
        let sorted = frames.map(\.db).sorted()
        let low = sorted[Int(0.1 * Double(sorted.count - 1))]
        let high = sorted[Int(0.8 * Double(sorted.count - 1))]
        guard high - low >= minContrastDB else { return floorDB }
        return max(floorDB, low + 0.5 * (high - low))
    }
}
