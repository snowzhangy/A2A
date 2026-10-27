// Commit policy v0 (section 4.3): closed source units, translated once.
//
// 1. Source stability: two consecutive hypotheses agree (LocalAgreement-2) after
//    normalization; only material beyond a monotonic frontier is committed.
// 2. A unit closes at a clause boundary or at a finalized utterance. A VAD end only
//    requests finalization. The reconsideration window never forces an unsafe split.
// 3. Unfinished numbers, negation, and dangling function words wait for context.

public struct CommitPolicyConfig: Sendable {
    /// Stable-but-uncommitted material older than this asks for weaker boundary evidence.
    public var reconsiderWindowNs: UInt64 = 900_000_000
    /// A comma closes a unit only once it carries at least this many keys.
    public var minCommaUnitKeys = 4
    /// Safety cap: an unbounded clause is split at the best available position.
    public var maxUnitKeys = 60
    /// Space-delimited source languages should split at a word before the length cap.
    public var preferWordBoundaries = false
    /// Live mode may use stable conjunctions and timed pauses before sentence finality.
    public var streamingClauses = false
    /// Silence between consecutive timed tokens that closes a clause (180 ms of source).
    public var minClausePauseSamples: Int64 = 8_640

    public init() {}
}

public enum CommitReason: String, Sendable {
    case sentenceEnd = "sentence_end"
    case clauseComma = "clause_comma"
    case finalized
    case conjunction
    case forcedLength = "forced_length"
    case prosodicPause = "prosodic_pause"
}

public struct SourceUnit: Hashable, Sendable {
    public let id: UnitID
    public let utterance: Int
    public let span: SourceSpan
    public let text: String
    public let reason: CommitReason
    /// True when the recognizer gave no token timing and the span is the conservative bound.
    public let timingEstimated: Bool
    /// Time between the unit's first stable key and its commit.
    public let semanticWaitNs: UInt64
}

public enum HoldReason: String, Sendable {
    case negation
    case number
    case dangling
    /// Stable text without any acceptable clause boundary yet.
    case noBoundary = "no_boundary"

    init(_ ending: UnsafeEnding?) {
        switch ending {
        case .negation: self = .negation
        case .number: self = .number
        case .dangling: self = .dangling
        case nil: self = .noBoundary
        }
    }
}

public enum CommitOutput: Hashable, Sendable {
    case commit(SourceUnit)
    /// VAD ended: the recognizer should finalize; its last partial is not final.
    case requestFinalize(utterance: Int)
    /// Stable material is waiting for context beyond the reconsideration window.
    case hold(utterance: Int, reason: HoldReason, pendingKeys: Int, waitedNs: UInt64)
    /// The recognizer revised text that was already committed (irreversible).
    case revisionBehindFrontier(utterance: Int, committedKeys: Int)
    /// An utterance ended without a final result; this stable-or-tentative text was never committed.
    case abandoned(utterance: Int, text: String)
}

public struct CommitPolicy: Sendable {
    public let config: CommitPolicyConfig
    private var nextUnit: Int
    private var utterance: Int?
    private var previousKeys: [String]?
    private var committedKeys: [String] = []
    private var keyStableAt: [UInt64] = []
    private var lastText: NormalizedText?
    private var speechOnset: Int64 = 0
    /// End of the previous unit's span in this utterance (start bound for estimated spans).
    private var lastUnitEnd: Int64?
    private var lastHold: HoldReason?
    private var revisionReported = false

    public init(config: CommitPolicyConfig = .init(), firstUnit: Int = 1) {
        self.config = config
        self.nextUnit = firstUnit
    }

    /// Committed keys of the current utterance (the frontier).
    public var frontier: Int { committedKeys.count }

    /// Keys of the latest hypothesis beyond the frontier (tentative or stable).
    public var uncommittedKeys: Int { max(0, (lastText?.keys.count ?? 0) - committedKeys.count) }

    /// Earliest source sample not yet covered by a committed unit in this utterance.
    public var uncommittedStart: Int64 { max(speechOnset, lastUnitEnd ?? speechOnset) }

    public mutating func speechStarted(at sample: Int64) {
        speechOnset = sample
    }

    public mutating func speechEnded(at sample: Int64) -> [CommitOutput] {
        guard let u = utterance else { return [] }
        return [.requestFinalize(utterance: u)]
    }

    /// Epoch boundary: drop comparison state; uncommitted text is reported as abandoned.
    public mutating func reset() -> [CommitOutput] {
        var out: [CommitOutput] = []
        if let u = utterance, let text = lastText, committedKeys.count < text.keys.count {
            out.append(.abandoned(utterance: u, text: text.rawText(keys: committedKeys.count..<text.keys.count)))
        }
        utterance = nil
        previousKeys = nil
        committedKeys = []
        keyStableAt = []
        lastText = nil
        lastHold = nil
        lastUnitEnd = nil
        revisionReported = false
        return out
    }

    public mutating func process(_ h: ASRHypothesis, at tNs: UInt64) -> [CommitOutput] {
        var out: [CommitOutput] = []
        if utterance != h.utterance {
            if utterance != nil { out += reset() }
            utterance = h.utterance
            // Music or continuous background audio may keep VAD open across many
            // recognizer utterances. Timed tokens establish the new utterance's real
            // onset; retaining the first VAD onset would age all later speech forever.
            if let start = h.tokens.compactMap(\.span).map(\.start).min() {
                speechOnset = start
            }
        }
        let text = NormalizedText(h.text)
        lastText = text
        let keys = text.keys
        let f = committedKeys.count

        // The committed prefix must survive; otherwise report once and continue past it.
        if keys.count < f || Array(keys[0..<f]) != committedKeys {
            if !revisionReported {
                out.append(.revisionBehindFrontier(utterance: h.utterance, committedKeys: f))
                revisionReported = true
            }
        }

        let stable: Int
        if h.isFinal {
            stable = keys.count
        } else if let prev = previousKeys {
            var n = f
            while n < keys.count, n < prev.count, keys[n] == prev[n] { n += 1 }
            stable = n
        } else {
            stable = f
        }
        previousKeys = keys

        if keyStableAt.count > stable { keyStableAt.removeLast(keyStableAt.count - stable) }
        while keyStableAt.count < stable { keyStableAt.append(tNs) }

        out += closeUnits(text: text, hypothesis: h, stable: stable, at: tNs)
        return out
    }

    private mutating func closeUnits(text: NormalizedText, hypothesis h: ASRHypothesis,
                                     stable: Int, at tNs: UInt64) -> [CommitOutput] {
        var out: [CommitOutput] = []
        let keys = text.keys

        while committedKeys.count < stable {
            let f = committedKeys.count
            let pendingKeys = stable - f
            if let end = boundary(in: text, from: f, to: stable) {
                out.append(.commit(makeUnit(text: text, hypothesis: h, keys: f..<end.index,
                                            reason: end.reason, at: tNs)))
                continue
            }
            if h.isFinal {
                if keys[f..<stable].allSatisfy(CommitLexicon.punctuation.contains) {
                    committedKeys = Array(keys[0..<stable])
                    break
                }
                out.append(.commit(makeUnit(text: text, hypothesis: h, keys: f..<stable,
                                            reason: .finalized, at: tNs)))
                continue
            }
            if config.streamingClauses {
                if let split = timedPauseSplit(text: text, hypothesis: h, from: f, to: stable) {
                    out.append(.commit(makeUnit(text: text, hypothesis: h, keys: f..<split,
                                                reason: .prosodicPause, at: tNs)))
                    continue
                }
                // A confirmed connective with a stable suffix is stronger evidence than a timer.
                if stable - f >= 8, let split = conjunctionSplit(in: text, from: f, to: stable),
                   stable - split >= 3 {
                    out.append(.commit(makeUnit(text: text, hypothesis: h, keys: f..<split,
                                                reason: .conjunction, at: tNs)))
                    continue
                }
            }
            if pendingKeys >= config.maxUnitKeys {
                let end = forcedSplit(in: text, from: f, to: stable)
                out.append(.commit(makeUnit(text: text, hypothesis: h, keys: f..<end,
                                            reason: .forcedLength, at: tNs)))
                continue
            }
            let waited = tNs &- keyStableAt[f]
            if waited >= config.reconsiderWindowNs {
                if let split = conjunctionSplit(in: text, from: f, to: stable) {
                    out.append(.commit(makeUnit(text: text, hypothesis: h, keys: f..<split,
                                                reason: .conjunction, at: tNs)))
                    continue
                }
                let reason = HoldReason(unsafeEnding(text.rawText(keys: f..<stable)))
                if reason != lastHold {
                    out.append(.hold(utterance: h.utterance, reason: reason, pendingKeys: pendingKeys, waitedNs: waited))
                    lastHold = reason
                }
            }
            break
        }
        return out
    }

    /// Earliest acceptable clause boundary in `(from, to]`.
    private func boundary(in text: NormalizedText, from: Int, to: Int) -> (index: Int, reason: CommitReason)? {
        var i = from
        while i < to {
            let k = text.keys[i]
            let end = i + 1
            if CommitLexicon.sentenceEnd.contains(k) {
                // Merge punctuation-only fragments into the next clause, and never split
                // a decimal number ("3.5"), including one whose fraction is not stable yet.
                if hasContent(text, from, end), !isDecimalPoint(text, at: i, stableEnd: to) {
                    return (end, .sentenceEnd)
                }
            } else if CommitLexicon.clauseComma.contains(k) {
                let candidate = text.rawText(keys: from..<end)
                if contentKeys(text, from, end) >= config.minCommaUnitKeys, unsafeEnding(candidate) == nil {
                    return (end, .clauseComma)
                }
            }
            i += 1
        }
        return nil
    }

    private func conjunctionSplit(in text: NormalizedText, from: Int, to: Int) -> Int? {
        let keys = text.keys
        var p = to - 1
        while p > from {
            let rest = keys[p..<to].joined()
            if CommitLexicon.clauseConjunctions.contains(where: { rest.hasPrefix($0) }),
               contentKeys(text, from, p) >= config.minCommaUnitKeys,
               unsafeEnding(text.rawText(keys: from..<p)) == nil {
                return p
            }
            p -= 1
        }
        return nil
    }

    /// First silence between timed tokens that closes a safe clause in `(from, to)`, leaving
    /// at least two stable keys after it. Recognizers must report gaps between tokens: Apple
    /// SpeechTranscriber partials carry one range for the whole text, and its final token
    /// ranges are contiguous, so this never splits Apple results.
    private func timedPauseSplit(text: NormalizedText, hypothesis h: ASRHypothesis, from: Int, to: Int) -> Int? {
        var key = 0
        for (token, next) in zip(h.tokens, h.tokens.dropFirst()) {
            key += NormalizedText(token.text).keys.count
            if key + 2 > to { break }
            guard key > from, let previous = token.span, let following = next.span,
                  following.start - previous.end >= config.minClausePauseSamples,
                  contentKeys(text, from, key) >= config.minCommaUnitKeys,
                  unsafeEnding(text.rawText(keys: from..<key)) == nil else { continue }
            return key
        }
        return nil
    }

    private func forcedSplit(in text: NormalizedText, from: Int, to: Int) -> Int {
        let limit = min(to, from + config.maxUnitKeys)
        func wordPart(_ key: String) -> Bool {
            key.first.map { $0.isLetter || $0.isNumber || "'’-".contains($0) } == true
        }
        var p = limit
        while p > from + config.minCommaUnitKeys {
            if config.preferWordBoundaries, p < text.keys.count,
               text.rawIndex[p] == text.rawIndex[p - 1] + 1,
               wordPart(text.keys[p - 1]), wordPart(text.keys[p]) {
                p -= 1
                continue
            }
            if unsafeEnding(text.rawText(keys: from..<p)) == nil { return p }
            p -= 1
        }
        return limit
    }

    private func isDecimalPoint(_ text: NormalizedText, at i: Int, stableEnd: Int) -> Bool {
        let raw = text.raw[text.rawIndex[i]]
        guard raw == "." || raw == "．", i > 0, text.keys[i - 1].first?.isNumber == true else { return false }
        guard i + 1 < stableEnd else { return true }
        return text.keys[i + 1].first?.isNumber == true
    }

    private func hasContent(_ text: NormalizedText, _ from: Int, _ to: Int) -> Bool {
        contentKeys(text, from, to) > 0
    }

    private func contentKeys(_ text: NormalizedText, _ from: Int, _ to: Int) -> Int {
        text.keys[from..<to].filter { !CommitLexicon.punctuation.contains($0) }.count
    }

    private mutating func makeUnit(text: NormalizedText, hypothesis h: ASRHypothesis, keys range: Range<Int>,
                                   reason: CommitReason, at tNs: UInt64) -> SourceUnit {
        let (span, estimated) = spanFor(text: text, hypothesis: h, keys: range)
        let unit = SourceUnit(
            id: UnitID(nextUnit), utterance: h.utterance, span: span,
            text: text.rawText(keys: range), reason: reason, timingEstimated: estimated,
            semanticWaitNs: tNs &- keyStableAt[range.lowerBound])
        nextUnit += 1
        committedKeys = Array(text.keys[0..<range.upperBound])
        lastHold = nil
        lastUnitEnd = span.end
        return unit
    }

    /// Token timing when every covered token has it. Otherwise the estimate runs from the
    /// later of speech onset and the previous unit's end to the latest accepted frame.
    /// The previous-end bound keeps estimated spans disjoint for coverage and source-age
    /// accounting; gate latency always uses the independent fixture alignment instead.
    private func spanFor(text: NormalizedText, hypothesis h: ASRHypothesis, keys range: Range<Int>) -> (SourceSpan, Bool) {
        let start = max(speechOnset, lastUnitEnd ?? speechOnset)
        let estimate = SourceSpan(start: start, end: max(start, h.audioFrontier))
        var charSpans: [SourceSpan?] = []
        charSpans.reserveCapacity(text.raw.count)
        for token in h.tokens {
            for _ in token.text { charSpans.append(token.span) }
        }
        var merged: SourceSpan?
        for k in range {
            let ri = text.rawIndex[k]
            guard ri < charSpans.count, let s = charSpans[ri] else { return (estimate, true) }
            merged = merged.map { $0.union(s) } ?? s
        }
        return (merged ?? estimate, merged == nil)
    }
}
