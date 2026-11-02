// Adapter contract tests (section 4.2). Fixture adapters always run; Apple adapters run
// only when explicitly enabled on the interactive runner.

import A2ACore
import A2AModels
import A2ALegacyModels
import Foundation
import Testing

private func frame(_ start: Int64, _ end: Int64, level: Float = 0) -> AudioFrame16k {
    let n = Int((end - start) / 3)
    return AudioFrame16k(span: SourceSpan(start: start, end: end), samples: [Float](repeating: level, count: n))
}

@Suite struct ScriptedRecognizerTests {
    let script = ASRScript(hypotheses: [
        .init(atSample: 9_600, utt: 1, text: "我们", final: false, tokens: nil),
        .init(atSample: 19_200, utt: 1, text: "我们开会。", final: true,
              tokens: [.init(text: "我们", start: 0, end: 4_800), .init(text: "开会。", start: 4_800, end: 9_600)]),
    ])

    @Test func neverEmitsBeforeAudioArrives() {
        let r = ScriptedRecognizer(script: script)
        r.accept(frame(0, 9_599))
        #expect(r.drainHypotheses().isEmpty)
        r.accept(frame(9_599, 9_600))
        let first = r.drainHypotheses()
        #expect(first.map(\.text) == ["我们"])
        #expect(first.first?.audioFrontier == 9_600)
    }

    @Test func drainKeepsFinalsWhenSeveralArriveTogether() {
        let r = ScriptedRecognizer(script: script)
        r.accept(frame(0, 24_000))
        let all = r.drainHypotheses()
        #expect(all.count == 2)
        #expect(all.last?.isFinal == true)
        #expect(all.last?.tokens.first?.span == SourceSpan(start: 0, end: 4_800))
        #expect(r.drainHypotheses().isEmpty)
    }
}

@Suite struct FixtureTranslatorAndSynthTests {
    @Test func dictionaryTranslatesClosedUnitsAndAdvertisesNoPrefix() async throws {
        let t = DictionaryTranslator(table: ["好的。": "Okay."])
        #expect(!t.supportsPrefix)
        let hit = try await t.translate(TranslationRequest(unitID: UnitID(1), context: [], glossary: [:], uncommittedSource: "好的。"))
        #expect(hit.text == "Okay.")
        let miss = try await t.translate(TranslationRequest(unitID: UnitID(2), context: [], glossary: [:], uncommittedSource: "没有"))
        #expect(miss.text.contains("Segment 2"))
    }

    @Test func toneSynthesizerStreamsBoundedChunksAndPreservesNoIdentity() async throws {
        let s = ToneSynthesizer()
        #expect(!s.capabilities.preservesSpeakerIdentity)
        #expect(s.capabilities.audioOutputStreaming && !s.capabilities.incrementalTextInput)
        let voice = PreparedVoice(speakerID: SpeakerID("S1"), profileVersion: 1, handle: 0)
        var frames = 0
        var chunks = 0
        for try await c in s.synthesize(unit: UnitID(1), text: "We will meet next week.", voice: voice) {
            #expect(c.frames <= 2_400)
            frames += c.frames
            chunks += 1
        }
        #expect(abs(Double(frames) / 24_000 - s.duration(of: "We will meet next week.")) < 0.01)
        #expect(chunks > 1)
    }
}

@Suite struct EnergyVADTests {
    @Test func detectsSpeechWithHangover() {
        var vad = EnergyVAD(thresholdDB: -40, minSpeechMs: 40, hangoverMs: 100)
        var events: [VADEvent] = []
        var t: Int64 = 0
        func feed(_ ms: Int, level: Float) {
            for _ in 0..<(ms / 20) {
                events += vad.process(frame(t, t + 960, level: level))
                t += 960
            }
        }
        feed(200, level: 0.0001)
        feed(400, level: 0.1)
        feed(60, level: 0.0001)    // shorter than hangover: still speech
        feed(200, level: 0.1)
        feed(300, level: 0.0001)
        #expect(events.count == 2)
        #expect(events.first == .speechStart(200 * 48))
        if case .speechEnd(let s) = events.last { #expect(s == Int64(860 * 48)) } else { Issue.record("no end") }
    }
}

@Suite(.enabled(if: ProcessInfo.processInfo.environment["A2A_RUN_APPLE_TESTS"] == "1"))
struct AppleAdapterContractTests {
    @Test func appleTranslatorRequiresInstalledAssets() async throws {
        do {
            try await AppleTranslator.checkInstalled()
        } catch AdapterError.assetsNotInstalled(let why) {
            // Recorded, not failed: runtime downloads are disabled by design (D8).
            print("skip: \(why)")
            return
        }
        let t = AppleTranslator()
        let c = try await t.translate(TranslationRequest(unitID: UnitID(1), context: [], glossary: [:], uncommittedSource: "我们下周开会。"))
        print("apple translation: \(c.text)")
        #expect(!c.text.isEmpty)
        #expect(TranslationValidator().evaluate(c, source: "我们下周开会。", previousTarget: nil).isAccepted)
    }

    @Test func appleSpeechRecognizerRequiresInstalledAssets() async throws {
        do {
            _ = try await AppleSpeechRecognizer.checkInstalled()
        } catch AdapterError.assetsNotInstalled(let why) {
            print("skip: \(why)")
            return
        }
        let r = try await AppleSpeechRecognizer()
        r.accept(frame(0, 48_000))
        await r.finish()
        #expect(r.lastError == nil)
    }
}
