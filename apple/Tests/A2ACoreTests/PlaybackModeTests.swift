import A2ACore
import Testing

@Suite struct PlaybackModeTests {
    @Test func separatedBackgroundPlaysDuringVoiceAcquisitionButMutesForOriginalAndStop() {
        var config = PipelineConfig(session: "background")
        config.originalAudioEnabled = false
        config.backgroundAudioEnabled = true
        var p = SessionPipeline(config: config)
        _ = p.handle(.arm, at: 0)
        let armed = p.handle(.armSucceeded, at: 1)
        #expect(armed.contains(.backgroundGain(0.8, durationMs: 0)))
        #expect(armed.contains { if case .rampGains(let g, 0) = $0 {
            g.original == 0 && g.translated == 0
        } else { false } })
        #expect(p.handle(.playbackMode(.original), at: 2).contains(.backgroundGain(0, durationMs: 30)))
        #expect(p.handle(.playbackMode(.translated), at: 3).contains(.backgroundGain(0.8, durationMs: 30)))
        #expect(p.handle(.stop, at: 4).contains(.backgroundGain(0, durationMs: 30)))
    }

    private let speaker = SpeakerID("S1")

    private func session(mode: PlaybackMode = .translated) -> SessionPipeline {
        var config = PipelineConfig(session: "playback-switch")
        config.originalAudioEnabled = false
        config.playbackMode = mode
        var pipeline = SessionPipeline(config: config)
        _ = pipeline.handle(.arm, at: 0)
        _ = pipeline.handle(.armSucceeded, at: 0)
        _ = pipeline.handle(.profileReady(speaker, version: 1), at: 0)
        _ = pipeline.handle(.vad(.speechStart(0)), at: 0)
        return pipeline
    }

    private func readyUnit(_ pipeline: inout SessionPipeline) -> [PipelineEffect] {
        _ = pipeline.handle(.hypothesis(ASRHypothesis(
            utterance: 1, tokens: [ASRToken(text: "我们开会。")], isFinal: true, audioFrontier: 48_000)), at: 1)
        _ = pipeline.handle(.translation(UnitID(1), .success(TranslationCandidate(
            unitID: UnitID(1), text: "We will meet."))), at: 2)
        let header = PCMChunkHeader(unitID: UnitID(1), speakerID: speaker, profileVersion: 1,
            sessionEpoch: 1, playoutGeneration: 1, format: PCMFormat(sampleRate: 24_000, channelCount: 1),
            span: SourceSpan(start: 0, end: 48_000), isLast: false)
        return pipeline.handle(.pcm(header, frames: 4_800), at: 3)
    }

    @Test func originalCanBeSelectedBeforeStarting() {
        var p = SessionPipeline(config: PipelineConfig(session: "idle-switch"))
        #expect(p.handle(.playbackMode(.original), at: 0).isEmpty)
        _ = p.handle(.arm, at: 1)
        let effects = p.handle(.armSucceeded, at: 2)
        #expect(effects.contains(.rampGains(.original, durationMs: 0)))
        #expect(p.playbackMode == .original)
    }

    @Test func switchingBackAndForthKeepsTheSessionAndVoiceWarm() {
        var p = session()
        _ = readyUnit(&p)
        let epoch = p.gate.epoch, generation = p.gate.generation
        let original = p.handle(.playbackMode(.original), at: 4)
        #expect(original.contains(.rampGains(.original, durationMs: 30)))
        #expect(p.state == .translating(speaker))
        let translated = p.handle(.playbackMode(.translated), at: 5)
        #expect(translated.contains(.rampGains(.translated, durationMs: 30)))
        #expect(!original.contains(.flushTranslatedBus))
        #expect(!translated.contains(.teardown))
        #expect(p.gate.epoch == epoch && p.gate.generation == generation)
        #expect(p.machine.hasProfile(speaker))
    }

    @Test func backgroundHandoffsAndRecoveryCannotOverrideOriginalSelection() {
        var p = session(mode: .original)
        #expect(readyUnit(&p).contains(.rampGains(.original, durationMs: 30)))
        let failed = p.handle(.workerHealthy(false), at: 4)
        #expect(failed.contains(.rampGains(.original, durationMs: 30)))
        #expect(p.playbackMode == .original)
        let restart = p.handle(.discontinuity("device changed"), at: 5)
        #expect(restart.contains(.rampGains(.original, durationMs: 30)))
        #expect(p.playbackMode == .original)
    }

    @Test func translatedSelectionStaysSilentUntilEnglishIsAvailable() {
        var p = session(mode: .original)
        let selected = p.handle(.playbackMode(.translated), at: 1)
        #expect(selected.contains { if case .rampGains(let gains, 30) = $0 {
            gains.original == 0 && gains.translated == 0
        } else { false } })
        #expect(readyUnit(&p).contains(.rampGains(.translated, durationMs: 30)))
        let failed = p.handle(.workerHealthy(false), at: 4)
        #expect(failed.contains { if case .rampGains(let gains, 30) = $0 {
            gains.original == 0 && gains.translated == 0
        } else { false } })
    }

    @Test func mutedEnglishDoesNotCountAsPlayedTranslation() {
        var p = session(mode: .original)
        _ = readyUnit(&p)
        let start = p.handle(.playStarted(UnitID(1), outSample: 0), at: 4)
        #expect(!start.contains { if case .trace(let event) = $0 { event.kind == .playStart } else { false } })
        let end = p.handle(.playEnded(UnitID(1), outSample: 9_600), at: 5)
        #expect(end.contains { if case .trace(let event) = $0 {
            event.kind == .sourceDisposition && event.jsonLine().contains("user_original_playback")
        } else { false } })
        #expect(!end.contains { if case .trace(let event) = $0 { event.kind == .playEnd } else { false } })
    }

    @Test func switchingMidUnitDoesNotCountPartialEnglishAsComplete() {
        var p = session()
        _ = readyUnit(&p)
        _ = p.handle(.playStarted(UnitID(1), outSample: 0), at: 4)
        _ = p.handle(.playbackMode(.original), at: 5)
        _ = p.handle(.playbackMode(.translated), at: 6)
        let end = p.handle(.playEnded(UnitID(1), outSample: 9_600), at: 7)
        #expect(end.contains { if case .trace(let event) = $0 {
            event.jsonLine().contains("user_original_playback")
        } else { false } })
    }
}
