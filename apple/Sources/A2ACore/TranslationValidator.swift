// Translation output checks before scheduling (section 4.3, rule 3): completion,
// language, duplication, and length cap.

public enum TranslationRejection: String, Sendable {
    case empty
    case incomplete
    case wrongLanguage = "wrong_language"
    case duplicate
    case tooLong = "too_long"
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

    public init(targetLanguage: TranslationLanguage = .english) {
        self.targetLanguage = targetLanguage
    }

    public func maxTargetCharacters(source: String) -> Int {
        max(minTargetCap, Int(Double(source.count) * maxTargetCharsPerSourceChar))
    }

    public func validate(_ candidate: TranslationCandidate, source: String,
                         previousTarget: String?) -> TranslationRejection? {
        let text = candidate.text.trimmingWhitespace()
        if text.isEmpty { return .empty }
        if !candidate.complete { return .incomplete }
        let letters = text.unicodeScalars.filter { $0.properties.isAlphabetic }
        if !letters.isEmpty {
            switch targetLanguage {
            case .english:
                let cjk = letters.filter(Self.isCJK).count
                if Double(cjk) / Double(letters.count) > maxCJKFraction { return .wrongLanguage }
            case .mandarin:
                let han = letters.filter(Self.isHan).count
                if Double(han) / Double(letters.count) < minHanFraction { return .wrongLanguage }
            }
        }
        if let previousTarget, previousTarget.trimmingWhitespace().lowercased() == text.lowercased() {
            return .duplicate
        }
        if text.count > maxTargetCharacters(source: source) { return .tooLong }
        return nil
    }

    static func isCJK(_ s: Unicode.Scalar) -> Bool {
        switch s.value {
        case 0x3400...0x4DBF, 0x4E00...0x9FFF, 0xF900...0xFAFF, 0x20000...0x2FA1F, 0x3040...0x30FF, 0xAC00...0xD7AF:
            true
        default:
            false
        }
    }

    private static func isHan(_ s: Unicode.Scalar) -> Bool {
        switch s.value {
        case 0x3400...0x4DBF, 0x4E00...0x9FFF, 0xF900...0xFAFF, 0x20000...0x2FA1F: true
        default: false
        }
    }
}
