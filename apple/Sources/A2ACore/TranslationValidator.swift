// Translation output checks before scheduling (section 4.3, rule 3): completion,
// language, duplication, and length cap — plus the two verdicts that are not failures:
// nothing to speak, and a target shortened to the time its source audio took.

public enum TranslationRejection: String, Sendable {
    case empty
    case incomplete
    case wrongLanguage = "wrong_language"
    case duplicate
    case tooLong = "too_long"
}

/// A translated unit that says nothing: the model answered music, laughter or a pause with an
/// empty string or a filler. Speaking it would add nothing and failing it would report an
/// error and trigger a fallback for audio that had no speech in it.
public enum TranslationSilence: String, Sendable {
    case empty
    case filler
}

public enum TranslationVerdict: Sendable, Equatable {
    /// Speak this text (the candidate's own text, or its shortened prefix).
    case accepted(String)
    /// Say nothing. Not a failure: no fallback, no error, no context entry.
    case silent(TranslationSilence)
    case rejected(TranslationRejection)

    public var acceptedText: String? { if case .accepted(let t) = self { t } else { nil } }
    public var rejection: TranslationRejection? { if case .rejected(let r) = self { r } else { nil } }
    public var silence: TranslationSilence? { if case .silent(let s) = self { s } else { nil } }
    public var isAccepted: Bool { acceptedText != nil }
}

public struct TranslationValidator: Sendable {
    public let targetLanguage: TranslationLanguage
    /// Fraction of CJK letters above which an English target is rejected.
    public var maxCJKFraction = 0.3
    /// A Mandarin target may include names and technical terms in Latin script.
    public var minHanFraction = 0.3
    /// Target length cap relative to source characters.
    public var maxTargetCharsPerSourceChar = 6.0
    public var minTargetCap = 40
    /// Characters per second the target is spoken at, for a source-span budget. Derived from
    /// the pipeline's own `estimatedSecondsPerTargetChar`, so debt and budget agree.
    public var spokenCharactersPerSecond = 15.0
    /// Audio time a target may run past its source span before it is shortened.
    public var spokenBudgetGraceSeconds = 1.0
    /// Cut an over-budget target at its last safe boundary instead of dropping the line.
    public var shortensOverBudgetTargets = true
    /// Treat an empty or filler-only target as silence rather than a failed translation.
    public var silencesFillers = true

    public init(targetLanguage: TranslationLanguage = .english) {
        self.targetLanguage = targetLanguage
    }

    public func maxTargetCharacters(source: String) -> Int {
        max(minTargetCap, Int(Double(source.count) * maxTargetCharsPerSourceChar))
    }

    /// Characters of target text that fit the source span's own playback time.
    /// `graceSeconds` replaces `spokenBudgetGraceSeconds` for one line, e.g. the silence known to
    /// follow it (ADR-0018).
    public func spokenBudgetCharacters(sourceDurationSeconds: Double, graceSeconds: Double? = nil) -> Int {
        let grace = max(0, graceSeconds ?? spokenBudgetGraceSeconds)
        return max(2, Int((max(0, sourceDurationSeconds) + grace) * spokenCharactersPerSecond))
    }

    public func evaluate(_ candidate: TranslationCandidate, source: String, previousTarget: String?,
                         sourceDurationSeconds: Double? = nil,
                         wordSegmenter: (any WordSegmenter)? = nil,
                         graceSeconds: Double? = nil) -> TranslationVerdict {
        let text = candidate.text.trimmingWhitespace()
        if text.isEmpty { return silencesFillers ? .silent(.empty) : .rejected(.empty) }
        if !candidate.complete { return .rejected(.incomplete) }
        let letters = text.unicodeScalars.filter { $0.properties.isAlphabetic }
        if !letters.isEmpty {
            switch targetLanguage {
            case .english:
                let cjk = letters.filter(Self.isCJK).count
                if Double(cjk) / Double(letters.count) > maxCJKFraction { return .rejected(.wrongLanguage) }
            case .mandarin:
                let han = letters.filter(Self.isHanScalar).count
                if Double(han) / Double(letters.count) < minHanFraction { return .rejected(.wrongLanguage) }
            }
        }
        if silencesFillers, FillerLexicon.isFillerOnly(text, language: targetLanguage) {
            return .silent(.filler)
        }
        if let previousTarget, previousTarget.trimmingWhitespace().lowercased() == text.lowercased() {
            return .rejected(.duplicate)
        }
        if let sourceDurationSeconds, shortensOverBudgetTargets {
            let budget = spokenBudgetCharacters(sourceDurationSeconds: sourceDurationSeconds, graceSeconds: graceSeconds)
            if text.count > budget,
               let shortened = TargetShortener.shorten(text, to: budget, wordSegmenter: wordSegmenter) {
                return .accepted(shortened)
            }
        }
        let cap = sourceDurationSeconds.map { max(minTargetCap, Int($0 * 35)) } ?? maxTargetCharacters(source: source)
        if text.count > cap { return .rejected(.tooLong) }
        return .accepted(text)
    }

    static func isCJK(_ s: Unicode.Scalar) -> Bool {
        switch s.value {
        case 0x3400...0x4DBF, 0x4E00...0x9FFF, 0xF900...0xFAFF, 0x20000...0x2FA1F, 0x3040...0x30FF, 0xAC00...0xD7AF:
            true
        default:
            false
        }
    }

    public static func isHanScalar(_ s: Unicode.Scalar) -> Bool {
        switch s.value {
        case 0x3400...0x4DBF, 0x4E00...0x9FFF, 0xF900...0xFAFF, 0x20000...0x2FA1F: true
        default: false
        }
    }
}
