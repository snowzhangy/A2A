// Picture delay D (PipelineConfig.presentationDelaySeconds, `a2a-pipeline --presentation-delay-ms`,
// ADR-0012): the host shows the source picture D seconds late, so each unit plays in its own
// slot (source start + D), lag limits count from that slot, and early audio is not debt.

import A2ACore
import Testing

private let speaker = SpeakerID("S1")
private let rate = Double(canonicalSampleRate)

private func ns(_ seconds: Double) -> UInt64 { UInt64((seconds * 1e9).rounded()) }

/// Live-like timeline: capture advances with event time, as in FluentPlayoutTests.
private struct Session {
    var p: SessionPipeline
    var effects: [PipelineEffect] = []
    private var captured: Int64 = 0

    init(delay: Double, maxDebt: Double = 3, maxLag: Double = 4, maxPending: Int = 8) {
        var config = PipelineConfig(session: "picture-delay")
        config.finishPhraseBeforeCatchUp = true
        config.originalAudioEnabled = false
        config.health.maxDebtSeconds = maxDebt
        config.health.maxSourceLagSeconds = maxLag
        config.maxPendingUnits = maxPending
        config.presentationDelaySeconds = delay
        p = SessionPipeline(config: config)
        run(0, .arm)
        run(0, .armSucceeded)
        run(0, .profileReady(speaker, version: 1))
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

    mutating func commit(_ id: Int, from start: Double, to end: Double, at t: Double) {
        let span = SourceSpan(start: Int64(start * rate), end: Int64(end * rate))
        run(t, .hypothesis(ASRHypothesis(utterance: id, tokens: [ASRToken(text: "测试第\(id)句。", span: span)],
                                         isFinal: true, audioFrontier: span.end)))
        run(t + 0.1, .translation(UnitID(id), .success(TranslationCandidate(unitID: UnitID(id), text: "Sentence \(id)."))))
    }

    /// One complete synthesized phrase of `seconds`.
    mutating func synthesize(_ id: Int, seconds: Double, at t: Double) {
        let header = PCMChunkHeader(unitID: UnitID(id), speakerID: speaker, profileVersion: 1, sessionEpoch: 1,
                                    playoutGeneration: p.gate.generation,
                                    format: PCMFormat(sampleRate: 24_000, channelCount: 1),
                                    span: SourceSpan(start: 0, end: 0), isLast: false)
        run(t, .pcm(header, frames: Int(seconds * 24_000)))
        run(t, .synthesisFinished(UnitID(id), generation: p.gate.generation))
    }

    @discardableResult
    mutating func tick(_ t: Double, backlog: Double = 0) -> [PipelineEffect] {
        run(t, .translatedBacklog(seconds: backlog)) + run(t, .tick)
    }

    /// Event time at which `unit` was released to the bus, scanning 20 ms driver steps.
    mutating func releaseTime(_ id: Int, from start: Double, to end: Double) -> Double? {
        var t = start
        while t <= end {
            // Capture advancing to `t` is itself an input, and can release the unit.
            let before = effects.count
            tick(t)
            if effects[before...].contains(.releaseHeld(UnitID(id))) { return t }
            t += 0.02
        }
        return nil
    }

    func traces(_ kind: TraceKind) -> [TraceEvent] {
        effects.compactMap { if case .trace(let e) = $0, e.kind == kind { e } else { nil } }
    }

    func canceled(_ reason: String) -> [String] {
        traces(.unitCancel).filter { $0.jsonLine().contains("\"reason\":\"\(reason)\"") }
            .compactMap { if case .string(let u)? = $0.value("unit") { u } else { nil } }
    }

    func lateMs(_ id: Int) -> Int? {
        for e in traces(.playScheduled) where e.value("unit") == .string("u\(id)") {
            if case .int(let ms)? = e.value("late_ms") { return Int(ms) }
        }
        return nil
    }
}

@Suite struct PictureDelayTests {
    @Test func theFirstPhraseWaitsForItsSlotAlthoughItWasReadyEarly() {
        var s = Session(delay: 8)
        s.commit(1, from: 0, to: 2, at: 2.1)
        s.synthesize(1, seconds: 2, at: 3.0)
        // Ready 5 s before its slot: a live session would start now, ahead of the picture.
        #expect(!s.effects.contains(.releaseHeld(UnitID(1))))
        let released = s.releaseTime(1, from: 3.02, to: 9)
        #expect(released != nil)
        #expect(abs((released ?? 0) - 8.0) <= 0.021)
        #expect(s.p.state == .translating(speaker))
        #expect((s.lateMs(1) ?? 999) >= 0 && (s.lateMs(1) ?? 999) <= 20)
    }

    @Test func aLaterPhraseWaitsForItsSlotEvenWhenTheBusIsFree() {
        var s = Session(delay: 8)
        s.commit(1, from: 0, to: 1, at: 1.1)
        s.synthesize(1, seconds: 1, at: 1.5)
        s.commit(2, from: 3, to: 4, at: 4.1)
        s.synthesize(2, seconds: 1, at: 4.5)
        #expect(s.releaseTime(1, from: 1.52, to: 8.5) != nil)
        s.run(8.05, .playStarted(UnitID(1), outSample: 0))
        s.run(9.05, .playEnded(UnitID(1), outSample: 48_000))
        // The bus is empty from 9.05 s, but unit 2's source started at 3 s: slot 11 s.
        let released = s.releaseTime(2, from: 9.06, to: 12)
        #expect(abs((released ?? 0) - 11.0) <= 0.021)
    }

    @Test func audioWaitingForItsSlotIsNotDebtAndNothingExpiresEarly() {
        // Live limits would skip all of this: 6 s of ready speech is twice the debt limit,
        // and every source is older than the 4 s lag limit long before its 10 s slot.
        var s = Session(delay: 10, maxDebt: 3, maxLag: 4)
        s.commit(1, from: 0, to: 2, at: 2.1)
        s.synthesize(1, seconds: 2, at: 2.5)
        s.commit(2, from: 2, to: 4, at: 4.1)
        s.synthesize(2, seconds: 2, at: 4.5)
        s.commit(3, from: 4, to: 6, at: 6.1)
        s.synthesize(3, seconds: 2, at: 6.5)
        var t = 6.6
        while t < 9.9 { s.tick(t); t += 0.1 }
        #expect(s.canceled("catch_up").isEmpty)
        #expect(s.canceled("source_expired").isEmpty)
        #expect(s.canceled("queue_limit").isEmpty)
        #expect(s.releaseTime(1, from: 9.9, to: 10.1) != nil)
    }

    @Test func aPhraseMissingItsSlotByMoreThanTheLagLimitIsSkipped() {
        var s = Session(delay: 8, maxLag: 1)
        s.commit(1, from: 0, to: 1, at: 1.1)
        s.synthesize(1, seconds: 1, at: 1.5)
        #expect(s.releaseTime(1, from: 1.52, to: 8.5) != nil)
        s.run(8.05, .playStarted(UnitID(1), outSample: 0))
        s.run(9.05, .playEnded(UnitID(1), outSample: 48_000))
        // Unit 2 never gets audio; unit 3 behind it keeps the newest slot alive.
        s.commit(2, from: 1, to: 2, at: 2.1)
        s.commit(3, from: 2, to: 3, at: 3.1)
        var t = 9.1
        while t < 10.9 { s.tick(t); t += 0.1 }
        // Slot of unit 2's end is 10 s; 1 s of lateness is tolerated.
        #expect(s.canceled("catch_up").isEmpty)
        while t < 11.3 { s.tick(t); t += 0.1 }
        #expect(s.canceled("catch_up") == ["u2"])
    }

    @Test func theQueueHoldsTheDelayedSecondsOfDialogue() {
        var s = Session(delay: 8, maxPending: 2)
        for id in 1...6 { s.commit(id, from: Double(id - 1), to: Double(id), at: Double(id) + 0.1) }
        #expect(s.canceled("queue_limit").isEmpty)
    }

    @Test func noPictureDelayKeepsLivePlayback() {
        var s = Session(delay: 0)
        s.commit(1, from: 0, to: 2, at: 2.1)
        s.synthesize(1, seconds: 2, at: 3.0)
        #expect(s.effects.contains(.releaseHeld(UnitID(1))))
        #expect(s.lateMs(1) == nil)
    }
}
