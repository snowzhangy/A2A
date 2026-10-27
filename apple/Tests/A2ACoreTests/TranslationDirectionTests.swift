import A2ACore
import Testing

@Suite struct TranslationDirectionTests {
    @Test func directionsSelectAllStageLanguages() {
        let forward = TranslationDirection.chineseToEnglish
        let reverse = TranslationDirection.englishToChinese
        #expect(forward.source == reverse.target)
        #expect(forward.target == reverse.source)
        #expect(reverse.source.speechLocaleIdentifier == "en-US")
        #expect(reverse.source.translationIdentifier == "en")
        #expect(reverse.target.speechLocaleIdentifier == "zh-CN")
        #expect(reverse.target.translationIdentifier == "zh-Hans")
        #expect(reverse.target.cloneLanguage == "chinese")
    }

    @Test func validatorChecksTheSelectedTarget() {
        let english = TranslationValidator(targetLanguage: .english)
        let chinese = TranslationValidator(targetLanguage: .mandarin)
        let source = "We will meet next week."
        func candidate(_ text: String, complete: Bool = true) -> TranslationCandidate {
            TranslationCandidate(unitID: UnitID(1), text: text, complete: complete)
        }
        #expect(chinese.validate(candidate("我们下周开会。"), source: source, previousTarget: nil) == nil)
        #expect(chinese.validate(candidate("请在 Zoom 开会。"), source: source, previousTarget: nil) == nil)
        #expect(chinese.validate(candidate(source), source: source, previousTarget: nil) == .wrongLanguage)
        #expect(english.validate(candidate("我们下周开会。"), source: source, previousTarget: nil) == .wrongLanguage)
        #expect(english.validate(candidate(source), source: "我们下周开会。", previousTarget: nil) == nil)
        #expect(chinese.validate(candidate(" "), source: source, previousTarget: nil) == .empty)
        #expect(chinese.validate(candidate("我们下周开会。", complete: false), source: source, previousTarget: nil) == .incomplete)
        #expect(chinese.validate(candidate("我们下周开会。"), source: source, previousTarget: "我们下周开会。") == .duplicate)
        #expect(chinese.validate(candidate(String(repeating: "会", count: 200)), source: source, previousTarget: nil) == .tooLong)
    }

    @Test(arguments: TranslationDirection.allCases)
    func selectedPairIsTracedAndTargetIsSynthesized(_ direction: TranslationDirection) {
        var config = PipelineConfig(session: "direction", direction: direction)
        config.emitCaptions = true
        var pipeline = SessionPipeline(config: config)
        let start = pipeline.handle(.arm, at: 0)
        #expect(start.contains { effect in
            if case .trace(let event) = effect, event.kind == .sessionStart {
                return event.value("pair") == .string(direction.tracePair)
            }
            return false
        })
        _ = pipeline.handle(.armSucceeded, at: 0)
        _ = pipeline.handle(.profileReady(SpeakerID("S1"), version: 1), at: 0)
        let source = direction.source == .english ? "We will meet next week." : "我们下周开会。"
        let target = direction.target == .mandarin ? "我们下周开会。" : "We will meet next week."
        let hypothesis = ASRHypothesis(utterance: 1, tokens: [ASRToken(text: source)],
                                       isFinal: true, audioFrontier: 48_000)
        let committed = pipeline.handle(.hypothesis(hypothesis), at: 1_000_000_000)
        #expect(committed.contains { if case .translate(let request) = $0 { request.uncommittedSource == source } else { false } })
        let effects = pipeline.handle(.translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: target))),
                                      at: 1_100_000_000)
        #expect(effects.contains { if case .synthesize(let job) = $0 { job.text == target } else { false } })
        #expect(effects.contains(.caption(kind: "target.final", id: UnitID(1).description, text: target)))
    }

    @Test func englishLengthCapKeepsWordsWhole() {
        for (text, cap) in [("We will discuss commercial quality tomorrow", 15),
                            ("Please don't stop testing tomorrow", 10)] {
            var config = PipelineConfig(session: "english", direction: .englishToChinese).commit
            config.maxUnitKeys = cap
            var policy = CommitPolicy(config: config)
            let partial = ASRHypothesis(utterance: 1, tokens: [ASRToken(text: text)], isFinal: false, audioFrontier: 48_000)
            _ = policy.process(partial, at: 0)
            let units = policy.process(partial, at: 100_000_000).compactMap {
                if case .commit(let unit) = $0 { unit } else { nil }
            }
            #expect(!units.isEmpty)
            let words = Set(text.split(separator: " ").map(String.init))
            #expect(units.allSatisfy { $0.text.split(separator: " ").allSatisfy { words.contains(String($0)) } })
        }
    }
}
