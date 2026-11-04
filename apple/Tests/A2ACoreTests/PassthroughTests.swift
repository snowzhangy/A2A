// ADR-0018: a line already spoken in the target language, or answered with nothing, plays the
// actor's own isolated voice; a line is budgeted into the silence after it, not the next line.

import A2ACore
import Testing

private let speaker = SpeakerID("S1")

private struct Direct {
    var p: SessionPipeline
    var effects: [PipelineEffect] = []

    init(restores: Bool = true, direction: TranslationDirection = .chineseToEnglish) {
        var c = PipelineConfig(session: "passthrough", direction: direction)
        c.directAudioTranslation = true
        c.restoresOriginalVoice = restores
        p = SessionPipeline(config: c)
        run(0, .arm)
        run(0, .armSucceeded)
        run(0, .profileReady(speaker, version: 1))
    }

    @discardableResult
    mutating func run(_ t: Double, _ input: PipelineInput) -> [PipelineEffect] {
        let out = p.handle(input, at: UInt64(t * 1e9))
        effects += out
        return out
    }

    mutating func translate(_ id: Int, _ text: String, at t: Double) -> [PipelineEffect] {
        run(t, .translation(UnitID(id), .success(TranslationCandidate(unitID: UnitID(id), text: text))))
    }

    func reason(_ id: Int) -> String? {
        for case .trace(let e) in effects where e.kind == .sourceDisposition && e.value("unit") == .string("u\(id)") {
            if case .string(let r)? = e.value("reason") { return r }
        }
        return nil
    }

    func synthesized(_ out: [PipelineEffect]) -> String? {
        for case .synthesize(let job) in out { return job.text }
        return nil
    }
}

@Suite struct PassthroughTests {
    let span = SourceSpan(start: 0, end: 144_000)

    @Test func aLineAlreadyInTheTargetLanguagePlaysTheActorAndIsNotTranslated() {
        var s = Direct()
        s.run(3.0, .audioSegment(span))
        let out = s.run(3.1, .sourceLanguage(span, matchesTarget: true))
        #expect(out.contains(.cancelTranslation(UnitID(1))))
        #expect(out.contains(.restoreOriginalVoice(span)))
        #expect(s.reason(1) == "same_language")
        // The translator's late answer is stale: the line is never voiced twice.
        #expect(s.synthesized(s.translate(1, "Hello there.", at: 3.4)) == nil)
    }

    @Test func aLineInTheSourceLanguageIsTranslatedAsBefore() {
        var s = Direct()
        s.run(3.0, .audioSegment(span))
        #expect(s.run(3.1, .sourceLanguage(span, matchesTarget: false)).isEmpty)
        #expect(s.synthesized(s.translate(1, "Hello there.", at: 3.4)) == "Hello there.")
    }

    @Test func aVerdictAfterSynthesisStartedChangesNothing() {
        var s = Direct()
        s.run(3.0, .audioSegment(span))
        #expect(s.synthesized(s.translate(1, "Hello there.", at: 3.2)) != nil)
        let late = s.run(3.4, .sourceLanguage(span, matchesTarget: true))
        #expect(!late.contains(.restoreOriginalVoice(span)) && !late.contains(.cancelTranslation(UnitID(1))))
    }

    @Test func anUnansweredLineGetsItsVoiceBackOnlyWhenTheHostCanRestoreIt() {
        var s = Direct()
        s.run(3.0, .audioSegment(span))
        #expect(s.translate(1, "", at: 3.3).contains(.restoreOriginalVoice(span)))
        var off = Direct(restores: false)
        off.run(3.0, .audioSegment(span))
        #expect(!off.translate(1, "", at: 3.3).contains(.restoreOriginalVoice(span)))
        #expect(!off.run(3.4, .sourceLanguage(span, matchesTarget: true)).contains(.restoreOriginalVoice(span)))
    }

    @Test func anUnattributedLineRestoresTheActorBeforeTheBedLeavesTheHold() {
        var c = PipelineConfig(session: "missing-speaker")
        c.directAudioTranslation = true
        c.multiSpeaker = true
        c.requireSpeakerAttribution = true
        c.presentationDelaySeconds = 8
        c.restoresOriginalVoice = true
        c.originalVoiceDecisionDeadlineSeconds = 5.3
        var p = SessionPipeline(config: c)
        let line = SourceSpan(start: 0, end: 24_000)
        _ = p.handle(.arm, at: 0)
        _ = p.handle(.armSucceeded, at: 0)
        _ = p.handle(.captured(span: SourceSpan(start: 0, end: 48_000)), at: 1_000_000_000)
        _ = p.handle(.audioSegment(line), at: 1_000_000_000)
        _ = p.handle(.translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "Hello."))), at: 1_100_000_000)
        #expect(!p.handle(.tick, at: 5_200_000_000).contains(.restoreOriginalVoice(line)))
        #expect(p.handle(.tick, at: 5_400_000_000).contains(.restoreOriginalVoice(line)))
        let lateLabel = p.handle(.speaker(.assign(speaker, line)), at: 5_500_000_000)
        #expect(!lateLabel.contains { if case .synthesize = $0 { true } else { false } })
    }

    @Test func aMissingVoiceProfileRestoresTheActorBeforeTheBedLeavesTheHold() {
        var c = PipelineConfig(session: "missing-profile")
        c.directAudioTranslation = true
        c.multiSpeaker = true
        c.requireSpeakerAttribution = true
        c.presentationDelaySeconds = 8
        c.restoresOriginalVoice = true
        c.originalVoiceDecisionDeadlineSeconds = 5.3
        var p = SessionPipeline(config: c)
        let line = SourceSpan(start: 0, end: 48_000)
        _ = p.handle(.arm, at: 0)
        _ = p.handle(.armSucceeded, at: 0)
        _ = p.handle(.captured(span: line), at: 1_000_000_000)
        _ = p.handle(.speaker(.assign(speaker, line)), at: 1_000_000_000)
        _ = p.handle(.audioSegment(line), at: 1_000_000_000)
        _ = p.handle(.translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "Hello."))), at: 1_100_000_000)
        #expect(p.handle(.tick, at: 5_400_000_000).contains(.restoreOriginalVoice(line)))
        let lateVoice = p.handle(.profileReady(speaker, version: 1), at: 5_500_000_000)
        #expect(!lateVoice.contains { if case .synthesize = $0 { true } else { false } })
    }

    @Test func aStalledTranslatorRestoresTheActorAndIgnoresItsLateResult() {
        var c = PipelineConfig(session: "stalled-mt")
        c.directAudioTranslation = true
        c.restoresOriginalVoice = true
        c.originalVoiceDecisionDeadlineSeconds = 5.3
        var p = SessionPipeline(config: c)
        let line = SourceSpan(start: 0, end: 48_000)
        _ = p.handle(.arm, at: 0)
        _ = p.handle(.armSucceeded, at: 0)
        _ = p.handle(.profileReady(speaker, version: 1), at: 0)
        _ = p.handle(.captured(span: line), at: 1_000_000_000)
        _ = p.handle(.audioSegment(line), at: 1_000_000_000)
        let timeout = p.handle(.tick, at: 5_400_000_000)
        #expect(timeout.contains(.cancelTranslation(UnitID(1))))
        #expect(timeout.contains(.restoreOriginalVoice(line)))
        let late = p.handle(.translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "Hello."))), at: 5_500_000_000)
        #expect(!late.contains { if case .synthesize = $0 { true } else { false } })
    }

    @Test func aTranslationFailureRestoresTheOriginalVoice() {
        var s = Direct()
        s.run(3.0, .audioSegment(span))
        let failure = s.run(3.1, .translation(UnitID(1), .failure("unavailable")))
        #expect(failure.contains(.restoreOriginalVoice(span)))
        var off = Direct(restores: false)
        off.run(3.0, .audioSegment(span))
        #expect(!off.run(3.1, .translation(UnitID(1), .failure("unavailable"))).contains(.restoreOriginalVoice(span)))
    }

    @Test func synthesisFailureBeforePlaybackRestoresTheOriginalVoice() {
        var c = PipelineConfig(session: "tts-failure")
        c.directAudioTranslation = true
        c.restoresOriginalVoice = true
        c.originalVoiceDecisionDeadlineSeconds = 5.3
        var p = SessionPipeline(config: c)
        let line = SourceSpan(start: 0, end: 48_000)
        _ = p.handle(.arm, at: 0)
        _ = p.handle(.armSucceeded, at: 0)
        _ = p.handle(.profileReady(speaker, version: 1), at: 0)
        _ = p.handle(.captured(span: line), at: 1_000_000_000)
        _ = p.handle(.audioSegment(line), at: 1_000_000_000)
        _ = p.handle(.translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "Hello."))), at: 1_100_000_000)
        #expect(p.handle(.synthesisFailed(UnitID(1), "failed"), at: 1_200_000_000).contains(.restoreOriginalVoice(line)))
    }

    @Test func fallbackRestoresOnlyUnheardCanceledUnits() {
        var c = PipelineConfig(session: "fallback-restore")
        c.directAudioTranslation = true
        c.restoresOriginalVoice = true
        c.originalVoiceDecisionDeadlineSeconds = 5.3
        var p = SessionPipeline(config: c)
        let first = SourceSpan(start: 0, end: 48_000)
        let second = SourceSpan(start: 48_000, end: 96_000)
        _ = p.handle(.arm, at: 0)
        _ = p.handle(.armSucceeded, at: 0)
        _ = p.handle(.profileReady(speaker, version: 1), at: 0)
        _ = p.handle(.vad(.speechStart(0)), at: 0)
        _ = p.handle(.captured(span: first), at: 1_000_000_000)
        _ = p.handle(.audioSegment(first), at: 1_000_000_000)
        _ = p.handle(.translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "Hello."))), at: 1_100_000_000)
        let header = PCMChunkHeader(unitID: UnitID(1), speakerID: speaker, profileVersion: 1, sessionEpoch: 1,
                                    playoutGeneration: 1, format: PCMFormat(sampleRate: 24_000, channelCount: 1),
                                    span: first, isLast: false)
        _ = p.handle(.pcm(header, frames: 4_800), at: 1_200_000_000)
        _ = p.handle(.captured(span: second), at: 2_000_000_000)
        _ = p.handle(.audioSegment(second), at: 2_000_000_000)
        _ = p.handle(.underrun, at: 2_100_000_000)
        let fallback = p.handle(.tick, at: 2_200_000_000)
        #expect(fallback.contains(.restoreOriginalVoice(second)))
        #expect(!fallback.contains(.restoreOriginalVoice(first)))
    }

    @Test func aLineIsBudgetedIntoTheSilenceAfterItNotIntoTheNextLine() {
        // 3 s of English into Mandarin at 5 characters a second: 1 s of grace allows 20.
        let line = "我们下周开会讨论这个方案的预算价格是三点五万元"   // 23 characters
        var roomy = Direct(direction: .englishToChinese)
        roomy.run(3.0, .audioSegment(span))
        roomy.run(3.3, .followingSilence(span, seconds: 2.0))
        #expect(roomy.synthesized(roomy.translate(1, line, at: 3.4))?.count == 20)
        // The next line starts right away: only 0.2 s of grace, 16 characters.
        var tight = Direct(direction: .englishToChinese)
        tight.run(3.0, .audioSegment(span))
        tight.run(3.3, .followingSilence(span, seconds: 0))
        #expect(tight.synthesized(tight.translate(1, line, at: 3.4))?.count == 16)
    }
}
