// Turn fill (ADR-0013): with a picture delay, a phrase that is still ahead of its slot may be
// slowed so that it ends where the actor stops. A phrase already late for its slot, a phrase with
// no slot at all, and a phrase whose timing is only estimated never get it.

import A2ACore
import Testing

private let speaker = SpeakerID("S1")
private let rate = Double(canonicalSampleRate)

private struct Session {
    var p: SessionPipeline
    var effects: [PipelineEffect] = []
    private var captured: Int64 = 0

    init(delay: Double, fillEnabled: Bool = true) {
        var config = PipelineConfig(session: "turn-fill")
        config.finishPhraseBeforeCatchUp = true
        config.originalAudioEnabled = false
        config.health.maxDebtSeconds = 30
        config.health.maxSourceLagSeconds = 30
        config.presentationDelaySeconds = delay
        config.turnFillEnabled = fillEnabled
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

    /// Speech from `start` to `end`, recognized at `commitAt` and answered 100 ms later. `timed`
    /// false stands for a recognizer without token timing, whose spans are only estimated.
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

    /// Whether the reducer marked this unit's `tts.request` as fillable.
    func requestedFill(_ id: Int) -> Bool {
        effects.contains { effect in
            guard case let .trace(event) = effect, event.kind == .ttsRequest,
                  event.value("unit") == .string("u\(id)") else { return false }
            return event.value("fill") == .bool(true)
        }
    }
}

@Suite struct TurnFillTests {
    @Test func aPhraseAheadOfItsSlotAsksToBeFilledToTheActor() throws {
        var s = Session(delay: 8)
        let offered = s.speak(1, from: 0, to: 2.5, at: 2.8)
        let job = try #require(offered)
        #expect(job.targetSeconds == 2.5)
        #expect(job.allowFill)
        #expect(s.requestedFill(1))
    }

    @Test func livePlaybackWithoutASlotNeverSlowsALine() throws {
        var s = Session(delay: 0)
        let offered = s.speak(1, from: 0, to: 2.5, at: 2.8)
        let job = try #require(offered)
        #expect(job.targetSeconds == 2.5)
        #expect(!job.allowFill)
        #expect(!s.requestedFill(1))
    }

    @Test func aPhraseAlreadyLateForItsSlotKeepsItsOwnPace() throws {
        // D = 1 s: the slot of speech that started at 0 passed long before the answer landed.
        // Stretching it would only spend more time on a line that is already behind the picture.
        var s = Session(delay: 1)
        let offered = s.speak(1, from: 0, to: 2.5, at: 3.0)
        let job = try #require(offered)
        #expect(job.targetSeconds == 2.5)
        #expect(!job.allowFill)
        #expect(!s.requestedFill(1))
    }

    @Test func estimatedTimingPacesAgainstNothing() throws {
        var s = Session(delay: 8)
        let offered = s.speak(1, from: 0, to: 2.5, at: 2.8, timed: false)
        let job = try #require(offered)
        #expect(job.targetSeconds == nil)
        #expect(!job.allowFill)
        #expect(!s.requestedFill(1))
    }

    @Test func theEscapeHatchLeavesEveryTakeAtItsOwnPace() throws {
        // `a2a-pipeline --no-turn-fill`: the same slotted session, for a listener to compare.
        var s = Session(delay: 8, fillEnabled: false)
        let offered = s.speak(1, from: 0, to: 2.5, at: 2.8)
        let job = try #require(offered)
        #expect(job.targetSeconds == 2.5)
        #expect(!job.allowFill)
        #expect(!s.requestedFill(1))
    }

    @Test func eachClauseIsFilledToItsOwnSpeechEnd() throws {
        // A clause of one turn: its own slot and its own actor end, so a short English clause is
        // filled to its own pause rather than to the end of the whole turn.
        var s = Session(delay: 8)
        let offered = s.speak(1, from: 0, to: 1.2, at: 1.5)
        let first = try #require(offered)
        #expect(first.targetSeconds == 1.2 && first.allowFill)
    }
}
