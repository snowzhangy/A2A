// Consistent text normalization for hypothesis comparison (section 4.3, rule 1).
// Comparison keys drop whitespace and fold width/case; the raw text is kept so a
// committed unit reproduces exactly what the recognizer emitted.

public struct NormalizedText: Sendable {
    public let raw: [Character]
    /// One comparison key per non-whitespace raw character.
    public let keys: [String]
    /// Index into `raw` for each key.
    public let rawIndex: [Int]

    public init(_ text: String) {
        let raw = Array(text)
        var keys: [String] = []
        var rawIndex: [Int] = []
        keys.reserveCapacity(raw.count)
        rawIndex.reserveCapacity(raw.count)
        for (i, ch) in raw.enumerated() where !ch.isWhitespace {
            keys.append(Self.key(for: ch))
            rawIndex.append(i)
        }
        self.raw = raw
        self.keys = keys
        self.rawIndex = rawIndex
    }

    /// Raw text covering keys `[from, to)`, including interior whitespace.
    public func rawText(keys range: Range<Int>) -> String {
        guard !range.isEmpty else { return "" }
        let start = range.lowerBound == 0 ? 0 : rawIndex[range.lowerBound]
        let end = rawIndex[range.upperBound - 1] + 1
        return String(raw[start..<end]).trimmingWhitespace()
    }

    static func key(for ch: Character) -> String {
        switch ch {
        case "，": return ","
        case "。": return "."
        case "！": return "!"
        case "？": return "?"
        case "；": return ";"
        case "：": return ":"
        default:
            // Fold full-width ASCII forms (U+FF01–FF5E) and case.
            if let s = ch.unicodeScalars.first, ch.unicodeScalars.count == 1,
               (0xFF01...0xFF5E).contains(s.value),
               let folded = Unicode.Scalar(s.value - 0xFEE0) {
                return String(Character(folded)).lowercased()
            }
            return String(ch).lowercased()
        }
    }
}

public enum CommitLexicon {
    public static let sentenceEnd: Set<String> = [".", "!", "?", ";"]
    public static let clauseComma: Set<String> = [","]
    public static let punctuation: Set<String> = [".", "!", "?", ";", ",", ":", "、", "…", "\"", "“", "”", "'"]

    /// Negation markers that must not end a committed clause (rule 1).
    public static let negation = ["不", "没", "没有", "别", "未", "非", "不是", "并不", "不要", "not", "no", "don't", "never"]
    /// Function words that leave a phrase incomplete when they end a clause.
    public static let dangling = ["的", "和", "跟", "与", "或", "或者", "还是", "但", "但是", "而", "而且", "因为",
                                  "所以", "如果", "虽然", "在", "把", "被", "对", "给", "从", "向", "比", "让",
                                  "是", "就是", "第", "the", "a", "an", "and", "or", "to", "of"]
    /// Conjunctions that start a new clause; splitting before them is boundary evidence only.
    public static let clauseConjunctions = ["但是", "所以", "然后", "而且", "因为", "如果", "可是", "不过", "并且"]
    /// A clause ending in one of these may still be inside a number or date ("三月二十" → "二十五号").
    /// Magnitude suffixes (百千万亿) and "%" close a number, so they are not listed.
    static let numeralCharacters: Set<Character> = Set("0123456789零一二三四五六七八九十两点.")
}

extension String {
    func trimmingWhitespace() -> String {
        var s = Substring(self)
        while let f = s.first, f.isWhitespace { s.removeFirst() }
        while let l = s.last, l.isWhitespace { s.removeLast() }
        return String(s)
    }
}

/// Why a candidate clause must wait for more context.
public enum UnsafeEnding: String, Sendable {
    case negation
    case number
    case dangling
}

public func unsafeEnding(_ text: String) -> UnsafeEnding? {
    var body = Substring(text.trimmingWhitespace())
    while let last = body.last, CommitLexicon.punctuation.contains(NormalizedText.key(for: last)) {
        body.removeLast()
    }
    let s = String(body).trimmingWhitespace()
    guard let last = s.last else { return nil }
    let lowered = s.lowercased()
    let words = lowered.split(whereSeparator: \.isWhitespace)
    func endsWith(_ marker: String) -> Bool {
        if marker.allSatisfy(\.isASCII) { return words.last.map(String.init) == marker }
        return lowered.hasSuffix(marker)
    }
    if CommitLexicon.negation.contains(where: endsWith) { return .negation }
    if CommitLexicon.numeralCharacters.contains(last) { return .number }
    if CommitLexicon.dangling.contains(where: endsWith) { return .dangling }
    return nil
}
