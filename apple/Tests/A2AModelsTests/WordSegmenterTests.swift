// The system word segmenter is an OS text-analysis service, not a registered model, so it
// has no manifest row and no download step. Its language model can be missing on a given
// machine; the suite then skips rather than failing (reports/week2/length-clause-commits.md).

import A2ACore
import A2AModels
import Testing

@Test(.enabled(if: NaturalLanguageWordSegmenter.available(language: "zh-Hans"),
               "the system Chinese word segmenter is not installed"))
func systemSegmenterKeepsHanWordsTogether() {
    let segmenter = NaturalLanguageWordSegmenter(language: "zh-Hans")
    // 我0 们1 讨2 论3 这4 个5 问6 题7: the system segments 讨论 as one word, so a cut at
    // offset 3 is inside it and a cut at 4 is a boundary. Before a segmenter was consulted,
    // the length budget made cuts of the first kind on real partials.
    #expect(segmenter.splitsWord(in: "我们讨论这个问题", at: 3))
    #expect(!segmenter.splitsWord(in: "我们讨论这个问题", at: 4))
    #expect(!segmenter.splitsWord(in: "", at: 0))
    #expect(!segmenter.splitsWord(in: "我们讨论这个问题", at: 8))
}

@Test func segmenterReportsUnavailableLanguageInsteadOfGuessing() {
    // No text means no tokens, which `tokens(in:)` reports as nil so callers can fall back
    // deliberately instead of reading "nothing found" as "safe to cut anywhere".
    #expect(NaturalLanguageWordSegmenter(language: "zh-Hans").tokens(in: "") == nil)
    // An unknown code is not reported as unavailable: the system falls back to its own
    // language detection, which is a guess the policy should not have to second-guess.
    // Hosts therefore pass the direction's own identifier, never a guess.
    #expect(NaturalLanguageWordSegmenter.available(language: "zh-Hans"))
}
