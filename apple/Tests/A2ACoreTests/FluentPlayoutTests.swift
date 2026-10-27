// Fluent playout (PipelineConfig.finishPhraseBeforeCatchUp, `a2a-pipeline --fluent`): the
// phrase being heard always finishes, later units wait behind it, and soft pressure skips
// unheard units instead of falling back.

import A2ACore
import Testing

private let speaker = SpeakerID("S1")
private let rate = 48_000.0

private func ns(_ seconds: Double) -> UInt64 { UInt64((seconds * 1e9).rounded()) }

/// Drives the reducer on a live-like timeline: capture advances with event time.
private struct Session {
    var p: SessionPipeline
    var effects: [PipelineEffect] = []
    private var captured: Int64 = 0

    init(fluent: Bool = true, originalAudio: Bool = false, prebuffer: Double = 0.08, maxDebt: Double = 3) {
        var config = PipelineConfig(session: "fluent")
        config.finishPhraseBeforeCatchUp = fluent
        config.originalAudioEnabled = originalAudio
        config.health.prebufferSeconds = prebuffer
        config.health.maxDebtSeconds = maxDebt
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

    /// Finalized source unit `id` covering `[start, end)` seconds, translated 0.1 s later.
    mutating func commit(_ id: Int, from start: Double, to end: Double, at t: Double) {
        let span = SourceSpan(start: Int64(start * rate), end: Int64(end * rate))
        run(t, .hypothesis(ASRHypothesis(utterance: id, tokens: [ASRToken(text: "测试第\(id)句。", span: span)],
                                         isFinal: true, audioFrontier: span.end)))
        run(t + 0.1, .translation(UnitID(id), .success(TranslationCandidate(unitID: UnitID(id), text: "Sentence \(id)."))))
    }

    @discardableResult
    mutating func pcm(_ id: Int, seconds: Double, at t: Double, generation: PlayoutGeneration = 1) -> [PipelineEffect] {
        let header = PCMChunkHeader(unitID: UnitID(id), speakerID: speaker, profileVersion: 1, sessionEpoch: 1,
                                    playoutGeneration: generation, format: PCMFormat(sampleRate: 24_000, channelCount: 1),
                                    span: SourceSpan(start: 0, end: 0), isLast: false)
        return run(t, .pcm(header, frames: Int(seconds * 24_000)))
    }

    /// The mixer reports `backlog` seconds still queued on the bus, then the 100 ms tick.
    @discardableResult
    mutating func tick(_ t: Double, backlog: Double) -> [PipelineEffect] {
        run(t, .translatedBacklog(seconds: backlog)) + run(t, .tick)
    }

    func traces(_ kind: TraceKind) -> [TraceEvent] {
        effects.compactMap { if case .trace(let e) = $0, e.kind == kind { e } else { nil } }
    }

    func canceled(_ reason: String) -> [String] {
        traces(.unitCancel).filter { $0.jsonLine().contains("\"reason\":\"\(reason)\"") }
            .compactMap { if case .string(let u)? = $0.value("unit") { u } else { nil } }
    }
}

@Suite struct FluentPlayoutTests {
    @Test func emptyBusDoesNotStarveByCancelingEveryInFlightSynthesis() {
        var s = Session(maxDebt: 0.3)
        s.commit(1, from: 0, to: 1, at: 1)
        s.pcm(1, seconds: 0.2, at: 1.2)
        s.run(1.3, .playStarted(UnitID(1), outSample: 0))
        s.run(1.4, .synthesisFinished(UnitID(1), generation: 1))
        s.run(1.5, .playEnded(UnitID(1), outSample: 9_600))
        s.commit(2, from: 1, to: 2, at: 2.2)
        s.commit(3, from: 2, to: 3, at: 3.2)
        s.commit(4, from: 3, to: 4, at: 4.2)
        let effects = s.tick(4.4, backlog: 0)
        #expect(!effects.contains(.cancelSynthesis(UnitID(2))))
        #expect(effects.contains(.cancelSynthesis(UnitID(3))))
        #expect(!effects.contains(.cancelSynthesis(UnitID(4))))
        #expect(s.pcm(2, seconds: 0.3, at: 4.5).contains(.releaseHeld(UnitID(2))))
    }

    @Test func longSessionPrunesTerminalBookkeepingAndStillRejectsLatePCM() {
        var s = Session()
        for id in 1...600 {
            let t = Double(id) * 1.2
            s.commit(id, from: Double(id - 1), to: Double(id), at: t)
            s.pcm(id, seconds: 0.1, at: t + 0.2)
            s.run(t + 0.3, .playStarted(UnitID(id), outSample: Int64(id - 1) * 4_800))
            s.run(t + 0.4, .synthesisFinished(UnitID(id), generation: 1))
            s.run(t + 0.5, .playEnded(UnitID(id), outSample: Int64(id) * 4_800))
        }
        func header(_ id: Int) -> PCMChunkHeader {
            PCMChunkHeader(unitID: UnitID(id), speakerID: speaker, profileVersion: 1, sessionEpoch: 1,
                           playoutGeneration: 1, format: PCMFormat(sampleRate: 24_000, channelCount: 1),
                           span: SourceSpan(start: 0, end: 4_800), isLast: false)
        }
        // Only recent terminal IDs need individual tombstones; old IDs coalesce into ranges.
        let retained = (1...600).filter { s.p.gate.admit(header($0)) == .alreadyFinished(UnitID($0)) }
        #expect(retained.count <= 256)
        #expect(retained.contains(600))
        #expect(s.p.gate.admit(header(1)) == .retiredUnit(UnitID(1)))
        let late = s.run(721, .pcm(header(1), frames: 2_400))
        #expect(late.contains { if case .dropPCM = $0 { true } else { false } })
        #expect(!late.contains { if case .enqueuePCM = $0 { true } else { false } })
        #expect(!late.contains { if case .holdPCM = $0 { true } else { false } })
    }

    @Test func everyPhraseWaitsForItsPrebuffer() {
        var s = Session(prebuffer: 0.24)
        s.commit(1, from: 0, to: 2, at: 2.5)
        s.pcm(1, seconds: 0.3, at: 2.7)
        s.run(2.8, .playStarted(UnitID(1), outSample: 0))
        s.run(2.9, .synthesisFinished(UnitID(1), generation: 1))
        s.run(3.0, .playEnded(UnitID(1), outSample: 14_400))
        s.commit(2, from: 2, to: 3, at: 3.1)
        let first = s.pcm(2, seconds: 0.1, at: 3.3)
        #expect(first.contains { if case .holdPCM = $0 { true } else { false } })
        #expect(!first.contains(.releaseHeld(UnitID(2))))
        #expect(!first.contains { if case .enqueuePCM = $0 { true } else { false } })
        #expect(!s.pcm(2, seconds: 0.1, at: 3.4).contains(.releaseHeld(UnitID(2))))
        #expect(s.pcm(2, seconds: 0.1, at: 3.5).contains(.releaseHeld(UnitID(2))))
    }

    @Test func shortCompletedPhraseDoesNotWaitForeverForPrebuffer() {
        var s = Session(prebuffer: 0.24)
        s.commit(1, from: 0, to: 2, at: 2.5)
        s.pcm(1, seconds: 0.3, at: 2.7)
        s.run(2.8, .playStarted(UnitID(1), outSample: 0))
        s.commit(2, from: 2, to: 3, at: 3.1)
        s.pcm(2, seconds: 0.05, at: 3.3)
        #expect(!s.run(3.4, .playEnded(UnitID(1), outSample: 14_400)).contains(.releaseHeld(UnitID(2))))
        #expect(s.run(3.5, .synthesisFinished(UnitID(2), generation: 1)).contains(.releaseHeld(UnitID(2))))
    }

    @Test func laterUnitsWaitBehindThePhraseBeingHeard() {
        var s = Session()
        s.commit(1, from: 0, to: 2, at: 2.5)
        #expect(s.pcm(1, seconds: 1, at: 2.7).contains(.releaseHeld(UnitID(1))))   // voice handoff
        s.run(2.8, .playStarted(UnitID(1), outSample: 0))
        s.commit(2, from: 2, to: 3, at: 3.2)
        let held = s.pcm(2, seconds: 1, at: 3.4)
        #expect(held.contains { if case .holdPCM = $0 { true } else { false } })
        #expect(!held.contains { if case .enqueuePCM = $0 { true } else { false } })
        let ended = s.run(4.8, .playEnded(UnitID(1), outSample: 96_000))
        #expect(ended.contains(.releaseHeld(UnitID(2))))
        // Later chunks of the released unit go straight to the bus.
        #expect(s.pcm(2, seconds: 0.5, at: 4.9).contains { if case .enqueuePCM = $0 { true } else { false } })
    }

    /// English longer than its source: the listener falls ever further behind unless unheard
    /// units are skipped. The phrase on the bus is never cut and the newest unit survives.
    @Test func softPressureSkipsUnheardUnitsWithoutCuttingThePhrase() {
        var s = Session()
        s.commit(1, from: 0, to: 2, at: 2.5)
        s.pcm(1, seconds: 4, at: 2.7)
        s.run(2.8, .playStarted(UnitID(1), outSample: 0))
        s.run(2.9, .synthesisFinished(UnitID(1), generation: 1))
        s.commit(2, from: 2, to: 4, at: 4.5)
        s.pcm(2, seconds: 4, at: 4.7)
        s.run(4.8, .synthesisFinished(UnitID(2), generation: 1))
        s.commit(3, from: 4, to: 6, at: 6.5)
        let caughtUp = s.tick(6.6, backlog: 0.9)
        #expect(s.p.state == .translating(speaker))
        #expect(s.canceled("catch_up") == ["u2"])
        #expect(!caughtUp.contains(.flushTranslatedBus))
        #expect(!s.effects.contains(.cancelSynthesis(UnitID(1))))
        #expect(s.traces(.catchUp).count == 1)
        #expect(s.traces(.fallbackEnter).isEmpty)
        // The newest unit is kept, scheduled after the phrase, and never skipped by later ticks.
        s.pcm(3, seconds: 4, at: 6.8)
        s.tick(6.9, backlog: 0.7)
        #expect(s.canceled("catch_up") == ["u2"])
        #expect(s.run(6.9, .playEnded(UnitID(1), outSample: 192_000)).contains(.releaseHeld(UnitID(3))))
    }

    /// The pre-fix gating suppressed all soft health while anything played, so back-to-back
    /// phrases let the queue grow until the hard cap flushed everything mid-phrase.
    @Test func continuousSpeechNeverReachesTheQueueCap() {
        var s = Session()
        var t = 2.5
        for id in 1...12 {
            s.commit(id, from: Double(id - 1) * 2, to: Double(id) * 2, at: t)
            s.pcm(id, seconds: 3, at: t + 0.2)
            s.run(t + 0.25, .synthesisFinished(UnitID(id), generation: 1))
            if id == 1 { s.run(t + 0.3, .playStarted(UnitID(1), outSample: 0)) }
            s.tick(t + 0.3, backlog: 1.5)
            t += 2
        }
        #expect(s.p.state == .translating(speaker))
        #expect(s.canceled("queue_limit").isEmpty)
        #expect(!s.canceled("catch_up").isEmpty)
        #expect(!s.effects.contains(.flushTranslatedBus))
    }

    @Test func englishOnlyPlaybackSkipsAMissingUnitInsteadOfFallingBack() {
        var s = Session()
        s.commit(1, from: 0, to: 2, at: 2.5)
        s.pcm(1, seconds: 2, at: 2.7)
        s.run(2.8, .playStarted(UnitID(1), outSample: 0))
        s.run(3.0, .hypothesis(ASRHypothesis(utterance: 2, tokens: [ASRToken(text: "第二句。")], isFinal: true,
                                             audioFrontier: 144_000)))
        let failed = s.run(3.1, .translation(UnitID(2), .failure("translation unavailable")))
        #expect(s.p.state == .translating(speaker))
        #expect(!failed.contains(.flushTranslatedBus))
        s.commit(3, from: 3, to: 4, at: 4.2)
        let synthFailed = s.run(4.4, .synthesisFailed(UnitID(3), "worker hiccup"))
        #expect(s.p.state == .translating(speaker))
        #expect(!synthFailed.contains(.flushTranslatedBus))
    }

    @Test func audibleOriginalStillFallsBackForAMissingUnit() {
        var s = Session(originalAudio: true)
        s.commit(1, from: 0, to: 2, at: 2.5)
        s.pcm(1, seconds: 2, at: 2.7)
        s.run(3.0, .hypothesis(ASRHypothesis(utterance: 2, tokens: [ASRToken(text: "第二句。")], isFinal: true,
                                             audioFrontier: 144_000)))
        let failed = s.run(3.1, .translation(UnitID(2), .failure("translation unavailable")))
        #expect(s.p.state == .degraded)
        #expect(failed.contains(.flushTranslatedBus))
    }

    /// Recovery from a hard failure waits for a healthy worker and a fresh unit, not for soft
    /// pressure that fluent playout handles by skipping.
    @Test func recoveryAfterWorkerLossIgnoresSoftPressure() {
        var s = Session()
        s.commit(1, from: 0, to: 2, at: 2.5)
        s.pcm(1, seconds: 2, at: 2.7)
        s.run(2.8, .workerHealthy(false))
        #expect(s.p.state == .degraded)
        s.run(3.0, .workerHealthy(true))
        s.run(3.5, .vad(.speechStart(Int64(3.5 * rate))))
        s.tick(3.6, backlog: 0)
        s.commit(2, from: 3.5, to: 9.5, at: 10.0)   // long unit: its onset is 6.5 s old
        s.tick(10.2, backlog: 0)
        s.pcm(2, seconds: 3, at: 10.3, generation: 2)
        s.tick(10.4, backlog: 0)
        #expect(s.p.state == .translating(speaker))
        #expect(s.canceled("source_expired").isEmpty)
    }

    @Test func fluentJobsCarryTheSourceDurationAsTarget() {
        var fluent = Session()
        fluent.commit(1, from: 0.5, to: 2.5, at: 3)
        var plain = Session(fluent: false)
        plain.commit(1, from: 0.5, to: 2.5, at: 3)
        func targets(_ s: Session) -> [Double?] {
            s.effects.compactMap { if case .synthesize(let job) = $0 { Optional(job.targetSeconds) } else { nil } }
        }
        #expect(targets(fluent) == [2.0])
        #expect(targets(plain) == [nil])
    }
}

@Suite struct StreamingClauseTests {
    private func hyp(_ tokens: [ASRToken], final: Bool = false) -> ASRHypothesis {
        ASRHypothesis(utterance: 1, tokens: tokens, isFinal: final, audioFrontier: 480_000)
    }

    private func streaming() -> CommitPolicy {
        var config = CommitPolicyConfig()
        config.streamingClauses = true
        return CommitPolicy(config: config)
    }

    private func units(_ outputs: [CommitOutput]) -> [SourceUnit] {
        outputs.compactMap { if case .commit(let u) = $0 { u } else { nil } }
    }

    @Test func stableConjunctionClosesAClauseWithoutWaitingForTheWindow() {
        let text = "我们今天开会但是他们不来"
        let tokens = text.map { ASRToken(text: String($0)) }
        var p = streaming()
        _ = p.process(hyp(tokens), at: 0)
        let out = units(p.process(hyp(tokens), at: 100_000_000))
        #expect(out.map(\.text) == ["我们今天开会"])
        #expect(out.first?.reason == .conjunction)
        var plain = CommitPolicy()
        _ = plain.process(hyp(tokens), at: 0)
        #expect(units(plain.process(hyp(tokens), at: 100_000_000)).isEmpty)
    }

    @Test func timedPauseBetweenTokensClosesAClause() {
        var tokens: [ASRToken] = []
        var cursor: Int64 = 0
        for (i, ch) in "我们今天开会他们不来".enumerated() {
            if i == 6 { cursor += 14_400 }   // 300 ms of silence after the first clause
            tokens.append(ASRToken(text: String(ch), span: SourceSpan(start: cursor, end: cursor + 9_600)))
            cursor += 9_600
        }
        var p = streaming()
        _ = p.process(hyp(tokens), at: 0)
        let out = units(p.process(hyp(tokens), at: 100_000_000))
        #expect(out.map(\.text) == ["我们今天开会"])
        #expect(out.first?.reason == .prosodicPause)
        #expect(out.first?.span == SourceSpan(start: 0, end: 57_600))
    }

    @Test func contiguousTokenTimingIsNotAPause() {
        let tokens = "我们今天开会他们不来".enumerated().map { i, ch in
            ASRToken(text: String(ch), span: SourceSpan(start: Int64(i) * 9_600, end: Int64(i + 1) * 9_600))
        }
        var p = streaming()
        _ = p.process(hyp(tokens), at: 0)
        #expect(units(p.process(hyp(tokens), at: 100_000_000)).isEmpty)
    }
}
