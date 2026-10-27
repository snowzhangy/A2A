import A2ACore
import Testing

private let ms: UInt64 = 1_000_000

private func hyp(_ text: String, utt: Int = 1, final: Bool = false, frontier: Int64 = 48_000) -> ASRHypothesis {
    ASRHypothesis(utterance: utt, tokens: text.map { ASRToken(text: String($0)) }, isFinal: final,
                  audioFrontier: frontier)
}

private func commits(_ outputs: [CommitOutput]) -> [SourceUnit] {
    outputs.compactMap { if case .commit(let u) = $0 { u } else { nil } }
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
