// Multi-speaker sessions (PipelineConfig.multiSpeaker, `a2a-pipeline --speakers auto`, ADR-0005):
// each unit takes the speaker who covers its span, turns never fall back, and a unit whose
// speaker has no voice yet waits for it.

import A2ACore
import Testing

private let s1 = SpeakerID("S1")
private let s2 = SpeakerID("S2")
private let rate = 48_000.0

private func ns(_ seconds: Double) -> UInt64 { UInt64((seconds * 1e9).rounded()) }
private func span(_ start: Double, _ end: Double) -> SourceSpan {
    SourceSpan(start: Int64(start * rate), end: Int64(end * rate))
}

private struct Dialogue {
    var p: SessionPipeline
    var effects: [PipelineEffect] = []
    private var captured: Int64 = 0

    init(fluent: Bool = true, voices: [SpeakerID] = [s1, s2], locked: Bool = false, attributed: Bool = false,
         carry: Double? = nil) {
        var config = PipelineConfig(session: "dialogue")
        config.multiSpeaker = true
        config.pinSpeakerProfiles = locked
        config.requireSpeakerAttribution = attributed
        if let carry { config.unlabeledSpeakerCarrySeconds = carry }
        config.finishPhraseBeforeCatchUp = fluent
        config.originalAudioEnabled = false
        p = SessionPipeline(config: config)
        run(0, .arm)
        run(0, .armSucceeded)
        for v in voices { run(0, .profileReady(v, version: 1)) }
        run(0, .vad(.speechStart(0)))
    }

    @discardableResult
    mutating func run(_ t: Double, _ input: PipelineInput) -> [PipelineEffect] {
        let frontier = Int64(t * rate)
        if frontier > captured {
            effects += p.handle(.captured(span: SourceSpan(start: captured, end: frontier)), at: ns(t))
            captured = frontier
        }
        let out = p.handle(input, at: ns(t))
        effects += out
        return out
    }

    mutating func label(_ speaker: SpeakerID, from start: Double, to end: Double, at t: Double) {
        run(t, .speaker(.assign(speaker, span(start, end))))
    }

    /// A VAD pause: the tracker ends the stretch it has been labeling, so the next speech is
    /// a new turn and needs its own label.
    mutating func pause(afterSeconds seconds: Double, at t: Double) {
        run(t, .vad(.speechEnd(Int64(seconds * rate))))
    }

    @discardableResult
    mutating func commit(_ id: Int, from start: Double, to end: Double, at t: Double) -> [PipelineEffect] {
        let unitSpan = span(start, end)
        let committed = run(t, .hypothesis(ASRHypothesis(utterance: id, tokens: [ASRToken(text: "测试第\(id)句。", span: unitSpan)],
                                                         isFinal: true, audioFrontier: unitSpan.end)))
        return committed + run(t + 0.1, .translation(UnitID(id), .success(TranslationCandidate(unitID: UnitID(id), text: "Sentence \(id)."))))
    }

    @discardableResult
    mutating func pcm(_ id: Int, speaker: SpeakerID, seconds: Double, at t: Double) -> [PipelineEffect] {
        let header = PCMChunkHeader(unitID: UnitID(id), speakerID: speaker, profileVersion: 1, sessionEpoch: 1,
                                    playoutGeneration: 1, format: PCMFormat(sampleRate: 24_000, channelCount: 1),
                                    span: SourceSpan(start: 0, end: 0), isLast: false)
        return run(t, .pcm(header, frames: Int(seconds * 24_000)))
    }

    func jobs(in effects: [PipelineEffect]? = nil) -> [SynthesisJob] {
        (effects ?? self.effects).compactMap { if case .synthesize(let j) = $0 { j } else { nil } }
    }

    func traces(_ kind: TraceKind) -> [TraceEvent] {
        effects.compactMap { if case .trace(let e) = $0, e.kind == kind { e } else { nil } }
    }
}

@Suite struct MultiSpeakerTests {
    @Test func returningActorsKeepTheirFirstVoiceDespiteLaterProfileEvents() {
        var d = Dialogue(locked: true, attributed: true)
        let turns = [s1, s2, s1, s2]
        for (i, actor) in turns.enumerated() {
            let start = Double(i * 2) + 0.2
            d.label(actor, from: start, to: start + 1.0, at: start + 1.1)
            if i >= 2 { d.run(start + 1.2, .profileReady(actor, version: 2)) }
            d.commit(i + 1, from: start, to: start + 1.0, at: start + 1.3)
        }
        #expect(d.jobs().map(\.speaker) == turns)
        #expect(d.jobs().map(\.profileVersion) == [1, 1, 1, 1])
        #expect(d.traces(.profileReady).count == 2)
    }

    @Test func translationThatBeatsTheSpeakerLabelWaitsForTheReturningActor() {
        var d = Dialogue(locked: true, attributed: true)
        d.label(s2, from: 0.2, to: 1, at: 1.1)
        d.pause(afterSeconds: 1.2, at: 1.15)  // the labeled stretch ended: the next turn is unknown
        d.commit(1, from: 1.5, to: 3.1, at: 3.2)
        #expect(d.jobs().isEmpty)
        // A profile-ready event for the previous actor must not release the guess.
        d.run(3.25, .profileReady(s2, version: 2))
        #expect(d.jobs().isEmpty)
        d.label(s1, from: 1.5, to: 3.1, at: 3.4)
        #expect(d.jobs().map(\.speaker) == [s1])
        #expect(d.jobs().map(\.profileVersion) == [1])
    }

    @Test func aBriefReplyJustAfterAPauseContinuesThePreviousSpeaker() {
        // Measured: most unlabeled pieces are short replies a moment after a pause, and at
        // 3 s / 1 s limits no real turn was mislabeled by carrying them.
        var d = Dialogue(locked: true, attributed: true)
        d.label(s2, from: 0.2, to: 1, at: 1.1)
        d.pause(afterSeconds: 1.1, at: 1.2)
        d.commit(1, from: 1.4, to: 2.1, at: 2.2)   // 0.7 s, 0.4 s after the pause
        #expect(d.jobs().map(\.speaker) == [s2])
        #expect(d.traces(.commitTarget).contains { $0.jsonLine().contains("\"speaker_carried\":true") })
    }

    @Test func aBriefReplyTooFarAfterAPauseStillWaitsForItsOwnLabel() {
        var d = Dialogue(locked: true, attributed: true)
        d.label(s2, from: 0.2, to: 1, at: 1.1)
        d.pause(afterSeconds: 1.1, at: 1.2)
        d.commit(1, from: 4.6, to: 5.3, at: 5.4)   // 3.6 s after that label ended
        #expect(d.jobs().isEmpty)
    }

    @Test func anUnlabeledTurnAfterAPauseExpiresInsteadOfBorrowingThePreviousActorsVoice() {
        var d = Dialogue(locked: true, attributed: true)
        d.label(s2, from: 0.2, to: 1, at: 1.1)
        // The labeled stretch ended, and what follows is a turn of its own (1.6 s): whoever
        // speaks has to be identified again.
        d.pause(afterSeconds: 1.2, at: 1.3)
        d.commit(1, from: 1.5, to: 3.1, at: 3.2)
        d.run(7.6, .tick)   // the source-lag limit for that piece has run out
        #expect(d.jobs().isEmpty)
        #expect(d.traces(.sourceDisposition).contains { $0.jsonLine().contains("\"reason\":\"no_speaker\"") })
        d.label(s1, from: 1.5, to: 3.1, at: 7.7)
        #expect(d.jobs().isEmpty)   // an expired line is never voiced late
    }

    @Test func speechWithNoPauseInheritsTheSpeakerOfTheLabeledPiece() {
        // The tracker labels a 3 s piece of a long turn and stays silent on the next: the
        // voiceprint of the labeled piece covers the whole uninterrupted stretch.
        var d = Dialogue(locked: true, attributed: true)
        d.label(s2, from: 0.2, to: 3, at: 3.1)
        d.commit(1, from: 3, to: 6, at: 6.1)
        #expect(d.jobs().map(\.speaker) == [s2])
        #expect(d.traces(.commitTarget).contains { $0.jsonLine().contains("\"speaker_carried\":true") })
        // …and the piece after that, while the stretch is still within the carry limit.
        d.commit(2, from: 6, to: 9, at: 9.1)
        #expect(d.jobs().map(\.speaker) == [s2, s2])
    }

    @Test func aLaterLabelNeverFlowsBackIntoAnEarlierUnlabeledPiece() {
        var d = Dialogue(locked: true, attributed: true)
        d.commit(1, from: 0.2, to: 3, at: 3.1)
        #expect(d.jobs().isEmpty)
        d.label(s2, from: 3, to: 6, at: 6.1)
        #expect(d.jobs().isEmpty)
    }

    @Test func theCarryEndsAtItsLimitAndThePieceWaitsAgain() {
        var d = Dialogue(locked: true, attributed: true)
        d.label(s2, from: 0.2, to: 3, at: 3.1)
        d.commit(1, from: 12, to: 15, at: 15.1)   // 9 s of unlabeled speech since the label
        #expect(d.jobs().isEmpty)
        d.run(20.1, .tick)   // the wait for a label of its own has run out
        #expect(d.traces(.sourceDisposition).contains { $0.jsonLine().contains("\"reason\":\"no_speaker\"") })
    }

    @Test func theCarryIsSwitchableOffForComparingRuns() {
        var d = Dialogue(locked: true, attributed: true, carry: 0)
        d.label(s2, from: 0.2, to: 3, at: 3.1)
        d.commit(1, from: 3, to: 6, at: 6.1)
        #expect(d.jobs().isEmpty)
    }

    @Test func anUnrelatedTurnCannotReassignAUnitWaitingForItsOwnVoice() {
        var d = Dialogue(voices: [s1], locked: true, attributed: true)
        d.label(s2, from: 0.2, to: 1.2, at: 1.3)
        d.commit(1, from: 0.2, to: 1.2, at: 1.4)
        #expect(d.jobs().isEmpty)
        // A later, disjoint span cannot reassign this waiting unit to S1.
        d.label(s1, from: 1.5, to: 2.5, at: 2.6)
        #expect(d.jobs().isEmpty)
        d.run(2.7, .profileReady(s2, version: 1))
        #expect(d.jobs().map(\.speaker) == [s2])
    }

    @Test func aSmallOrTiedLabeledShareCannotSelectThePreviousActor() {
        var d = Dialogue(locked: true, attributed: true)
        d.label(s2, from: 0.2, to: 0.7, at: 1.3)
        d.commit(1, from: 0.2, to: 1.2, at: 1.4)
        #expect(d.jobs().isEmpty)
        d.label(s1, from: 0.7, to: 1.2, at: 1.6)
        #expect(d.jobs().isEmpty)
    }

    @Test func aSourceDiscontinuityAllowsANewSessionVoice() {
        var d = Dialogue(locked: true, attributed: true)
        d.run(0.1, .discontinuity("test"))
        d.run(0.2, .profileReady(s1, version: 2))
        d.label(s1, from: 0.3, to: 1.3, at: 1.4)
        d.commit(1, from: 0.3, to: 1.3, at: 1.5)
        #expect(d.jobs().map(\.profileVersion) == [2])
    }

    @Test func eachUnitIsVoicedByTheSpeakerWhoSaidIt() {
        var d = Dialogue()
        d.label(s1, from: 0.2, to: 2.0, at: 2.3)
        d.commit(1, from: 0.2, to: 2.0, at: 2.6)
        d.label(s2, from: 2.4, to: 4.0, at: 4.3)
        d.commit(2, from: 2.4, to: 4.0, at: 4.6)
        #expect(d.jobs().map(\.speaker) == [s1, s2])
        let committed = d.traces(.commitSource).map { $0.jsonLine() }
        #expect(committed.count == 2 && committed[0].contains("\"spk\":\"S1\"") && committed[1].contains("\"spk\":\"S2\""))
    }

    @Test func aUnitCommittedBeforeItsLabelTakesTheLabelBeforeSynthesis() {
        var d = Dialogue()
        d.label(s1, from: 0.2, to: 2.0, at: 2.3)
        // Committed while S2's segment is still open: the guess is S1, but the label lands
        // before translation returns.
        d.run(3.5, .hypothesis(ASRHypothesis(utterance: 2, tokens: [ASRToken(text: "好。", span: span(2.4, 3.4))],
                                             isFinal: true, audioFrontier: span(2.4, 3.4).end)))
        d.label(s2, from: 2.4, to: 3.4, at: 3.55)
        d.run(3.6, .translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "Fine."))))
        #expect(d.jobs().map(\.speaker) == [s2])
    }

    @Test func speakerTurnsNeverFallBackOrCutThePhrase() {
        var d = Dialogue()
        d.label(s1, from: 0.2, to: 2.0, at: 2.3)
        d.commit(1, from: 0.2, to: 2.0, at: 2.6)
        d.pcm(1, speaker: s1, seconds: 1, at: 2.8)
        d.run(2.9, .playStarted(UnitID(1), outSample: 0))
        let before = d.effects.count
        d.label(s2, from: 2.4, to: 4.0, at: 4.3)
        d.commit(2, from: 2.4, to: 4.0, at: 4.6)
        let turn = Array(d.effects[before...])
        #expect(!turn.contains(.flushTranslatedBus))
        #expect(!turn.contains { if case .cancelSynthesis = $0 { true } else { false } })
        #expect(!turn.contains { if case .rampGains = $0 { true } else { false } })
        #expect(d.traces(.fallbackEnter).isEmpty)
        #expect(d.traces(.spkChange).count == 1)
        if case .translating = d.p.state {} else { Issue.record("left translating: \(d.p.state)") }
        // S2's phrase waits behind S1's and follows it.
        d.pcm(2, speaker: s2, seconds: 1, at: 4.8)
        #expect(d.run(5.0, .playEnded(UnitID(1), outSample: 48_000)).contains(.releaseHeld(UnitID(2))))
    }

    @Test func aNewSpeakersUnitWaitsForTheirVoice() {
        var d = Dialogue(voices: [s1])
        d.label(s1, from: 0.2, to: 2.0, at: 2.3)
        d.commit(1, from: 0.2, to: 2.0, at: 2.6)
        d.label(s2, from: 2.4, to: 4.0, at: 4.3)
        let waiting = d.commit(2, from: 2.4, to: 4.0, at: 4.6)
        #expect(d.jobs(in: waiting).isEmpty)
        #expect(!d.traces(.sourceDisposition).contains { $0.jsonLine().contains("no_profile") })
        let ready = d.run(5.0, .profileReady(s2, version: 1))
        #expect(d.jobs(in: ready).map(\.unit) == [UnitID(2)])
        #expect(d.jobs(in: ready).first?.speaker == s2)
    }

    @Test func aVoiceThatNeverArrivesExpiresTheWaitingUnit() {
        var d = Dialogue(voices: [s1])
        d.label(s2, from: 0.2, to: 2.0, at: 2.3)
        d.commit(1, from: 0.2, to: 2.0, at: 2.6)
        d.run(5.9, .tick)
        #expect(!d.traces(.sourceDisposition).contains { $0.jsonLine().contains("no_profile") })
        d.run(6.1, .tick)
        let expired = d.traces(.sourceDisposition).filter { $0.jsonLine().contains("\"reason\":\"no_profile\"") }
        #expect(expired.count == 1)
        #expect(d.jobs(in: d.run(6.2, .profileReady(s2, version: 1))).isEmpty)
    }

    @Test func theFirstReadyUnitOfAnySpeakerStartsTranslation() {
        var d = Dialogue(voices: [s2])
        d.label(s2, from: 0.2, to: 2.0, at: 2.3)
        d.commit(1, from: 0.2, to: 2.0, at: 2.6)
        // The session was acquiring S1's voice (VAD speech before any label).
        #expect(d.pcm(1, speaker: s2, seconds: 0.5, at: 2.8).contains(.releaseHeld(UnitID(1))))
        if case .translating(let spk) = d.p.state { #expect(spk == s2) } else { Issue.record("not translating: \(d.p.state)") }
    }

    @Test func coverageReportsTheDominantSpeakerAndLabeledShare() {
        var d = Dialogue()
        d.label(s1, from: 0, to: 1, at: 1.1)
        d.label(s2, from: 1.2, to: 4, at: 4.1)
        let c = d.p.speakerCoverage(of: span(0, 4))
        #expect(c?.speaker == s2)
        #expect(abs((c?.share ?? 0) - 0.7) < 1e-6)
        #expect(abs((c?.labeled ?? 0) - 0.95) < 1e-6)
        #expect(d.p.speaker(for: span(4.5, 5)) == s2)
        #expect(d.p.speakerCoverage(of: span(4.5, 5)) == nil)
    }

    @Test func speechTheTrackerNeverLabelsStillPlaysInTheDefaultVoice() {
        // Loud music, shouting above the pitch range, or quiet input: no `.speaker` input at all.
        var d = Dialogue(voices: [s1])
        d.commit(1, from: 0.2, to: 2.0, at: 2.6)
        d.commit(2, from: 2.4, to: 4.0, at: 4.6)
        #expect(d.jobs().map(\.speaker) == [s1, s1])
        #expect(d.pcm(1, speaker: s1, seconds: 0.5, at: 2.8).contains(.releaseHeld(UnitID(1))))
    }

    @Test func aSpeakersCastVoiceReplacesTheDefaultForLaterUnits() {
        var d = Dialogue(voices: [s1])
        d.commit(1, from: 0.2, to: 2.0, at: 2.6)
        d.label(s1, from: 2.4, to: 4.0, at: 4.3)
        d.run(4.4, .profileReady(s1, version: 2))
        d.commit(2, from: 2.4, to: 4.0, at: 4.6)
        #expect(d.jobs().map(\.profileVersion) == [1, 2])
    }
}
