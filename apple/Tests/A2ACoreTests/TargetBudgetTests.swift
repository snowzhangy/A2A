// Fitting a translation into the time its source audio took: the spoken budget, the deletion
// that respects it, and the silence verdict for answers that carry no content
// (reports/week2/live-session-recovery.md).

import A2ACore
import Testing

private let rate: Double = 48_000

private func span(_ start: Double, _ end: Double) -> SourceSpan {
    SourceSpan(start: Int64(start * rate), end: Int64(end * rate))
}

/// A 3 s direct-audio session: the shape the app runs when a source piece is sent whole.
private struct AudioSession {
    var p: SessionPipeline
    var effects: [PipelineEffect] = []

    init(direction: TranslationDirection = .chineseToEnglish, targetSegmenter: (any WordSegmenter)? = nil,
         tune: (inout PipelineConfig) -> Void = { _ in }) {
        var config = PipelineConfig(session: "budget", direction: direction)
        config.directAudioTranslation = true
        config.originalAudioEnabled = true   // so a rejection would ask for the original bus
        tune(&config)
        p = SessionPipeline(config: config, targetWordSegmenter: targetSegmenter)
        _ = p.handle(.arm, at: 0)
        _ = p.handle(.armSucceeded, at: 0)
        _ = p.handle(.profileReady(SpeakerID("S1"), version: 1), at: 0)
    }

    @discardableResult
    mutating func piece(_ seconds: Double = 3, at t: Double) -> [PipelineEffect] {
        let out = p.handle(.audioSegment(span(0, seconds)), at: UInt64(t * 1e9))
        effects += out
        return out
    }

    @discardableResult
    mutating func translate(_ text: String, unit: Int = 1, at t: Double) -> [PipelineEffect] {
        let out = p.handle(.translation(UnitID(unit), .success(TranslationCandidate(unitID: UnitID(unit), text: text))),
                           at: UInt64(t * 1e9))
        effects += out
        return out
    }

    func synthesizedText() -> String? {
        effects.compactMap { if case .synthesize(let j) = $0 { j.text } else { nil } }.first
    }

    func traces(_ kind: TraceKind) -> [TraceEvent] {
        effects.compactMap { if case .trace(let e) = $0, e.kind == kind { e } else { nil } }
    }

    func disposition() -> (TraceValue, TraceValue)? {
        for case .trace(let e) in effects where e.kind == .sourceDisposition {
            if let d = e.value("disposition"), let r = e.value("reason") { return (d, r) }
        }
        return nil
    }

    mutating func tick(_ t: Double) {
        effects += p.handle(.tick, at: UInt64(t * 1e9))
    }
}

@Suite struct TargetBudgetTests {
    @Test func budgetComesFromTheSpokenRateOfTheTargetLanguage() {
        var zh = PipelineConfig(session: "zh")
        zh.directAudioTranslation = true
        #expect(zh.validator.spokenCharactersPerSecond == 15)
        #expect(zh.validator.spokenBudgetCharacters(sourceDurationSeconds: 3) == 60)
        let en = PipelineConfig(session: "en", direction: .englishToChinese)
        // Mandarin targets are budgeted in Han characters, not Latin ones.
        #expect(en.validator.spokenCharactersPerSecond == 4)
        #expect(en.validator.spokenBudgetCharacters(sourceDurationSeconds: 3) == 16)
    }

    @Test func shorteningKeepsWholeClausesThenWholeWords() {
        let text = "The good news is that the price is final, and we can start on Monday morning if the team agrees."
        // Budget cuts the trailing clause away and keeps a sentence, not a word fragment.
        let cut = TargetShortener.shorten(text, to: 48)
        #expect(cut == "The good news is that the price is final")
        #expect(cut!.count <= 48)
        // The only clause boundary sits at character 4: a word cut deeper into the line keeps
        // the message instead of dropping a whole sentence to a fragment.
        #expect(TargetShortener.shorten("Yes, we can start on Monday morning as planned.", to: 40)
                    == "Yes, we can start on Monday morning")
    }

    @Test func anEarlyClauseEndingLosesToAWordCutThatFillsTheSlot() {
        // The clause ends at 45% of a 60-character slot; ending there would leave half the slot
        // silent (1 November live session), so the deepest word boundary wins.
        let text = "The new price is final now, so we can sign the contract with the client before Friday."
        #expect(TargetShortener.shorten(text, to: 60) == "The new price is final now, so we can sign the contract")
        // The old 40% preference is still available to a caller that wants it.
        #expect(TargetShortener.shorten(text, to: 60, preferClauseFraction: 0.4) == "The new price is final now")
    }

    @Test func shorteningRefusesToSpeakAMisleadingFragment() {
        // One 90-character clause with a single comma at character 8: no safe prefix is left.
        #expect(TargetShortener.shorten("Yes, aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                                        to: 60) == nil)
        #expect(TargetShortener.shorten("fits inside the budget", to: 40) == "fits inside the budget")
        #expect(TargetShortener.shorten("nothing", to: 0) == nil)
    }

    @Test func shorteningNeverEndsOnADanglingFunctionWord() {
        let cut = TargetShortener.shorten("Please tell the client that we will set the price of the extra seats today", to: 56)
        #expect(cut != nil)
        #expect(cut!.hasSuffix("price"))
        #expect(!cut!.hasSuffix("of"))
    }

    @Test func hanTargetsCutAtCharacterBoundariesAndAvoidWordsWhenASegmenterIsPresent() {
        let text = "我们下周开会讨论这个方案的成本和价格变化"
        #expect(TargetShortener.shorten(text, to: 14)?.count == 14)
        struct FixedSegmenter: WordSegmenter {
            func splitsWord(in text: String, at offset: Int) -> Bool { offset % 3 != 0 }
        }
        // The injected segmenter pushes the cut back to the nearest boundary it accepts.
        #expect(TargetShortener.shorten(text, to: 14, wordSegmenter: FixedSegmenter()) == "我们下周开会讨论这个方案")
    }

    @Test func englishToMandarinShorteningUsesTheTargetSegmenter() {
        struct FixedSegmenter: WordSegmenter {
            func splitsWord(in text: String, at offset: Int) -> Bool { offset % 3 != 0 }
        }
        let line = "我们下周开会讨论这个方案的预算价格是三点五万元"
        var plain = AudioSession(direction: .englishToChinese)
        plain.piece(at: 1)
        plain.translate(line, at: 1.4)
        #expect(plain.synthesizedText()?.count == 16)   // 3 s of speech at 4 Han characters a second
        var segmented = AudioSession(direction: .englishToChinese, targetSegmenter: FixedSegmenter())
        segmented.piece(at: 1)
        segmented.translate(line, at: 1.4)
        #expect(segmented.synthesizedText() == String(Array(line.prefix(15))))
        // A cut that would land on a dangling function word gives one character back.
        #expect(TargetShortener.shorten("我们下周开会讨论这个方案的成本和价格", to: 16) == "我们下周开会讨论这个方案的成本")
    }

    @Test func anOverLongTargetIsSpokenShorterRatherThanDropped() {
        var s = AudioSession()
        s.piece(at: 1)
        let line = "The good news is that the price is final, and we can start on Monday morning."
        let effects = s.translate(line, at: 1.4)
        let spoken = s.synthesizedText()
        #expect(spoken != nil && spoken! != line && spoken!.count <= 60)
        #expect(effects.contains { if case .synthesize(let job) = $0 { job.text == spoken } else { false } })
        let target = s.traces(.commitTarget).first
        #expect(target?.value("shortened") == .bool(true))
        #expect(target?.value("mt_chars") == .int(Int64(line.count)))
        #expect(s.disposition() == nil || s.disposition()!.0 != .string("original_only"))   // the line still plays
    }

    @Test func anEmptyAnswerIsSilenceRatherThanAFailedTranslation() {
        var s = AudioSession()
        s.piece(at: 1)
        let effects = s.translate("   ", at: 1.4)
        let disposed = s.disposition()
        #expect(disposed?.0 == .string("silence") && disposed?.1 == .string("empty"))
        #expect(s.traces(.error).isEmpty)
        // No fallback: nothing was skipped, so the original bus must not be asked for audio.
        #expect(s.traces(.fallbackEnter).isEmpty)
        #expect(!effects.contains { if case .rampGains = $0 { true } else { false } })
        // The unit is terminal: it never synthesizes and holds no debt.
        #expect(s.synthesizedText() == nil)
        #expect(s.traces(.commitTarget).isEmpty)
        s.tick(2)
        #expect(s.traces(.fallbackEnter).isEmpty)
    }

    @Test func aFillerAnswerFromMusicOrAPauseIsSilenceToo() {
        var s = AudioSession()
        s.piece(at: 1)
        s.translate("Yeah.", at: 1.4)
        let disposed = s.disposition()
        #expect(disposed?.0 == .string("silence") && disposed?.1 == .string("filler"))
        #expect(s.traces(.error).isEmpty)
        #expect(s.synthesizedText() == nil)
        // mt.result stays ok: the translator answered, there was simply nothing to say.
        #expect(s.traces(.mtResult).first?.value("ok") == .bool(true))
        #expect(s.traces(.mtResult).first?.value("silence") == .string("filler"))
    }

    @Test func silencedUnitsDoNotBecomeTheTranslationContext() {
        var s = AudioSession()
        s.piece(at: 1)
        s.translate("Oh.", at: 1.4)
        // The next piece asks for context; the filler must not have entered it.
        let second = s.p.handle(.audioSegment(span(3, 6)), at: 2_000_000_000)
        let requests = second.compactMap { if case .translate(let r) = $0 { r } else { nil } }
        #expect(requests.count == 1 && requests[0].context.isEmpty)
    }

    @Test func aLineThatCannotBeCutStaysAGapRatherThanSilence() {
        // Same over-long line, shortening off: it is a real gap in coverage, not silence.
        var strict = AudioSession(tune: { $0.validator.shortensOverBudgetTargets = false })
        let line = "The good news is that the price is final, and we can start on Monday morning "
            + "if the team agrees that the schedule works for everyone involved today."
        strict.piece(at: 1)
        strict.translate(line, at: 1.4)
        let disposed = strict.disposition()
        #expect(disposed?.0 == .string("original_only") && disposed?.1 == .string("too_long"))
        // And with shortening on, the very same line is spoken instead of skipped.
        var s = AudioSession()
        s.piece(at: 1)
        s.translate(line, at: 1.4)
        #expect(s.synthesizedText() == "The good news is that the price is final, and we can start")
        #expect(s.disposition() == nil)
    }
}
