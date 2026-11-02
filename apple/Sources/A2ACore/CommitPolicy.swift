// Commit policy v0 (section 4.3): closed source units, translated once.
//
// 1. Source stability: two consecutive hypotheses agree (LocalAgreement-2) after
//    normalization; only material beyond a monotonic frontier is committed.
// 2. A unit closes at a clause boundary or at a finalized utterance. A VAD end only
//    requests finalization. The reconsideration window never forces an unsafe split.
// 3. Unfinished numbers, negation, and dangling function words wait for context.
// 4. An un-finalized unit that already covers `maxUnitSourceSeconds` of speech closes at its
//    last safe boundary rather than holding the whole utterance (M4).

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
    /// A unit that covers this much source speech closes at its last safe boundary without
    /// waiting for finalization. Key counts mean different speech durations across languages,
    /// so the budget is measured in source seconds. Zero disables the rule.
    public var maxUnitSourceSeconds: Double = 4
    /// Stable keys a length-forced cut leaves behind, so the rest of the utterance stays
    /// splittable and a cut never strands one or two characters.
    public var minTailKeys = 3
    /// Keys the clause must already carry before the seconds budget may cut it. A recognizer
    /// that keeps VAD open over music advances its audio frontier without new text, and the
    /// floor keeps that from cutting a short clause nobody meant to end.
    public var lengthClauseMinKeys = 12
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
    case lengthClause = "length_clause"
    case prosodicPause = "prosodic_pause"
    case audioSegment = "audio_segment"
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

/// Decides whether a cut falls inside a word.
///
/// Keys are not words. The recognizers emit one key per Han character while Mandarin
/// words run one to four characters, so no rule that inspects keys or spaces can see a
/// cut like `…讨 | 论…` — which is what the length budget did to an idiom and to a proper
/// noun in the real-clip replay of reports/week2/length-clause-commits.md. A2ACore stays
/// model-free, so a host with an on-device segmenter injects one (see
/// `NaturalLanguageWordSegmenter`).
public protocol WordSegmenter: Sendable {
    /// True when the character offset `offset` in `text` lies strictly inside a word.
    func splitsWord(in text: String, at offset: Int) -> Bool
}

public struct CommitPolicy: Sendable {
    public let config: CommitPolicyConfig
    /// Nil keeps the Latin-only rule. Han hosts inject a segmenter: see `WordSegmenter`.
    private let wordSegmenter: (any WordSegmenter)?
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

    public init(config: CommitPolicyConfig = .init(), firstUnit: Int = 1,
                wordSegmenter: (any WordSegmenter)? = nil) {
        self.config = config
        self.wordSegmenter = wordSegmenter
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

        // The committed prefix must survive; otherwise report once and try to re-align.
        if keys.count < committedKeys.count || Array(keys[0..<committedKeys.count]) != committedKeys {
            if !revisionReported {
                out.append(.revisionBehindFrontier(utterance: h.utterance, committedKeys: committedKeys.count))
                revisionReported = true
            }
            // The usual revision is punctuation the recognizer adds late, which inserts keys
            // into text that was already spoken. Without re-alignment the frontier index points
            // into the inserted keys and the next unit speaks the words after it a second time.
            if let aligned = reSync(committedKeys, with: keys) {
                committedKeys = Array(keys[0..<aligned])
                while keyStableAt.count > aligned { keyStableAt.removeLast(keyStableAt.count - aligned) }
                while keyStableAt.count < aligned { keyStableAt.append(tNs) }
            }
        }
        let f = committedKeys.count

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
                   stable - split >= config.minTailKeys {
                    out.append(.commit(makeUnit(text: text, hypothesis: h, keys: f..<split,
                                                reason: .conjunction, at: tNs)))
                    continue
                }
            }
            // Meetings produce recognizer utterances of 10-14 s (measured: 24 of 29 commits in
            // reports/week2/dub-anything-review.md waited for finalization, longest unit 14.1 s).
            // Once a stable unit exceeds its budget, holding it costs a whole utterance of lag
            // and projects English 1.7x its own length into the debt, so close it at the latest
            // safe position instead.
            if pendingKeys >= config.lengthClauseMinKeys,
               let seconds = pendingSourceSeconds(text: text, hypothesis: h, stable: stable),
               seconds >= config.maxUnitSourceSeconds,
               let split = lateBoundary(in: text, from: f, to: stable) {
                out.append(.commit(makeUnit(text: text, hypothesis: h, keys: f..<split,
                                            reason: .lengthClause, at: tNs)))
                continue
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

    /// Index just past the committed keys in a revised hypothesis, skipping keys the recognizer
    /// inserted (late punctuation). Nil when the change is in spoken content, where no alignment
    /// is safe and the frontier must stay where it is.
    private func reSync(_ committed: [String], with keys: [String]) -> Int? {
        var i = 0
        for k in committed {
            while i < keys.count, keys[i] != k {
                guard CommitLexicon.punctuation.contains(keys[i]) else { return nil }
                i += 1
            }
            guard i < keys.count else { return nil }
            i += 1
        }
        return i
    }

    /// Source speech the pending unit covers, from where uncovered source begins to the newest
    /// stable key. That key's own token timing is preferred when the recognizer reports one: an
    /// utterance held open over music advances the audio frontier without any speech.
    private func pendingSourceSeconds(text: NormalizedText, hypothesis h: ASRHypothesis,
                                      stable: Int) -> Double? {
        guard config.maxUnitSourceSeconds > 0, stable > 0 else { return nil }
        let start = uncommittedStart
        let spans = charSpans(for: h)
        let raw = text.rawIndex[stable - 1]
        let end = raw < spans.count ? spans[raw]?.end ?? h.audioFrontier : h.audioFrontier
        guard end > start else { return nil }
        return Double(end - start) / Double(canonicalSampleRate)
    }

    /// Recognizer timing for every raw character of the hypothesis (nil where a token has none).
    private func charSpans(for h: ASRHypothesis) -> [SourceSpan?] {
        var spans: [SourceSpan?] = []
        spans.reserveCapacity(h.tokens.reduce(0) { $0 + $1.text.count })
        for token in h.tokens { for _ in token.text { spans.append(token.span) } }
        return spans
    }

    /// Latest boundary in `(from, to)` that leaves a splittable tail and a safe ending.
    /// Cutting as late as possible keeps the delivered clause as natural as a forced split
    /// can be; the cut happens only because the unit is already over budget.
    private func lateBoundary(in text: NormalizedText, from: Int, to: Int) -> Int? {
        var p = to - config.minTailKeys
        while p - from >= config.minCommaUnitKeys {
            if unsafeEnding(text.rawText(keys: from..<p)) == nil, !splitsAnyWord(text, at: p) {
                return p
            }
            p -= 1
        }
        return nil
    }

    /// True when cutting before key `p` would split a Latin word in space-delimited text.
    private func splitsWord(_ text: NormalizedText, at p: Int) -> Bool {
        guard p > 0, p < text.keys.count, text.rawIndex[p] == text.rawIndex[p - 1] + 1 else { return false }
        func wordPart(_ key: String) -> Bool {
            key.first.map { $0.isLetter || $0.isNumber || "'’-".contains($0) } == true
        }
        return wordPart(text.keys[p - 1]) && wordPart(text.keys[p])
    }

    /// True when cutting before key `p` would split a word. The Latin rule stays behind
    /// `preferWordBoundaries` because there spaces are the only cue; an injected segmenter
    /// always applies, because per-character keys reveal nothing about Han words.
    private func splitsAnyWord(_ text: NormalizedText, at p: Int) -> Bool {
        if config.preferWordBoundaries, splitsWord(text, at: p) { return true }
        guard let wordSegmenter, p > 0, p < text.keys.count else { return false }
        return wordSegmenter.splitsWord(in: String(text.raw), at: text.rawIndex[p])
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
        var p = limit
        while p > from + config.minCommaUnitKeys {
            if splitsAnyWord(text, at: p) {
                p -= 1
                continue
            }
            if unsafeEnding(text.rawText(keys: from..<p)) == nil { return p }
            p -= 1
        }
        return limit   // nowhere safe at all: split rather than hold a unit forever
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
        let charSpans = charSpans(for: h)
        var merged: SourceSpan?
        for k in range {
            let ri = text.rawIndex[k]
            guard ri < charSpans.count, let s = charSpans[ri] else { return (estimate, true) }
            merged = merged.map { $0.union(s) } ?? s
        }
        return (merged ?? estimate, merged == nil)
    }
}
