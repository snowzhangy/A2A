// Literal term replacement for glossaries (source spellings → the agreed target term,
// before translation) and pronunciations (target word → a respelling the voice says
// correctly, before synthesis only; captions keep the real spelling).
//
// Longest terms win and replaced text is never re-scanned. A term edge made of ASCII
// letters or digits only matches at a word boundary, so "Li" never matches inside
// "Lily"; CJK terms match anywhere, because Chinese has no spaces between words.

public struct TermMap: Hashable, Sendable {
    private struct Entry: Hashable, Sendable {
        let term: [Character]
        let replacement: String
    }

    private let entries: [Entry]
    public let ignoresCase: Bool

    public init(_ terms: [String: String], ignoresCase: Bool = false) {
        self.ignoresCase = ignoresCase
        entries = terms.compactMap { term, replacement -> Entry? in
            let key = term.trimmingWhitespace()
            return key.isEmpty ? nil : Entry(term: Array(key), replacement: replacement.trimmingWhitespace())
        }.sorted { a, b in
            a.term.count != b.term.count ? a.term.count > b.term.count : String(a.term) < String(b.term)
        }
    }

    public var isEmpty: Bool { entries.isEmpty }
    public var count: Int { entries.count }

    public func apply(_ text: String) -> String {
        guard !entries.isEmpty else { return text }
        let chars = Array(text)
        var out = ""
        var i = 0
        scan: while i < chars.count {
            for entry in entries where matches(entry.term, in: chars, at: i) {
                out += entry.replacement
                i += entry.term.count
                continue scan
            }
            out.append(chars[i])
            i += 1
        }
        return out
    }

    private func matches(_ term: [Character], in chars: [Character], at start: Int) -> Bool {
        let end = start + term.count
        guard end <= chars.count else { return false }
        for k in 0..<term.count where !same(term[k], chars[start + k]) { return false }
        if Self.isWordCharacter(term[0]), start > 0, Self.isWordCharacter(chars[start - 1]) { return false }
        if Self.isWordCharacter(term[term.count - 1]), end < chars.count, Self.isWordCharacter(chars[end]) { return false }
        return true
    }

    private func same(_ a: Character, _ b: Character) -> Bool {
        a == b || (ignoresCase && a.lowercased() == b.lowercased())
    }

    private static func isWordCharacter(_ c: Character) -> Bool { c.isASCII && (c.isLetter || c.isNumber) }
}
