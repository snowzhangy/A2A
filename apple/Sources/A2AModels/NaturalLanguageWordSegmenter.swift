import Foundation
import NaturalLanguage
import A2ACore

/// Apple's on-device word segmenter, used to keep commit cuts off Han words.
///
/// The recognizers emit one key per Chinese character, so a policy that only sees keys
/// cuts `讨论` in half. `NLTokenizer` segments with the system language models: no download
/// through the model registry (D8 — the OS owns these), no runtime network use, and no
/// text leaves the process. It is a text-analysis service, not a speech model, so it stays
/// out of `models/manifest.yaml`.
///
/// A missing or unloaded system model yields no tokens; `available(language:)` reports that,
/// and a host that cannot tolerate it passes `wordSegmenter: nil`, which returns the policy
/// to its previous Latin-only rule. An unrecognised language code is not reported as
/// unavailable — the tokenizer falls back to its own detection — so hosts pass the
/// direction's identifier rather than a guess.
public struct NaturalLanguageWordSegmenter: WordSegmenter, Sendable {
    private let language: String

    public init(language: String) {
        self.language = language
    }

    public func splitsWord(in text: String, at offset: Int) -> Bool {
        guard let tokens = tokens(in: text) else { return false }
        return tokens.contains { $0.0 < offset && offset < $0.1 }
    }

    /// Word ranges as character offsets, or nil when the language has no segmenter here.
    public func tokens(in text: String) -> [(Int, Int)]? {
        let tokenizer = NLTokenizer(unit: .word)
        tokenizer.setLanguage(NLLanguage(rawValue: language))
        tokenizer.string = text
        var out: [(Int, Int)] = []
        tokenizer.enumerateTokens(in: text.startIndex..<text.endIndex) { range, _ in
            out.append((text.distance(from: text.startIndex, to: range.lowerBound),
                        text.distance(from: text.startIndex, to: range.upperBound)))
            return true
        }
        return out.isEmpty ? nil : out
    }

    /// True when the system can segment this language on this machine.
    public static func available(language: String) -> Bool {
        let probe = NaturalLanguageWordSegmenter(language: language)
            .tokens(in: language == "en" ? "word boundary here" : "我们讨论这个问题")
        return (probe?.count ?? 0) > 1
    }
}
