import A2ACore
import Testing

private let speaker = SpeakerID("S1")

private func hypothesis(_ text: String, utterance: Int = 1) -> ASRHypothesis {
    ASRHypothesis(utterance: utterance, tokens: [ASRToken(text: text)], isFinal: true, audioFrontier: 48_000)
}

private func armed() -> SessionPipeline {
    var p = SessionPipeline(config: PipelineConfig(session: "recovery"))
    _ = p.handle(.arm, at: 0)
    _ = p.handle(.armSucceeded, at: 0)
    _ = p.handle(.profileReady(speaker, version: 1), at: 0)
    _ = p.handle(.vad(.speechStart(0)), at: 0)
    return p
}

private func startPlaying(_ p: inout SessionPipeline) {
    _ = p.handle(.hypothesis(hypothesis("我们开会。")), at: 1)
    _ = p.handle(.translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "We will meet."))), at: 2)
    let header = PCMChunkHeader(unitID: UnitID(1), speakerID: speaker, profileVersion: 1,
                                sessionEpoch: 1, playoutGeneration: 1,
                                format: PCMFormat(sampleRate: 24_000, channelCount: 1),
                                span: SourceSpan(start: 0, end: 48_000), isLast: false)
    _ = p.handle(.pcm(header, frames: 4_800), at: 3)
}

@Suite struct RecoveryTests {
    @Test func deviceChangeFlushesAndRestoresOriginal() {
        var p = armed()
        startPlaying(&p)
        #expect(p.state == .translating(speaker))
        let effects = p.handle(.discontinuity("headphones removed"), at: 4)
        #expect(effects.contains(.flushTranslatedBus))
        #expect(effects.contains(.rampGains(.original, durationMs: 30)))
        #expect(p.state == .listening)
        #expect(p.gate.epoch == 2)
        #expect(!p.machine.hasProfile(speaker))
    }

    @Test func activeSynthesisFailureRestoresOriginalImmediately() {
        var p = armed()
        startPlaying(&p)
        let effects = p.handle(.synthesisFailed(UnitID(1), "worker stopped"), at: 4)
        #expect(p.state == .degraded)
        #expect(effects.contains(.rampGains(.original, durationMs: 30)))
        #expect(effects.contains(.flushTranslatedBus))
    }

    @Test func lateFailureCannotInterruptANewGeneration() {
        var p = armed()
        startPlaying(&p)
        _ = p.handle(.discontinuity("restart"), at: 4)
        #expect(p.handle(.synthesisFailed(UnitID(1), "late cancellation"), at: 5).isEmpty)
    }

    @Test func translationFailureWhilePlayingRestoresOriginal() {
        var p = armed()
        startPlaying(&p)
        _ = p.handle(.hypothesis(hypothesis("第二句。", utterance: 2)), at: 4)
        let effects = p.handle(.translation(UnitID(2), .failure("translation unavailable")), at: 5)
        #expect(p.state == .degraded)
        #expect(effects.contains(.rampGains(.original, durationMs: 30)))
        #expect(effects.contains(.flushTranslatedBus))
    }

    @Test func voiceAcquisitionBoundsPendingTranslationsAndCancelsWork() {
        var config = PipelineConfig(session: "bounded")
        config.maxPendingUnits = 2
        var p = SessionPipeline(config: config)
        _ = p.handle(.arm, at: 0)
        _ = p.handle(.armSucceeded, at: 0)
        for id in 1...2 { _ = p.handle(.hypothesis(hypothesis("测试。", utterance: id)), at: UInt64(id)) }
        let effects = p.handle(.hypothesis(hypothesis("测试。", utterance: 3)), at: 3)
        #expect(effects.contains(.cancelTranslation(UnitID(1))))
        let stale = p.handle(.translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "Stale."))), at: 4)
        #expect(!stale.contains { if case .synthesize = $0 { true } else { false } })
    }

    @Test func captionsDoNotPutTextInDiagnostics() {
        var config = PipelineConfig(session: "private")
        config.emitCaptions = true
        var p = SessionPipeline(config: config)
        _ = p.handle(.arm, at: 0)
        _ = p.handle(.armSucceeded, at: 0)
        let effects = p.handle(.hypothesis(hypothesis("私密内容。")), at: 1)
        #expect(effects.contains(.caption(kind: "source.final", id: "utt1", text: "私密内容。")))
        for effect in effects {
            if case .trace(let event) = effect { #expect(!event.jsonLine().contains("私密")) }
        }
    }

    @Test func stoppedSessionIgnoresLateRecognitionAndProfiles() {
        var p = armed()
        _ = p.handle(.stop, at: 1)
        #expect(p.handle(.hypothesis(hypothesis("迟到的结果。")), at: 2).isEmpty)
        #expect(p.handle(.profileReady(speaker, version: 2), at: 3).isEmpty)
        #expect(p.state == .idle)
    }

    @Test func frameworkErrorDescriptionsCannotLeakTextToDefaultDiagnostics() {
        var p = armed()
        _ = p.handle(.hypothesis(hypothesis("私密内容。")), at: 1)
        let effects = p.handle(.translation(UnitID(1), .failure("error processing 私密内容。")), at: 2)
        for effect in effects {
            if case .trace(let event) = effect { #expect(!event.jsonLine().contains("私密")) }
        }
    }

    @Test func staleHeldAudioCannotPreventRecoveryWithFreshSpeech() {
        var config = PipelineConfig(session: "recovery")
        config.health.stableRetryNs = 500_000_000
        var p = SessionPipeline(config: config)
        func seconds(_ s: Double) -> UInt64 { UInt64(s * 1e9) }
        func timed(_ id: Int, _ start: Int64, _ end: Int64) -> ASRHypothesis {
            ASRHypothesis(utterance: id, tokens: [ASRToken(text: "测试。", span: SourceSpan(start: start, end: end))],
                          isFinal: true, audioFrontier: end)
        }
        func header(_ id: Int, generation: PlayoutGeneration) -> PCMChunkHeader {
            PCMChunkHeader(unitID: UnitID(id), speakerID: speaker, profileVersion: 1, sessionEpoch: 1,
                           playoutGeneration: generation, format: PCMFormat(sampleRate: 24_000, channelCount: 1),
                           span: SourceSpan(start: 0, end: 48_000), isLast: false)
        }
        _ = p.handle(.arm, at: 0)
        _ = p.handle(.armSucceeded, at: 0)
        _ = p.handle(.profileReady(speaker, version: 1), at: 0)
        _ = p.handle(.vad(.speechStart(0)), at: 0)
        _ = p.handle(.captured(span: SourceSpan(start: 0, end: 48_000)), at: seconds(1))
        _ = p.handle(.hypothesis(timed(1, 0, 48_000)), at: seconds(1))
        _ = p.handle(.translation(UnitID(1), .success(TranslationCandidate(unitID: UnitID(1), text: "First."))), at: seconds(1.1))
        _ = p.handle(.pcm(header(1, generation: 1), frames: 4_800), at: seconds(1.2))
        _ = p.handle(.workerHealthy(false), at: seconds(1.3))
        #expect(p.state == .degraded)
        _ = p.handle(.hypothesis(timed(2, 48_000, 96_000)), at: seconds(2))
        _ = p.handle(.translation(UnitID(2), .success(TranslationCandidate(unitID: UnitID(2), text: "Second."))), at: seconds(2.1))
        _ = p.handle(.pcm(header(2, generation: 2), frames: 4_800), at: seconds(2.2))
        _ = p.handle(.captured(span: SourceSpan(start: 48_000, end: 384_000)), at: seconds(8))
        _ = p.handle(.workerHealthy(true), at: seconds(8))
        let expired = p.handle(.tick, at: seconds(8))
        #expect(expired.contains(.discardHeld(UnitID(2))))
        _ = p.handle(.hypothesis(timed(3, 336_000, 384_000)), at: seconds(8))
        _ = p.handle(.translation(UnitID(3), .success(TranslationCandidate(unitID: UnitID(3), text: "Fresh."))), at: seconds(8.01))
        _ = p.handle(.pcm(header(3, generation: 2), frames: 4_800), at: seconds(8.02))
        _ = p.handle(.tick, at: seconds(8.1))
        let recovered = p.handle(.tick, at: seconds(8.7))
        #expect(p.state == .translating(speaker))
        #expect(recovered.contains(.releaseHeld(UnitID(3))))
        #expect(!recovered.contains(.releaseHeld(UnitID(2))))
    }

    @Test func translationOnlyNeverEnablesOriginalOnAnyRecoveryPath() {
        var config = PipelineConfig(session: "english-only")
        config.originalAudioEnabled = false
        var p = SessionPipeline(config: config)
        var effects: [PipelineEffect] = []
        effects += p.handle(.arm, at: 0)
        effects += p.handle(.armSucceeded, at: 0)
        effects += p.handle(.profileReady(speaker, version: 1), at: 0)
        effects += p.handle(.vad(.speechStart(0)), at: 0)
        startPlaying(&p)
        #expect(p.state == .translating(speaker))
        effects += p.handle(.workerHealthy(false), at: 4)
        effects += p.handle(.discontinuity("device change"), at: 5)
        effects += p.handle(.stop, at: 6)
        let ramps = effects.compactMap { effect -> BusGains? in
            if case .rampGains(let gains, _) = effect { gains } else { nil }
        }
        #expect(!ramps.isEmpty)
        #expect(ramps.allSatisfy { $0.original == 0 })
    }
}

@Suite struct SpeechEvidenceTests {
    @Test func rejectsSilenceButAcceptsDelayedFinalOverlappingSpeech() {
        var evidence = SpeechEvidence()
        evidence.advance(to: 96_000)
        let h = ASRHypothesis(utterance: 1, tokens: [ASRToken(text: "好的", span: SourceSpan(start: 0, end: 48_000))],
                              isFinal: true, audioFrontier: 96_000)
        #expect(!evidence.accepts(h))
        evidence.process(.speechStart(0))
        evidence.process(.speechEnd(48_000))
        #expect(evidence.accepts(h))
        let silence = ASRHypothesis(utterance: 2, tokens: [ASRToken(text: "you", span: SourceSpan(start: 72_000, end: 90_000))],
                                    isFinal: true, audioFrontier: 96_000)
        #expect(!evidence.accepts(silence))
    }

    @Test func oldSpeechDoesNotQualifyNewUntimedHallucinations() {
        var evidence = SpeechEvidence(historySeconds: 1)
        evidence.process(.speechStart(0))
        evidence.process(.speechEnd(48_000))
        evidence.advance(to: 192_000)
        #expect(!evidence.accepts(hypothesis("you")))
    }
}
