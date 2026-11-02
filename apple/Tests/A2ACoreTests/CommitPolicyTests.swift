import A2ACore
import Testing

private let ms: UInt64 = 1_000_000

private func hyp(_ text: String, utt: Int = 1, final: Bool = false, frontier: Int64 = 48_000) -> ASRHypothesis {
    ASRHypothesis(utterance: utt, tokens: text.map { ASRToken(text: String($0)) }, isFinal: final,
                  audioFrontier: frontier)
}

/// Spoken content only: recognizer punctuation carries no audio.
private func contentOnly(_ text: String) -> String {
    NormalizedText(text).keys.filter { !CommitLexicon.punctuation.contains($0) }.joined()
}

private func commits(_ outputs: [CommitOutput]) -> [SourceUnit] {
    outputs.compactMap { if case .commit(let u) = $0 { u } else { nil } }
}

/// A segmenter over a fixed vocabulary, standing in for the host's system tokenizer:
/// no cut may fall strictly inside one of the listed words.
private struct DictionarySegmenter: WordSegmenter {
    let words: [String]

    func splitsWord(in text: String, at offset: Int) -> Bool {
        let chars = Array(text)
        for word in words {
            let w = Array(word)
            guard w.count > 1, offset > 0, offset < chars.count else { continue }
            for start in 0...(chars.count - w.count)
            where Array(chars[start..<start + w.count]) == w
                && start < offset && offset < start + w.count {
                return true
            }
        }
        return false
    }
}

@Suite struct CommitPolicyTests {
    @Test func singlePartialIsNotStable() {
        var p = CommitPolicy()
        #expect(commits(p.process(hyp("我们下周开会。"), at: 0)).isEmpty)
    }

    @Test func duplicateHypothesesCommitAtSentenceEndOnce() {
        var p = CommitPolicy()
        _ = p.process(hyp("我们下周开会。"), at: 0)
        let first = commits(p.process(hyp("我们下周开会。"), at: 100 * ms))
        #expect(first.map(\.text) == ["我们下周开会。"])
        #expect(first.first?.reason == .sentenceEnd)
        #expect(first.first?.id == UnitID(1))
        // The same hypothesis again must not emit the unit twice.
        #expect(commits(p.process(hyp("我们下周开会。"), at: 200 * ms)).isEmpty)
        #expect(p.frontier == 7)
    }

    @Test func onlyAgreedPrefixBeyondFrontierCommits() {
        var p = CommitPolicy()
        _ = p.process(hyp("今天天气很好。明天"), at: 0)
        let out = commits(p.process(hyp("今天天气很好。明天下雨"), at: 100 * ms))
        #expect(out.map(\.text) == ["今天天气很好。"])
        #expect(commits(p.process(hyp("今天天气很好。明天会下雨。"), at: 200 * ms)).isEmpty)
        let next = commits(p.process(hyp("今天天气很好。明天会下雨。"), at: 300 * ms))
        #expect(next.map(\.text) == ["明天会下雨。"])
        #expect(next.first?.id == UnitID(2))
    }

    @Test func normalizationIgnoresWidthCaseAndWhitespace() {
        var p = CommitPolicy()
        _ = p.process(hyp("我们用 Zoom 开会。"), at: 0)
        let out = commits(p.process(hyp("我们用zoom开会."), at: 100 * ms))
        #expect(out.count == 1)
        // Raw text of the latest hypothesis is committed.
        #expect(out.first?.text == "我们用zoom开会.")
    }

    @Test func vadEndRequestsFinalizeButDoesNotCommitPartial() {
        var p = CommitPolicy()
        _ = p.process(hyp("好的"), at: 0)
        _ = p.process(hyp("好的"), at: 100 * ms)
        let out = p.speechEnded(at: 96_000)
        #expect(out == [.requestFinalize(utterance: 1)])
        #expect(p.frontier == 0)
    }

    @Test func finalizedShortAcknowledgmentCommitsImmediately() {
        var p = CommitPolicy()
        let out = commits(p.process(hyp("好的", final: true), at: 0))
        #expect(out.map(\.text) == ["好的"])
        #expect(out.first?.reason == .finalized)
        #expect(out.first?.semanticWaitNs == 0)
    }

    @Test func commaNeedsMinimumLength() {
        var p = CommitPolicy()
        _ = p.process(hyp("对，我们明天"), at: 0)
        #expect(commits(p.process(hyp("对，我们明天"), at: 100 * ms)).isEmpty)
        _ = p.process(hyp("对，我们明天见面，然后"), at: 200 * ms)
        let out = commits(p.process(hyp("对，我们明天见面，然后"), at: 300 * ms))
        #expect(out.map(\.text) == ["对，我们明天见面，"])
        #expect(out.first?.reason == .clauseComma)
    }

    @Test func negationAtCommaHoldsAndReportsAfterWindow() {
        var p = CommitPolicy()
        _ = p.process(hyp("这个方案我们不，"), at: 0)
        #expect(commits(p.process(hyp("这个方案我们不，"), at: 100 * ms)).isEmpty)
        let late = p.process(hyp("这个方案我们不，"), at: 1_000 * ms)
        #expect(commits(late).isEmpty)
        #expect(late.contains { if case .hold(_, .negation, _, _) = $0 { true } else { false } })
        _ = p.process(hyp("这个方案我们不，同意。"), at: 1_100 * ms)
        let out = commits(p.process(hyp("这个方案我们不，同意。"), at: 1_200 * ms))
        #expect(out.map(\.text) == ["这个方案我们不，同意。"])
        #expect(out.first.map { $0.semanticWaitNs >= 1_100 * ms } == true)
    }

    @Test func unfinishedNumberAndDecimalAreNotSplit() {
        var p = CommitPolicy()
        _ = p.process(hyp("价格是3."), at: 0)
        #expect(commits(p.process(hyp("价格是3."), at: 100 * ms)).isEmpty)
        _ = p.process(hyp("价格是3.5万，"), at: 200 * ms)
        #expect(commits(p.process(hyp("价格是3.5万，"), at: 300 * ms)).map(\.text) == ["价格是3.5万，"])

        var q = CommitPolicy()
        _ = q.process(hyp("会议定在三月二十，"), at: 0)
        #expect(commits(q.process(hyp("会议定在三月二十，"), at: 100 * ms)).isEmpty)
    }

    @Test func conjunctionSplitOnlyAfterReconsiderWindow() {
        var p = CommitPolicy()
        let text = "我们已经完成了测试但是还要检查"
        _ = p.process(hyp(text), at: 0)
        #expect(commits(p.process(hyp(text), at: 100 * ms)).isEmpty)
        let out = commits(p.process(hyp(text), at: 1_000 * ms))
        #expect(out.map(\.text) == ["我们已经完成了测试"])
        #expect(out.first?.reason == .conjunction)
    }

    @Test func onlyButCompoundDoesNotBecomeTwoUnrelatedClauses() {
        for streaming in [false, true] {
            var config = CommitPolicyConfig()
            config.streamingClauses = streaming
            var p = CommitPolicy(config: config)
            let text = "他们只不过多看了几眼"
            _ = p.process(hyp(text), at: 0)
            #expect(commits(p.process(hyp(text), at: 100 * ms)).isEmpty)
            #expect(commits(p.process(hyp(text), at: 1_000 * ms)).isEmpty)
            let final = commits(p.process(hyp(text, final: true), at: 1_100 * ms))
            #expect(final.map(\.text) == [text])
        }
    }

    @Test func forcedLengthCapBoundsWaiting() {
        var config = CommitPolicyConfig()
        config.maxUnitKeys = 10
        var p = CommitPolicy(config: config)
        let text = "一二三四五六七八九十甲乙丙丁"
        _ = p.process(hyp(text), at: 0)
        let out = commits(p.process(hyp(text), at: 100 * ms))
        #expect(out.first?.reason == .forcedLength)
        #expect((out.first?.text.count ?? 0) <= 10)
    }

    @Test func latePunctuationNeverSpeaksCommittedContentTwice() {
        var config = CommitPolicyConfig()
        config.maxUnitSourceSeconds = 3
        var p = CommitPolicy(config: config)
        // Partials arrive unpunctuated, and the final result adds a period inside content that
        // was already committed. Nothing may be spoken twice or dropped.
        let partial = "我们下周开会讨论这个方案"
        _ = p.process(hyp(partial, frontier: 4 * 48_000), at: 0)
        let first = commits(p.process(hyp(partial, frontier: 4 * 48_000), at: 100 * ms))
        #expect(first.first?.reason == .lengthClause)
        let rest = commits(p.process(hyp("我们下周开会。讨论这个方案。", final: true,
                                         frontier: 5 * 48_000), at: 200 * ms))
        #expect(contentOnly((first + rest).map(\.text).joined()) == contentOnly(partial))
    }

    @Test func insertedCommaDoesNotShiftTheFrontierWithoutTheBudget() {
        var config = CommitPolicyConfig()
        config.maxUnitKeys = 8
        var p = CommitPolicy(config: config)
        let partial = "我们下周开会讨论这个方案"
        _ = p.process(hyp(partial), at: 0)
        let first = commits(p.process(hyp(partial), at: 100 * ms))
        #expect(first.map(\.text) == ["我们下周开会讨论"])
        #expect(first.first?.reason == .forcedLength)
        let rest = commits(p.process(hyp("我们下周开会，讨论这个方案。", final: true), at: 200 * ms))
        // Before re-alignment this committed "论这个方案。": the inserted comma moved the
        // frontier back over a key that had already been spoken.
        #expect(rest.map(\.text) == ["这个方案。"])
        #expect(contentOnly((first + rest).map(\.text).joined()) == contentOnly(partial))
    }

    @Test func longUnfinalizedUtteranceClosesAtTheSourceTimeBudget() {
        var p = CommitPolicy()
        let text = "我们今天下午三点在会议室讨论下个季度的预算安排"
        _ = p.process(hyp(text, frontier: 5 * 48_000), at: 0)
        let out = commits(p.process(hyp(text, frontier: 5 * 48_000), at: 100 * ms))
        #expect(out.count == 1)
        #expect(out.first?.reason == .lengthClause)
        // Cut as late as the safety rules allow, leaving a splittable tail.
        #expect(out.first?.text == "我们今天下午三点在会议室讨论下个季度的预")
        #expect(out.first?.span == SourceSpan(start: 0, end: 5 * 48_000))
        #expect(out.first?.timingEstimated == true)
        // The tail waits for its own evidence, and its estimated span starts where the
        // length clause ended, so coverage never double-counts source.
        let rest = commits(p.process(hyp(text, final: true, frontier: 6 * 48_000), at: 200 * ms))
        #expect(rest.map(\.text) == ["算安排"])
        #expect(rest.first?.reason == .finalized)
        #expect(rest.first?.span == SourceSpan(start: 5 * 48_000, end: 6 * 48_000))
    }

    @Test func lengthBudgetWaitsForTheKeyFloor() {
        var p = CommitPolicy()
        let text = "我们今天下午三点开会"
        _ = p.process(hyp(text, frontier: 10 * 48_000), at: 0)
        // 10 keys is below the floor: a frontier that runs ahead of the text (VAD held open
        // over music) must not turn a short clause into a committed unit.
        #expect(commits(p.process(hyp(text, frontier: 10 * 48_000), at: 100 * ms)).isEmpty)
    }

    @Test func lengthBudgetCutsEarlierRatherThanEndOnANumber() {
        var p = CommitPolicy()
        let text = "我们讨论一二三四五六七八九十"
        _ = p.process(hyp(text, frontier: 6 * 48_000), at: 0)
        let out = commits(p.process(hyp(text, frontier: 6 * 48_000), at: 100 * ms))
        #expect(out.map(\.text) == ["我们讨论"])
        #expect(out.first?.reason == .lengthClause)
    }

    @Test func lengthBudgetHoldsWhenNoBoundaryIsSafe() {
        var p = CommitPolicy()
        let text = "一二三四五六七八九十一二"
        _ = p.process(hyp(text, frontier: 6 * 48_000), at: 0)
        #expect(commits(p.process(hyp(text, frontier: 6 * 48_000), at: 100 * ms)).isEmpty)
        // Still reported as waiting, with the same reason the reconsider window uses.
        let out = p.process(hyp(text, frontier: 6 * 48_000), at: 1_000 * ms)
        #expect(out.contains(.hold(utterance: 1, reason: .number, pendingKeys: 12, waitedNs: 900 * ms)))
    }

    @Test func lengthBudgetDisabledWaitsForFinalization() {
        var config = CommitPolicyConfig()
        config.maxUnitSourceSeconds = 0
        var p = CommitPolicy(config: config)
        let text = "我们今天下午三点在会议室讨论下个季度的预算安排"
        _ = p.process(hyp(text, frontier: 8 * 48_000), at: 0)
        #expect(commits(p.process(hyp(text, frontier: 8 * 48_000), at: 100 * ms)).isEmpty)
        #expect(commits(p.process(hyp(text, final: true, frontier: 8 * 48_000), at: 200 * ms)).count == 1)
    }

    @Test func lengthBudgetRespectsWordBoundariesWhenConfigured() {
        var config = CommitPolicyConfig()
        config.preferWordBoundaries = true
        var p = CommitPolicy(config: config)
        let text = "we will discuss the budget tomorrow afternoon"
        _ = p.process(hyp(text, frontier: 6 * 48_000), at: 0)
        let out = commits(p.process(hyp(text, frontier: 6 * 48_000), at: 100 * ms))
        #expect(out.first?.text == "we will discuss the budget tomorrow")
        #expect(out.first?.reason == .lengthClause)
    }

    @Test func lengthBudgetNeverCutsInsideAWordTheSegmenterKnows() {
        // Per-character keys hide Mandarin words: the same cut that lands after 预 in the
        // test above would speak half of 度的预算. The host's segmenter is the only thing
        // that can see it, so the rule must obey it and cut earlier instead.
        var p = CommitPolicy(wordSegmenter: DictionarySegmenter(words: ["度的预算"]))
        let text = "我们今天下午三点在会议室讨论下个季度的预算安排"
        _ = p.process(hyp(text, frontier: 5 * 48_000), at: 0)
        let out = commits(p.process(hyp(text, frontier: 5 * 48_000), at: 100 * ms))
        #expect(out.first?.reason == .lengthClause)
        #expect(out.first?.text == "我们今天下午三点在会议室讨论下个季")
        // The rest of the word is not lost: the tail carries it to the next unit.
        let rest = commits(p.process(hyp(text, final: true, frontier: 6 * 48_000), at: 200 * ms))
        #expect(rest.map(\.text) == ["度的预算安排"])
        #expect(contentOnly((out + rest).map(\.text).joined()) == contentOnly(text))
    }

    @Test func successiveLengthClausesStayDisjoint() {
        var p = CommitPolicy()
        p.speechStarted(at: 48_000)
        let first = "我们今天下午三点在会议室讨论下个季度的预算安排问题"
        _ = p.process(hyp(first, frontier: 5 * 48_000), at: 0)
        let a = commits(p.process(hyp(first, frontier: 5 * 48_000), at: 100 * ms))
        // The second half of the utterance keeps growing, and a second budget cut follows.
        let second = first + "并且需要各位提前准备材料"
        let b = commits(p.process(hyp(second, frontier: 10 * 48_000), at: 200 * ms))
        let c = commits(p.process(hyp(second, frontier: 10 * 48_000), at: 300 * ms))
        let units = a + b + c
        #expect(units.count >= 2)
        #expect(Set(units.map(\.id)).count == units.count)
        for x in zip(units, units.dropFirst()) {
            #expect(x.0.span.end <= x.1.span.start)
        }
        #expect(units.first?.span.start == 48_000)
    }

    @Test func revisionBehindFrontierIsReportedOnce() {
        var p = CommitPolicy()
        _ = p.process(hyp("我们开会。"), at: 0)
        _ = p.process(hyp("我们开会。"), at: 100 * ms)
        let a = p.process(hyp("你们开会。然后"), at: 200 * ms)
        let b = p.process(hyp("你们开会。然后"), at: 300 * ms)
        #expect(a.contains(.revisionBehindFrontier(utterance: 1, committedKeys: 5)))
        #expect(!b.contains(.revisionBehindFrontier(utterance: 1, committedKeys: 5)))
    }

    @Test func tokenTimingIsUsedWhenEveryTokenHasIt() {
        var p = CommitPolicy()
        let tokens = [ASRToken(text: "好的", span: SourceSpan(start: 1_000, end: 20_000)),
                      ASRToken(text: "。", span: SourceSpan(start: 20_000, end: 21_000))]
        let h = ASRHypothesis(utterance: 1, tokens: tokens, isFinal: true, audioFrontier: 30_000)
        let unit = commits(p.process(h, at: 0)).first
        #expect(unit?.span == SourceSpan(start: 1_000, end: 21_000))
        #expect(unit?.timingEstimated == false)
    }

    @Test func missingTimingUsesConservativeOnsetToFrontier() {
        var p = CommitPolicy()
        p.speechStarted(at: 4_800)
        let unit = commits(p.process(hyp("好的", final: true, frontier: 60_000), at: 0)).first
        #expect(unit?.span == SourceSpan(start: 4_800, end: 60_000))
        #expect(unit?.timingEstimated == true)
    }

    @Test func newUtteranceResetsComparisonAndReportsAbandonedText() {
        var p = CommitPolicy()
        _ = p.process(hyp("我们", utt: 1), at: 0)
        let out = p.process(hyp("你好", utt: 2), at: 100 * ms)
        #expect(out.contains(.abandoned(utterance: 1, text: "我们")))
        #expect(p.frontier == 0)
    }

    @Test func unsafeEndingClassifier() {
        #expect(unsafeEnding("我们不") == .negation)
        #expect(unsafeEnding("We do not") == .negation)
        #expect(unsafeEnding("一共有三百二") == .number)
        #expect(unsafeEnding("价格是3.5万") == nil)
        #expect(unsafeEnding("因为") == .dangling)
        #expect(unsafeEnding("我们下周开会") == nil)
        #expect(unsafeEnding("Nothing") == nil)
    }

    @Test func timedNewUtteranceAdvancesOnsetWithoutAVADSilence() {
        var p = CommitPolicy()
        p.speechStarted(at: 0)
        let first = ASRHypothesis(utterance: 1, tokens: [ASRToken(text: "第一句。", span: SourceSpan(start: 0, end: 48_000))],
                                  isFinal: true, audioFrontier: 48_000)
        _ = p.process(first, at: 1_000 * ms)
        let next = ASRHypothesis(utterance: 2, tokens: [ASRToken(text: "新的", span: SourceSpan(start: 480_000, end: 528_000))],
                                 isFinal: false, audioFrontier: 528_000)
        _ = p.process(next, at: 11_000 * ms)
        #expect(p.uncommittedStart == 480_000)
        #expect(p.frontier == 0)
    }
}
