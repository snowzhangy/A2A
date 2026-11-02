// Delivery conditioning (ADR-0015): a unit may be offered to the synthesizer with its own source
// audio appended to the speaker's reference, so the take keeps how the actor said that line.
// The reducer owns the permission; the worker owns whether the clip can be appended. These are the
// reducer's rules: asked for explicitly, real speech timing, and the words the recognizer heard are
// available to pair with the audio.

import A2ACore
import Testing

private let speaker = SpeakerID("S1")
private let rate = Double(canonicalSampleRate)

private struct Session {
    var p: SessionPipeline
    var effects: [PipelineEffect] = []
    private var captured: Int64 = 0

    init(delivery: Bool) {
        var config = PipelineConfig(session: "delivery")
        config.finishPhraseBeforeCatchUp = true
        config.originalAudioEnabled = false
        config.health.maxDebtSeconds = 30
        config.health.maxSourceLagSeconds = 30
        config.cloneDeliveryLine = delivery
        p = SessionPipeline(config: config)
        run(0, .arm)
        run(0, .armSucceeded)
        run(0, .profileReady(speaker, version: 1))
    }

    mutating func run(_ t: Double, _ input: PipelineInput) -> [PipelineEffect] {
        let frontier = Int64(t * rate)
        if frontier > captured {
            effects += p.handle(.captured(span: SourceSpan(start: captured, end: frontier)), at: UInt64(t * 1e9))
            captured = frontier
        }
        let out = p.handle(input, at: UInt64(t * 1e9))
        effects += out
        return out
    }

    @discardableResult
    mutating func speak(_ id: Int, from start: Double, to end: Double, at commitAt: Double,
                        timed: Bool = true) -> SynthesisJob? {
        let span = SourceSpan(start: Int64(start * rate), end: Int64(end * rate))
        run(commitAt, .vad(.speechStart(span.start)))
        run(commitAt, .vad(.speechEnd(span.end)))
        run(commitAt, .hypothesis(ASRHypothesis(utterance: id,
                                                tokens: [ASRToken(text: "测试第\(id)句。", span: timed ? span : nil)],
                                                isFinal: true, audioFrontier: span.end)))
        let before = effects.count
        run(commitAt + 0.1, .translation(UnitID(id), .success(TranslationCandidate(unitID: UnitID(id),
                                                                                   text: "Sentence \(id)."))))
        for case let .synthesize(job) in effects[before...] { return job }
        return nil
    }

    func requestedDelivery(_ id: Int) -> Bool {
        effects.contains { effect in
            guard case let .trace(event) = effect, event.kind == .ttsRequest,
                  event.value("unit") == .string("u\(id)") else { return false }
            return event.value("delivery") == .bool(true)
        }
    }
}

@Suite struct DeliveryConditioningTests {
    @Test func anAskedForLineWithRealTimingMayCarryItsOwnAudio() throws {
        var s = Session(delivery: true)
        let offered = s.speak(1, from: 0, to: 2.5, at: 2.8)
        let job = try #require(offered)
        #expect(job.deliveryAllowed)
        #expect(s.requestedDelivery(1))
    }

    @Test func itIsOffUntilSomebodyAsks() throws {
        // The default is the pinned profile: conditioning changes the character of every cloned
        // line, so a run gets it only with `--clone-delivery line`.
        var s = Session(delivery: false)
        let offered = s.speak(1, from: 0, to: 2.5, at: 2.8)
        let job = try #require(offered)
        #expect(!job.deliveryAllowed)
        #expect(!s.requestedDelivery(1))
    }

    @Test func anEstimatedSpanOffersNoDelivery() throws {
        // A forced split's span is a guess at a boundary, not a performed line: its audio would
        // condition the take on words that were never spoken together.
        var s = Session(delivery: true)
        let offered = s.speak(1, from: 0, to: 2.5, at: 2.8, timed: false)
        let job = try #require(offered)
        #expect(!job.deliveryAllowed)
        #expect(!s.requestedDelivery(1))
    }

    @Test func theWordsTheActorSaidTravelWithTheClip() throws {
        // In-context conditioning pairs reference audio with its transcript, so the driver needs
        // the recognized source words of the unit, not the English.
        var s = Session(delivery: true)
        s.speak(1, from: 0, to: 2.5, at: 2.8)
        #expect(s.p.sourceText(of: UnitID(1)) == "测试第1句。")
    }
}
