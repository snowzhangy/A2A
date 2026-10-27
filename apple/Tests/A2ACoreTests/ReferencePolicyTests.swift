// Voice reference acquisition policy: which finalized utterances form the reference and
// when each profile version is due.

import A2ACore
import Testing

private let rate = Int64(canonicalSampleRate)

private func final(_ text: String, from start: Double, to end: Double, timed: Bool = true) -> ASRHypothesis {
    let span = SourceSpan(start: Int64(start * Double(rate)), end: Int64(end * Double(rate)))
    return ASRHypothesis(utterance: 1, tokens: [ASRToken(text: text, span: timed ? span : nil)],
                         isFinal: true, audioFrontier: span.end)
}

private func seconds(_ s: Double, at start: Double = 0) -> SourceSpan {
    SourceSpan(start: Int64(start * Double(rate)), end: Int64((start + s) * Double(rate)))
}

@Suite struct ReferencePolicyTests {
    @Test func speechSpanPadsTokenTimingAndNeedsFinalTimedResults() {
        let policy = ReferencePolicy(speaker: SpeakerID("S1"))
        let pad = policy.config.paddingSamples
        #expect(policy.speechSpan(of: final("我们开会。", from: 1, to: 3)) == SourceSpan(start: rate - pad, end: 3 * rate + pad))
        #expect(policy.speechSpan(of: final("我们开会。", from: 1, to: 3, timed: false)) == nil)
        let partial = ASRHypothesis(utterance: 1, tokens: [ASRToken(text: "我们", span: seconds(1))], isFinal: false, audioFrontier: rate)
        #expect(policy.speechSpan(of: partial) == nil)
        // Padding never reaches before the epoch start.
        #expect(policy.speechSpan(of: final("好", from: 0, to: 1))?.start == 0)
    }

    @Test func firstVersionAtMinimumThenOneUpgradeAtTarget() throws {
        var policy = ReferencePolicy(speaker: SpeakerID("S1"))
        #expect(try policy.offer(span: seconds(2), text: "第一句。", levelDB: -20, clippedFraction: 0).get() == nil)
        let v1 = try #require(try policy.offer(span: seconds(2, at: 3), text: "第二句。", levelDB: -20, clippedFraction: 0).get())
        #expect(v1.version == 1 && v1.segments.count == 2 && v1.text == "第一句。 第二句。")
        #expect(abs(v1.seconds - 4) < 1e-9)
        #expect(try policy.offer(span: seconds(2, at: 6), text: "三。", levelDB: -20, clippedFraction: 0).get() == nil)
        let v2 = try #require(try policy.offer(span: seconds(2.5, at: 9), text: "四。", levelDB: -20, clippedFraction: 0).get())
        #expect(v2.version == 2 && v2.segments.count == 4 && policy.isComplete)
        #expect(policy.offer(span: seconds(2, at: 12), text: "五。", levelDB: -20, clippedFraction: 0) == .failure(.complete))
    }

    @Test func rejectsQuietClippedShortAndEmptySegments() {
        var policy = ReferencePolicy(speaker: SpeakerID("S1"))
        #expect(policy.offer(span: seconds(3), text: "  ", levelDB: -20, clippedFraction: 0) == .failure(.empty))
        #expect(policy.offer(span: seconds(0.2), text: "好。", levelDB: -20, clippedFraction: 0) == .failure(.tooShort))
        #expect(policy.offer(span: seconds(3), text: "好。", levelDB: -60, clippedFraction: 0) == .failure(.quiet))
        #expect(policy.offer(span: seconds(3), text: "好。", levelDB: -.infinity, clippedFraction: 0) == .failure(.quiet))
        #expect(policy.offer(span: seconds(3), text: "好。", levelDB: -20, clippedFraction: 0.01) == .failure(.clipped))
        #expect(policy.acceptedSeconds == 0)
    }

    @Test func shortDialogueTurnsAccumulateWithoutCountingPaddingAsSpeakerEvidence() throws {
        var policy = ReferencePolicy(speaker: SpeakerID("S1"))
        for i in 0..<5 {
            #expect(try policy.offer(span: seconds(0.5, at: Double(i)), text: "Okay", levelDB: -20, clippedFraction: 0).get() == nil)
        }
        let plan = try #require(try policy.offer(span: seconds(0.5, at: 5), text: "All right", levelDB: -20, clippedFraction: 0).get())
        #expect(plan.version == 1 && plan.seconds == 3)
        #expect(plan.text == "Okay Okay Okay Okay Okay All right")
        let h = final("好", from: 1, to: 1.4)
        #expect(policy.tokenSpan(of: h) == SourceSpan(start: 48_000, end: 67_200))
        #expect(policy.speechSpan(of: h) == SourceSpan(start: 43_200, end: 72_000))
    }

    @Test func segmentBeyondCapEndsAcquisitionWithoutDuplicateVersion() throws {
        var policy = ReferencePolicy(speaker: SpeakerID("S1"))
        #expect(policy.offer(span: seconds(11), text: "很长。", levelDB: -20, clippedFraction: 0) == .failure(.tooLong))
        let v1 = try #require(try policy.offer(span: seconds(6), text: "一。", levelDB: -20, clippedFraction: 0).get())
        #expect(v1.version == 1)
        // Nothing new since v1, so a segment that cannot fit completes acquisition with no plan.
        #expect(policy.offer(span: seconds(5, at: 7), text: "二。", levelDB: -20, clippedFraction: 0) == .failure(.tooLong))
        #expect(policy.isComplete)
    }

    @Test func upgradeIssuedWhenNextSegmentCannotFit() throws {
        var policy = ReferencePolicy(speaker: SpeakerID("S1"))
        _ = try #require(try policy.offer(span: seconds(4), text: "一。", levelDB: -20, clippedFraction: 0).get())
        #expect(try policy.offer(span: seconds(2, at: 5), text: "二。", levelDB: -20, clippedFraction: 0).get() == nil)
        let v2 = try #require(try policy.offer(span: seconds(5, at: 8), text: "三。", levelDB: -20, clippedFraction: 0).get())
        #expect(v2.version == 2 && v2.segments.map(\.text) == ["一。", "二。"])
    }

    @Test func resetStartsOverForANewSpeaker() throws {
        var policy = ReferencePolicy(speaker: SpeakerID("S1"))
        _ = try policy.offer(span: seconds(4), text: "一。", levelDB: -20, clippedFraction: 0).get()
        policy.reset(speaker: SpeakerID("S2"))
        #expect(policy.acceptedSeconds == 0 && !policy.isComplete)
        let v1 = try #require(try policy.offer(span: seconds(3.5), text: "二。", levelDB: -20, clippedFraction: 0).get())
        #expect(v1.speaker == SpeakerID("S2") && v1.version == 1)
    }
}
