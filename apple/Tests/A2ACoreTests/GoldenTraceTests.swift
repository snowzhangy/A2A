// Golden-trace tests: scripted inputs through SessionPipeline, compared byte-for-byte
// with Golden/<name>.jsonl. Regenerate with A2A_UPDATE_GOLDEN=1 and review the diff.

import A2ACore
import Foundation
import Testing

private let ms: UInt64 = 1_000_000
private let s1 = SpeakerID("S1")
private let ttsFormat = PCMFormat(sampleRate: 24_000, channelCount: 1)

/// Drives the reducer and records traces plus non-trace effects.
private struct Scenario {
    var pipeline: SessionPipeline
    var t: UInt64 = 0
    var lines: [String] = []
    var effects: [PipelineEffect] = []

    init() {
        var config = PipelineConfig(session: "golden")
        config.traceText = true
        pipeline = SessionPipeline(config: config)
    }

    mutating func at(_ milliseconds: UInt64, _ input: PipelineInput) {
        t = milliseconds * ms
        let out = pipeline.handle(input, at: t)
        for e in out {
            if case .trace(let ev) = e { lines.append(ev.jsonLine()) } else { effects.append(e) }
        }
    }

    mutating func partial(_ ms: UInt64, _ text: String, final: Bool = false, utt: Int = 1) {
        let frontier = Int64(ms) * 48
        at(ms, .hypothesis(ASRHypothesis(utterance: utt, tokens: text.map { ASRToken(text: String($0)) },
                                         isFinal: final, audioFrontier: frontier)))
    }

    func header(_ unit: Int, gen: PlayoutGeneration, epoch: SessionEpoch = 1, last: Bool = false) -> PCMChunkHeader {
        PCMChunkHeader(unitID: UnitID(unit), speakerID: s1, profileVersion: 1, sessionEpoch: epoch,
                       playoutGeneration: gen, format: ttsFormat, span: SourceSpan(start: 0, end: 0), isLast: last)
    }

    mutating func armed(profile: Bool = true) {
        at(0, .arm)
        at(0, .armSucceeded)
        if profile { at(0, .profileReady(s1, version: 1)) }
    }
}

private func checkGolden(_ name: String, _ lines: [String], filePath: String = #filePath) throws {
    let url = URL(fileURLWithPath: filePath).deletingLastPathComponent()
        .appendingPathComponent("Golden").appendingPathComponent("\(name).jsonl")
    let actual = lines.joined(separator: "\n") + "\n"
    if ProcessInfo.processInfo.environment["A2A_UPDATE_GOLDEN"] == "1" {
        try actual.write(to: url, atomically: true, encoding: .utf8)
        return
    }
    let expected = try String(contentsOf: url, encoding: .utf8)
    if expected != actual {
        let e = expected.split(separator: "\n", omittingEmptySubsequences: false)
        let a = actual.split(separator: "\n", omittingEmptySubsequences: false)
        let i = (0..<min(e.count, a.count)).first { e[$0] != a[$0] } ?? min(e.count, a.count)
        Issue.record("""
            Golden \(name) differs at line \(i + 1):
              expected: \(i < e.count ? String(e[i]) : "<end>")
              actual:   \(i < a.count ? String(a[i]) : "<end>")
            """)
    }
}

@Suite struct GoldenTraceTests {
    /// Prepared profile, one sentence, translated, synthesized, played.
    @Test func basicUnit() throws {
        var s = Scenario()
        s.armed()
        s.at(0, .captured(span: SourceSpan(start: 0, end: 4_800)))
        s.at(100, .vad(.speechStart(4_800)))
        s.partial(400, "我们下周")
        s.partial(700, "我们下周开会。")
        s.partial(900, "我们下周开会。")
        s.at(1_000, .translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "We will meet next week."))))
        s.at(1_200, .pcm(s.header(1, gen: 1), frames: 1_200))
        s.at(1_250, .pcm(s.header(1, gen: 1), frames: 2_400))
        s.at(1_300, .playStarted(UnitID(1), outSample: 62_400))
        s.at(1_600, .synthesisFinished(UnitID(1), generation: 1))
        s.at(1_700, .vad(.speechEnd(81_600)))
        s.partial(1_800, "我们下周开会。", final: true)
        s.at(2_900, .playEnded(UnitID(1), outSample: 141_600))
        s.at(3_000, .tick)
        try checkGolden("basic_unit", s.lines)
        #expect(s.pipeline.state == .translating(s1))
        #expect(s.effects.contains(.releaseHeld(UnitID(1))))
    }

    /// Debt pushes the session into fallback; stale-generation PCM is rejected; recovery
    /// happens only after a stable interval with a fresh unit.
    @Test func fallbackRejectsStaleGenerationAndRecovers() throws {
        var s = Scenario()
        s.armed()
        s.at(0, .captured(span: SourceSpan(start: 0, end: 48_000)))
        s.at(0, .vad(.speechStart(0)))
        s.partial(500, "第一句话。")
        s.partial(600, "第一句话。")
        s.at(700, .translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "First sentence."))))
        s.at(800, .pcm(s.header(1, gen: 1), frames: 4_800))
        s.partial(900, "第一句话。第二句话。")
        s.partial(1_000, "第一句话。第二句话。")
        s.at(1_100, .translation(UnitID(2), .success(TranslationCandidate(unitID: UnitID(2), text: "Second sentence."))))
        s.at(1_200, .translatedBacklog(seconds: 3.5))
        s.at(1_200, .tick)
        // Unit 2's PCM was generated for generation 1 and must be dropped.
        s.at(1_300, .pcm(s.header(2, gen: 1), frames: 4_800))
        s.at(1_400, .translatedBacklog(seconds: 0))
        s.at(1_400, .tick)
        s.partial(1_500, "第一句话。第二句话。第三句。")
        s.partial(1_600, "第一句话。第二句话。第三句。")
        s.at(1_700, .translation(UnitID(3), .success(TranslationCandidate(unitID: UnitID(3), text: "Third."))))
        s.at(1_800, .pcm(s.header(3, gen: 2), frames: 4_800))
        // Not yet stable for 2 s: stays degraded with unit 3 held.
        s.at(2_000, .tick)
        s.at(3_500, .tick)
        try checkGolden("fallback_recovery", s.lines)
        #expect(s.pipeline.state == .translating(s1))
        #expect(s.effects.contains(.dropPCM(s.header(2, gen: 1), reason: "stale_generation")))
        #expect(s.effects.contains(.flushTranslatedBus))
        #expect(s.effects.contains(.releaseHeld(UnitID(3))))
    }

    /// Without a profile the session stays on the original bus while the unit waits for a voice.
    @Test func acquisitionWithoutProfileKeepsOriginal() throws {
        var s = Scenario()
        s.armed(profile: false)
        s.at(0, .vad(.speechStart(0)))
        s.partial(300, "嗯，好的。", final: true)
        s.at(500, .translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "Okay."))))
        s.at(600, .tick)
        try checkGolden("acquisition_no_profile", s.lines)
        #expect(s.pipeline.state == .acquiringVoice(s1))
        #expect(!s.effects.contains { if case .synthesize = $0 { true } else { false } })
    }

    /// Discontinuity cancels in-flight units, advances the epoch, and rejects old-epoch PCM.
    @Test func discontinuityCancelsUnits() throws {
        var s = Scenario()
        s.armed()
        s.at(0, .vad(.speechStart(0)))
        s.partial(200, "这是测试。", final: true)
        s.at(300, .translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "This is a test."))))
        s.at(400, .discontinuity("device change"))
        s.at(500, .pcm(s.header(1, gen: 1, epoch: 1), frames: 2_400))
        s.at(600, .translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "late"))))
        try checkGolden("discontinuity", s.lines)
        #expect(s.pipeline.gate.epoch == 2)
        #expect(s.effects.contains(.cancelSynthesis(UnitID(1))))
    }

    /// A failed synthesis releases the unit everywhere: gate, held PCM, and disposition.
    @Test func synthesisFailureDiscardsHeldPCM() {
        var s = Scenario()
        s.armed()
        s.at(0, .vad(.speechStart(0)))
        s.partial(100, "第一。", final: true)
        s.at(200, .translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "First."))))
        s.at(300, .pcm(s.header(1, gen: 1), frames: 240))   // below prebuffer: held
        s.at(400, .synthesisFailed(UnitID(1), "model crashed"))
        #expect(s.effects.contains(.discardHeld(UnitID(1))))
        #expect(s.effects.contains(.cancelSynthesis(UnitID(1))))
        #expect(s.pipeline.gate.isCanceled(UnitID(1)))
        #expect(s.lines.last?.contains("\"disposition\":\"original_only\",\"reason\":\"tts_failed\"") == true)
        #expect(s.pipeline.state == .acquiringVoice(s1))
    }

    /// Translation validation rejects wrong-language and duplicate output.
    @Test func validationRejectsBadTranslations() throws {
        var s = Scenario()
        s.armed()
        s.at(0, .vad(.speechStart(0)))
        s.partial(100, "第一。", final: true, utt: 1)
        s.at(200, .translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "第一。"))))
        s.partial(300, "第二。", final: true, utt: 2)
        s.at(400, .translation(UnitID(2), .success(TranslationCandidate(unitID: UnitID(2), text: "Second."))))
        s.partial(500, "第三。", final: true, utt: 3)
        s.at(600, .translation(UnitID(3), .success(TranslationCandidate(unitID: UnitID(3), text: "second."))))
        s.partial(700, "第四。", final: true, utt: 4)
        s.at(800, .translation(UnitID(4), .failure("model error")))
        try checkGolden("validation", s.lines)
    }
}
