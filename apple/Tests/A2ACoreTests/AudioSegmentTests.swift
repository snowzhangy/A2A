import A2ACore
import Testing

@Suite struct AudioSegmentTests {
    @Test func cutsContinuousSpeechAndClosesOnSilenceWithoutOverlap() {
        var p = AudioSegmentPolicy(maxSeconds: 3, minSeconds: 0.25)
        #expect(p.process(events: [.speechStart(4_800)], frontier: 48_000).isEmpty)
        #expect(p.process(events: [], frontier: 148_800) == [SourceSpan(start: 4_800, end: 148_800)])
        #expect(p.process(events: [.speechEnd(196_800)], frontier: 211_200) == [SourceSpan(start: 148_800, end: 196_800)])
        #expect(p.process(events: [], frontier: 400_000).isEmpty)
    }

    @Test func directAudioUsesSourceTimeForValidationAndNeverRequiresTranscript() {
        var c = PipelineConfig(session: "audio")
        c.directAudioTranslation = true
        var p = SessionPipeline(config: c)
        _ = p.handle(.arm, at: 0)
        _ = p.handle(.armSucceeded, at: 0)
        _ = p.handle(.profileReady(SpeakerID("S1"), version: 1), at: 0)
        let span = SourceSpan(start: 0, end: 144_000)
        let effects = p.handle(.audioSegment(span), at: 3_000_000_000)
        #expect(effects.contains { if case .translate(let r) = $0 { r.sourceSpan == span && r.uncommittedSource.isEmpty } else { false } })
        let target = "We will meet next week to talk about the price."   // 47 characters: inside 3 s of speech
        let translated = p.handle(.translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: target))), at: 3_100_000_000)
        #expect(translated.contains { if case .synthesize(let job) = $0 { job.text == target } else { false } })
        #expect(!p.handle(.audioSegment(span), at: 3_200_000_000).contains { if case .translate = $0 { true } else { false } })
        var validator = TranslationValidator()
        validator.shortensOverBudgetTargets = false   // the hard cap, on its own
        #expect(validator.evaluate(TranslationCandidate(unitID: UnitID(2), text: String(repeating: "a", count: 106)),
                                  source: "", previousTarget: nil, sourceDurationSeconds: 3).rejection == .tooLong)
    }

    @Test func audioOnlyReferenceExplicitlyAllowsMissingTranscript() {
        let span = SourceSpan(start: 0, end: 144_000)
        var normal = ReferencePolicy(speaker: SpeakerID("S1"))
        #expect(normal.offer(span: span, text: "", levelDB: -20, clippedFraction: 0) == .failure(.empty))
        var config = ReferencePolicyConfig()
        config.allowEmptyText = true
        var audio = ReferencePolicy(speaker: SpeakerID("S1"), config: config)
        guard case .success(let plan?) = audio.offer(span: span, text: "", levelDB: -20, clippedFraction: 0) else {
            Issue.record("audio-only reference was not accepted")
            return
        }
        #expect(plan.seconds == 3 && plan.text.isEmpty)
    }

    @Test func directAudioWaitsForTheAttributedSpeakerProfile() {
        var config = PipelineConfig(session: "speakers")
        config.directAudioTranslation = true
        config.multiSpeaker = true
        config.requireSpeakerAttribution = true
        var pipeline = SessionPipeline(config: config)
        _ = pipeline.handle(.arm, at: 0)
        _ = pipeline.handle(.armSucceeded, at: 0)
        _ = pipeline.handle(.profileReady(SpeakerID("S1"), version: 1), at: 0)
        let span = SourceSpan(start: 0, end: 144_000)
        _ = pipeline.handle(.audioSegment(span), at: 3_000_000_000)
        let result = pipeline.handle(.translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "Hello there."))), at: 3_100_000_000)
        #expect(!result.contains { if case .synthesize = $0 { true } else { false } })
        let labeled = pipeline.handle(.speaker(.assign(SpeakerID("S2"), span)), at: 3_200_000_000)
        #expect(!labeled.contains { if case .synthesize = $0 { true } else { false } })
        let ready = pipeline.handle(.profileReady(SpeakerID("S2"), version: 1), at: 3_300_000_000)
        #expect(ready.contains { if case .synthesize(let job) = $0 { job.speaker == SpeakerID("S2") } else { false } })
    }
}

@Suite struct AudioStreamFeedTests {
    private let max = Int64(3 * 48_000)

    @Test func streamsVoicedAudioAheadAndClosesEachSpanWithItsRest() {
        var feed = AudioStreamFeed(maxSamples: max, minAppendSeconds: 0.1)
        // Speech starts: only audio up to the last voiced sample may go, in 100 ms steps or more.
        #expect(feed.update(closed: [], openStart: 4_800, voicedEnd: 7_200).isEmpty)
        #expect(feed.update(closed: [], openStart: 4_800, voicedEnd: 52_800) == [.append(SourceSpan(start: 4_800, end: 52_800))])
        // A pause inside speech stops the stream at the last voiced sample.
        #expect(feed.update(closed: [], openStart: 4_800, voicedEnd: 52_800).isEmpty)
        #expect(feed.update(closed: [], openStart: 4_800, voicedEnd: 148_800 + 9_600)
                == [.append(SourceSpan(start: 52_800, end: 148_800))])   // never past the forced cut
        // The cut closes the first span; the next one continues from it.
        let first = SourceSpan(start: 4_800, end: 148_800)
        #expect(feed.update(closed: [first], openStart: 148_800, voicedEnd: 158_400)
                == [.close(first), .append(SourceSpan(start: 148_800, end: 158_400))])
        // The speech end closes it with the audio that was not voiced yet when last streamed.
        let second = SourceSpan(start: 148_800, end: 170_000)
        #expect(feed.update(closed: [second], openStart: nil, voicedEnd: nil)
                == [.append(SourceSpan(start: 158_400, end: 170_000)), .close(second)])
        #expect(feed.update(closed: [], openStart: nil, voicedEnd: nil).isEmpty)
    }

    @Test func aDroppedSpanIsDiscardedAndAnUnstreamedSpanIsSentWhole() {
        var feed = AudioStreamFeed(maxSamples: max, minAppendSeconds: 0.1)
        #expect(feed.update(closed: [], openStart: 0, voicedEnd: 9_600) == [.append(SourceSpan(start: 0, end: 9_600))])
        // Too short for the segmenter: no span closes.
        #expect(feed.update(closed: [], openStart: nil, voicedEnd: nil) == [.discard])
        // A new speech start replaces a span that never closed.
        _ = feed.update(closed: [], openStart: 48_000, voicedEnd: 57_600)
        #expect(feed.update(closed: [], openStart: 96_000, voicedEnd: 105_600)
                == [.discard, .append(SourceSpan(start: 96_000, end: 105_600))])
        // A span that opened and closed within one step goes out whole.
        var fresh = AudioStreamFeed(maxSamples: max)
        let span = SourceSpan(start: 0, end: 24_000)
        #expect(fresh.update(closed: [span], openStart: nil, voicedEnd: nil) == [.append(span), .close(span)])
    }
}
