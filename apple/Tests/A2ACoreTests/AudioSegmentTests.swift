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

    // ADR-0017: under a picture delay a sentence stays one unit, and a cut lands in a breath.
    @Test func sentenceUnitsGrowWithThePictureDelayWithinBounds() {
        let second = Int64(canonicalSampleRate)
        #expect(AudioSegmentPolicy.sentences(presentationDelaySeconds: 8).maxSamples == 9 * second / 2)
        #expect(AudioSegmentPolicy.sentences(presentationDelaySeconds: 4).maxSamples == 3 * second)
        #expect(AudioSegmentPolicy.sentences(presentationDelaySeconds: 20).maxSamples == 6 * second)
        let p = AudioSegmentPolicy.sentences(presentationDelaySeconds: 8)
        #expect(p.streamLimitSamples == p.maxSamples - second)
        // The live default streams up to its fixed cut.
        #expect(AudioSegmentPolicy().streamLimitSamples == AudioSegmentPolicy().maxSamples)
    }

    @Test func aLongSentenceIsCutAtTheQuietestFrameBeforeTheMaximum() {
        var p = AudioSegmentPolicy(maxSeconds: 5, minSeconds: 0.25, dipWindowSeconds: 1)
        let frame = Int64(960)   // 20 ms
        var closed: [SourceSpan] = []
        var at: Int64 = 0
        _ = p.process(events: [.speechStart(0)], frontier: 0)
        while closed.isEmpty, at < 6 * 48_000 {
            let span = SourceSpan(start: at, end: at + frame)
            // A breath 4.40–4.42 s, and a quieter frame outside the window that must not count.
            let db: Float = span.start == 211_200 ? -50 : (span.start == 96_000 ? -70 : -20)
            closed = p.process(events: [], frontier: span.end, level: (span, db))
            at += frame
        }
        #expect(closed == [SourceSpan(start: 0, end: 211_680)])
        #expect(p.openStart == 211_680)
        #expect(p.capCloses == 1 && p.pauseCloses == 0)
        // A pause closes the rest as usual.
        #expect(p.process(events: [.speechEnd(300_000)], frontier: 310_000) == [SourceSpan(start: 211_680, end: 300_000)])
        #expect(p.pauseCloses == 1)
    }

    @Test func withoutFrameLevelsTheCutIsTheMaximum() {
        var p = AudioSegmentPolicy(maxSeconds: 5, minSeconds: 0.25, dipWindowSeconds: 1)
        _ = p.process(events: [.speechStart(0)], frontier: 0)
        #expect(p.process(events: [], frontier: 240_000) == [SourceSpan(start: 0, end: 240_000)])
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

@Suite struct AdaptiveSpeechThresholdTests {
    private func feed(_ t: inout AdaptiveSpeechThreshold, _ levels: [Float], from start: inout Int64) -> Float {
        var last: Float = 0
        for db in levels {
            last = t.update(span: SourceSpan(start: start, end: start + 960), db: db)
            start += 960
        }
        return last
    }

    @Test func pausesOverResidueFallBelowTheThreshold() {
        // Speech at -20 dB with pauses where a loud scene leaves residue at -36 dB.
        var t = AdaptiveSpeechThreshold(floorDB: -45)
        var at: Int64 = 0
        var threshold: Float = 0
        for _ in 0..<6 { threshold = feed(&t, Array(repeating: -20, count: 50) + Array(repeating: -36, count: 25), from: &at) }
        #expect(threshold > -36 && threshold < -20)
        #expect(abs(threshold - -28) <= 1)
    }

    @Test func quietOrFlatSignalsKeepTheFixedFloor() {
        var t = AdaptiveSpeechThreshold(floorDB: -45)
        var at: Int64 = 0
        // Clean speech over silence: the midpoint would be far below the floor.
        #expect(feed(&t, Array(repeating: -25, count: 100) + Array(repeating: -120, count: 50), from: &at) == -45)
        // No contrast (a steady bed): nothing to split on, so the floor stands.
        var flat = AdaptiveSpeechThreshold(floorDB: -45)
        var at2: Int64 = 0
        #expect(feed(&flat, Array(repeating: -33, count: 400), from: &at2) == -45)
        #expect(feed(&flat, [-Float.infinity, .nan], from: &at2) == -45)
    }

    @Test func onlyTheLastFewSecondsCount() {
        var t = AdaptiveSpeechThreshold(floorDB: -45, windowSeconds: 1)
        var at: Int64 = 0
        _ = feed(&t, Array(repeating: -10, count: 200), from: &at)
        // One second later the loud past is gone: speech at -20 over residue at -36.
        let threshold = feed(&t, Array(repeating: -20, count: 34) + Array(repeating: -36, count: 16), from: &at)
        #expect(abs(threshold - -28) <= 1)
    }
}
