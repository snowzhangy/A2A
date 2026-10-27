// Session pipeline reducer: commit policy + translation validation + synthesis scheduling +
// playout gate + state machine + debt accounting. Pure: inputs (with their event time) in,
// effects out. Drivers (a2a-pipeline, the app host) execute effects with real adapters and
// feed results back, so the same reducer runs in golden tests, replay, and live sessions.

public enum PlaybackMode: String, CaseIterable, Hashable, Sendable {
    case original
    case translated
}

public struct PipelineConfig: Sendable {
    public var session: String
    public let direction: TranslationDirection
    public var commit = CommitPolicyConfig()
    public var health = HealthThresholds()
    public var validator = TranslationValidator()
    /// Preceding committed source/target pairs sent with each request.
    public var contextPairs = 3
    public var glossary: [String: String] = [:]
    public var defaultSpeaker = SpeakerID("S1")
    /// Debt estimate for committed text that has no PCM yet.
    public var estimatedSecondsPerSourceChar = 0.25
    public var estimatedSecondsPerTargetChar = 1.0 / 15.0
    /// Research mode includes text in trace records; product diagnostics omit it (D9).
    public var traceText = false
    /// Bounds model work even while acquiring a voice or recovering from a failure.
    public var maxPendingUnits = 8
    /// Captions are delivered in memory, separately from persisted diagnostics.
    public var emitCaptions = false
    /// Translation-only sessions stay silent while preparing or recovering.
    public var originalAudioEnabled = true
    /// Explicit listening choice. Translation continues when the original bus is selected.
    public var playbackMode: PlaybackMode = .translated
    /// Fluent playout: soft pressure (debt, source lag) never cuts the phrase being heard and
    /// never falls back. Units wait behind the phrase on the bus, and catching up skips the
    /// oldest units nobody has heard yet, always keeping the newest. Worker loss, underruns,
    /// and the hard queue cap still fall back immediately.
    public var finishPhraseBeforeCatchUp = false
    /// Dialogue (ADR-0005): each unit takes the speaker who covers most of its span, speaker
    /// turns never fall back, and a unit whose speaker has no profile yet waits for one
    /// (up to the source-lag limit) instead of being dropped.
    public var multiSpeaker = false

    public init(session: String, direction: TranslationDirection = .chineseToEnglish) {
        self.session = session
        self.direction = direction
        validator = TranslationValidator(targetLanguage: direction.target)
        if direction.source == .english {
            commit.preferWordBoundaries = true
            estimatedSecondsPerSourceChar = 1.0 / 15.0
            estimatedSecondsPerTargetChar = 0.25
        }
    }
}

public enum TranslationOutcome: Hashable, Sendable {
    case success(TranslationCandidate)
    case failure(String)
}

public enum PipelineInput: Hashable, Sendable {
    case arm
    case armSucceeded
    case armFailed(String)
    case stop
    case playbackMode(PlaybackMode)
    /// A capture buffer covering `span` became available to the pipeline.
    case captured(span: SourceSpan)
    case vad(VADEvent)
    case speaker(SpeakerEvent)
    case hypothesis(ASRHypothesis)
    case profileReady(SpeakerID, version: Int)
    case translation(UnitID, TranslationOutcome)
    case pcm(PCMChunkHeader, frames: Int)
    case synthesisFinished(UnitID, generation: PlayoutGeneration)
    case synthesisFailed(UnitID, String)
    case playStarted(UnitID, outSample: Int64)
    case playEnded(UnitID, outSample: Int64)
    /// Scheduled-but-unplayed translated audio reported by the mixer.
    case translatedBacklog(seconds: Double)
    case underrun
    case workerHealthy(Bool)
    /// Periodic (100 ms) health and queue sampling.
    case tick
    /// Source/device discontinuity or restart.
    case discontinuity(String)
}

public struct SynthesisJob: Hashable, Sendable {
    public let unit: UnitID
    public let text: String
    public let speaker: SpeakerID
    public let profileVersion: Int
    public let epoch: SessionEpoch
    public let generation: PlayoutGeneration
    public let span: SourceSpan
    public var targetSeconds: Double? = nil
}

public enum PipelineEffect: Hashable, Sendable {
    case trace(TraceEvent)
    case caption(kind: String, id: String, text: String)
    case requestFinalize
    case translate(TranslationRequest)
    case synthesize(SynthesisJob)
    case cancelTranslation(UnitID)
    case cancelSynthesis(UnitID)
    /// Admitted PCM waits for a handoff decision.
    case holdPCM(PCMChunkHeader)
    /// Admitted PCM goes straight to the translated bus.
    case enqueuePCM(PCMChunkHeader)
    /// Move a held unit's PCM to the translated bus.
    case releaseHeld(UnitID)
    case discardHeld(UnitID)
    case dropPCM(PCMChunkHeader, reason: String)
    case rampGains(BusGains, durationMs: Int)
    case flushTranslatedBus
    case teardown
}

public enum SourceDisposition: String, Sendable {
    case translated
    case originalOnly = "original_only"
    case abandoned
    case neverCommitted = "never_committed"
}

public struct SessionPipeline: Sendable {
    enum Stage: String, Sendable {
        /// `waiting`: fluent playout holds the unit's PCM until the phrase ahead of it ends.
        /// `awaitingVoice`: translated; its speaker's profile is not ready yet (multi-speaker).
        case translating, awaitingVoice, synthesizing, held, waiting, queued, playing, finished, canceled, rejected

        /// Synthesis has started: PCM may be in flight, held, or on the bus.
        var synthesisStarted: Bool {
            self == .synthesizing || self == .held || self == .waiting || self == .queued || self == .playing
        }
        var isLive: Bool { self == .translating || self == .awaitingVoice || synthesisStarted }
        /// Scheduled on the translated bus.
        var onBus: Bool { self == .queued || self == .playing }
    }

    struct UnitRecord: Sendable {
        let source: SourceUnit
        var speaker: SpeakerID
        var stage: Stage = .translating
        var target: String?
        var generation: PlayoutGeneration = 0
        var bufferedSeconds = 0.0
        var synthesisComplete = false
        var scheduled = false
        var handoff = false
        var playbackSuppressed = false
    }

    public let config: PipelineConfig
    public private(set) var gate = PlayoutGate()
    public private(set) var machine: SessionStateMachine
    public private(set) var playbackMode: PlaybackMode
    private var policy: CommitPolicy
    private var units: [UnitID: UnitRecord] = [:]
    private var order: [UnitID] = []
    private var context: [CommittedPair] = []
    private var lastTarget: String?
    private var speaker: SpeakerID
    private var speakerAssigned = false
    /// Multi-speaker: tracker labels in source order, for attributing units by span.
    private var speakerTimeline: [(span: SourceSpan, speaker: SpeakerID)] = []
    private var backlogSeconds = 0.0
    private var underrunPending = false
    private var workerHealthy = true
    private var speechOnset: Int64?
    private var inSpeech = false
    private var anchors: [(sample: Int64, tNs: UInt64)] = []
    private var now: UInt64 = 0

    public init(config: PipelineConfig) {
        self.config = config
        self.policy = CommitPolicy(config: config.commit)
        self.machine = SessionStateMachine(thresholds: config.health, generation: 1, multiSpeaker: config.multiSpeaker)
        self.playbackMode = config.playbackMode
        self.speaker = config.defaultSpeaker
    }

    public var state: SessionState { machine.state }

    public mutating func handle(_ input: PipelineInput, at tNs: UInt64) -> [PipelineEffect] {
        now = tNs
        if machine.state == .idle {
            switch input {
            case .arm, .playbackMode: break
            default: return []
            }
        }
        var out: [PipelineEffect] = []
        switch input {
        case .arm:
            out.append(trace(.sessionStart, [.string("pair", config.direction.tracePair), .int("schema", TraceEvent.schemaVersion)]))
            out += session(.arm)
        case .armSucceeded:
            out += session(.armSucceeded)
        case .armFailed(let message):
            out.append(trace(.error, [.string("where", "arm"), .string("message", config.traceText ? message : "preparation failed")]))
            out += session(.armFailed(config.traceText ? message : "preparation failed"))
        case .playbackMode(let mode):
            guard mode != playbackMode else { break }
            playbackMode = mode
            if mode == .original {
                for id in order where units[id]?.stage == .playing {
                    units[id]?.playbackSuppressed = true
                }
            }
            guard machine.state != .idle else { break }
            let gains = outputGains(machine.state.gains)
            out.append(trace(.gainRamp, [.double("original", Double(gains.original)),
                                         .double("translated", Double(gains.translated)),
                                         .int("ms", config.health.rampMs), .string("playback", mode.rawValue)]))
            out.append(.rampGains(gains, durationMs: config.health.rampMs))
        case .stop:
            out += cancelAll(disposition: .abandoned, reason: "stop")
            out += commitOutputs(policy.reset())
            out += session(.stop)
            gate.advanceEpoch()
            machine = SessionStateMachine(thresholds: config.health, generation: gate.generation,
                                          multiSpeaker: config.multiSpeaker)
            profileVersions.removeAll()
            context.removeAll()
            units.removeAll()
            order.removeAll()
            anchors.removeAll()
            lastTarget = nil
            speechOnset = nil
            inSpeech = false
            speakerAssigned = false
            speakerTimeline.removeAll()
            backlogSeconds = 0
            workerHealthy = true

        case .captured(let span):
            anchors.append((span.end, tNs))
            if anchors.count > 4096 { anchors.removeFirst(anchors.count - 2048) }
            out.append(trace(.bufCapture, [.span(span), .int64("frames", span.count)]))

        case .vad(.speechStart(let s)):
            speechOnset = s
            inSpeech = true
            policy.speechStarted(at: s)
            out.append(trace(.vadStart, [.int64("src_sample", s)]))
            out += session(.speech(speaker))
        case .vad(.speechEnd(let s)):
            inSpeech = false
            out.append(trace(.vadEnd, [.int64("src_sample", s)]))
            out += commitOutputs(policy.speechEnded(at: s))

        case .speaker(.assign(let spk, let span)):
            let changed = speakerAssigned && spk != speaker
            speaker = spk
            speakerAssigned = true
            out.append(trace(.spkAssign, [.speaker(spk), .span(span)]))
            if config.multiSpeaker {
                speakerTimeline.append((span, spk))
                if speakerTimeline.count > 512 { speakerTimeline.removeFirst(speakerTimeline.count - 256) }
                if changed { out.append(trace(.spkChange, [.speaker(spk), .span(span)])) }
            } else if changed {
                out.append(trace(.spkChange, [.speaker(spk), .span(span)]))
                out += session(.speakerChange(spk))
            }
        case .speaker(.change(let span)):
            out.append(trace(.spkChange, [.span(span)]))

        case .hypothesis(let h):
            if config.emitCaptions {
                out.append(.caption(kind: h.isFinal ? "source.final" : "source.partial",
                                    id: "utt\(h.utterance)", text: h.text))
            }
            var fields: [TraceEvent.Field] = [
                .span(SourceSpan(start: speechOnset ?? 0, end: max(speechOnset ?? 0, h.audioFrontier))),
                .int("utt", h.utterance),
            ]
            if config.traceText { fields.append(.string("text", h.text)) }
            out.append(trace(h.isFinal ? .asrFinal : .asrPartial, fields))
            out += commitOutputs(policy.process(h, at: tNs))

        case .profileReady(let spk, let version):
            out.append(trace(.profileReady, [.speaker(spk), .int("prof", version)]))
            out += session(.profileReady(spk, version: version))
            for id in order where units[id]?.stage == .awaitingVoice && units[id]?.speaker == spk {
                out += synthesize(id, version: version)
            }

        case .translation(let id, let outcome):
            out += translationResult(id, outcome)

        case .pcm(let header, let frames):
            out += pcm(header, frames: frames)

        case .synthesisFinished(let id, let generation):
            guard var r = units[id], r.generation == generation, r.stage.synthesisStarted else { break }
            r.synthesisComplete = true
            units[id] = r
            if r.stage == .held { out += session(.unitReady(readiness(r, id))) }
        case .synthesisFailed(let id, let message):
            guard let r = units[id], r.stage.synthesisStarted else { break }
            out.append(trace(.error, [.unit(id), .string("where", "tts"), .string("message", config.traceText ? message : "synthesis failed")]))
            gate.cancel(id)
            out += [.cancelSynthesis(id), .discardHeld(id)]
            out += finish(id, as: .originalOnly, reason: "tts_failed")
            out += missingUnitFallback()

        case .playStarted(let id, let outSample):
            guard var r = units[id], r.stage == .queued else { break }
            r.stage = .playing
            r.playbackSuppressed = playbackMode == .original
            units[id] = r
            if !r.playbackSuppressed {
                out.append(trace(.playStart, [.unit(id), .int64("out_sample", outSample)]))
            }
        case .playEnded(let id, let outSample):
            guard let r = units[id], r.stage == .playing || r.stage == .queued else { break }
            gate.markFinished(id)
            if r.playbackSuppressed {
                out += finish(id, as: .originalOnly, reason: "user_original_playback")
            } else {
                out.append(trace(.playEnd, [.unit(id), .int64("out_sample", outSample)]))
                out += finish(id, as: .translated, reason: r.handoff ? "handoff" : "played")
            }

        case .translatedBacklog(let seconds):
            backlogSeconds = seconds
        case .underrun:
            underrunPending = true
        case .workerHealthy(let ok):
            workerHealthy = ok
            if !ok { out.append(trace(.error, [.string("where", "worker"), .string("message", "service lost")])) }
            out += session(.workerHealthy(ok))

        case .tick:
            out += tick()

        case .discontinuity(let reason):
            out.append(trace(.error, [.string("where", "source"), .string("message", "discontinuity: \(reason)")]))
            out += commitOutputs(policy.reset())
            out += cancelAll(disposition: .abandoned, reason: "discontinuity")
            gate.advanceEpoch()
            machine = SessionStateMachine(thresholds: config.health, generation: gate.generation,
                                          multiSpeaker: config.multiSpeaker)
            profileVersions.removeAll()
            context.removeAll()
            speakerTimeline.removeAll()
            lastTarget = nil
            backlogSeconds = 0
            inSpeech = false
            out += [.flushTranslatedBus, .rampGains(outputGains(.original), durationMs: config.health.rampMs)]
            out += session(.arm)
            out += session(.armSucceeded)
            anchors.removeAll()
            speechOnset = nil
        }
        out += promoteWaiting()
        return out
    }

    // MARK: Commit → translate

    private mutating func commitOutputs(_ outputs: [CommitOutput]) -> [PipelineEffect] {
        var out: [PipelineEffect] = []
        for o in outputs {
            switch o {
            case .commit(let unit):
                let unitSpeaker = config.multiSpeaker ? speaker(for: unit.span) : speaker
                var fields: [TraceEvent.Field] = [
                    .unit(unit.id), .speaker(unitSpeaker), .span(unit.span),
                    .string("reason", unit.reason.rawValue), .bool("timing_estimated", unit.timingEstimated),
                    .int("wait_ms", Int(unit.semanticWaitNs / 1_000_000)),
                ]
                if config.traceText { fields.append(.string("text", unit.text)) }
                out.append(trace(.commitSource, fields))
                units[unit.id] = UnitRecord(source: unit, speaker: unitSpeaker)
                order.append(unit.id)
                let pending = order.filter { units[$0]?.stage.isLive == true }
                if pending.count > max(1, config.maxPendingUnits) {
                    for id in pending.prefix(pending.count - max(1, config.maxPendingUnits)) {
                        out += cancel(id, disposition: .originalOnly, reason: "queue_limit")
                    }
                    out += session(.health(HealthSample(debtSeconds: config.health.maxDebtSeconds + 1,
                                                        sourceLagSeconds: 0)))
                }
                guard units[unit.id]?.stage == .translating else { continue }
                let request = TranslationRequest(
                    unitID: unit.id, context: Array(context.suffix(config.contextPairs)),
                    glossary: config.glossary, uncommittedSource: unit.text)
                out.append(trace(.mtRequest, [.unit(unit.id), .int("context_pairs", request.context.count)]))
                out.append(.translate(request))
            case .requestFinalize(let u):
                out.append(trace(.finalizeRequest, [.int("utt", u)]))
                out.append(.requestFinalize)
            case .hold(let u, let reason, let pending, let waited):
                out.append(trace(.commitHold, [.int("utt", u), .string("reason", reason.rawValue),
                                               .int("pending_keys", pending), .int("wait_ms", Int(waited / 1_000_000))]))
            case .revisionBehindFrontier(let u, let keys):
                out.append(trace(.commitRevision, [.int("utt", u), .int("committed_keys", keys)]))
            case .abandoned(let u, let text):
                var fields: [TraceEvent.Field] = [.int("utt", u), .string("disposition", SourceDisposition.neverCommitted.rawValue)]
                if config.traceText { fields.append(.string("text", text)) }
                out.append(trace(.sourceDisposition, fields))
            }
        }
        return out
    }

    private mutating func translationResult(_ id: UnitID, _ outcome: TranslationOutcome) -> [PipelineEffect] {
        guard var r = units[id], r.stage == .translating else {
            return [trace(.mtResult, [.unit(id), .bool("ok", false), .string("reason", "stale")])]
        }
        switch outcome {
        case .failure(let message):
            return [trace(.mtResult, [.unit(id), .bool("ok", false), .string("reason", "error")]),
                    trace(.error, [.unit(id), .string("where", "mt"), .string("message", config.traceText ? message : "translation failed")])]
                + rejectTranslation(id, reason: "mt_error")
        case .success(let candidate):
            guard candidate.unitID == id else {
                return rejectTranslation(id, reason: "wrong_unit")
            }
            if let rejection = config.validator.validate(candidate, source: r.source.text, previousTarget: lastTarget) {
                return [trace(.mtResult, [.unit(id), .bool("ok", false), .string("reason", rejection.rawValue)])]
                    + rejectTranslation(id, reason: rejection.rawValue)
            }
            let text = candidate.text.trimmingWhitespace()
            var out: [PipelineEffect] = [trace(.mtResult, [.unit(id), .bool("ok", true)])]
            if config.emitCaptions { out.append(.caption(kind: "target.final", id: id.description, text: text)) }
            // The tracker may have labeled more of the unit's span since it was committed.
            if config.multiSpeaker { r.speaker = speaker(for: r.source.span) }
            var fields: [TraceEvent.Field] = [.unit(id), .speaker(r.speaker), .span(r.source.span)]
            r.target = text
            lastTarget = text
            context.append(CommittedPair(source: r.source.text, target: text))
            if context.count > config.contextPairs { context.removeFirst(context.count - config.contextPairs) }
            guard machine.hasProfile(r.speaker), let version = profileVersion(r.speaker) else {
                if config.multiSpeaker { r.stage = .awaitingVoice }
                units[id] = r
                if config.traceText { fields.append(.string("text", text)) }
                out.append(trace(.commitTarget, fields))
                return config.multiSpeaker ? out : out + finish(id, as: .originalOnly, reason: "no_profile")
            }
            fields.insert(.int("prof", version), at: 2)
            if config.traceText { fields.append(.string("text", text)) }
            out.append(trace(.commitTarget, fields))
            units[id] = r
            return out + synthesize(id, version: version)
        }
    }

    private mutating func synthesize(_ id: UnitID, version: Int) -> [PipelineEffect] {
        guard var r = units[id], let text = r.target else { return [] }
        r.stage = .synthesizing
        r.generation = gate.generation
        units[id] = r
        return [trace(.ttsRequest, [.unit(id), .int("prof", version), .int("chars", text.count)]),
                .synthesize(SynthesisJob(unit: id, text: text, speaker: r.speaker, profileVersion: version,
                                         epoch: gate.epoch, generation: gate.generation, span: r.source.span,
                                         targetSeconds: config.finishPhraseBeforeCatchUp && !r.source.timingEstimated
                                             ? max(0.5, r.source.span.durationSeconds) : nil))]
    }

    private var profileVersions: [SpeakerID: Int] = [:]
    private func profileVersion(_ spk: SpeakerID) -> Int? { profileVersions[spk] }

    /// The speaker who covers most of `span` in the tracker's labels; without overlap, the
    /// latest speaker labeled before the span ends (multi-speaker sessions).
    public func speaker(for span: SourceSpan) -> SpeakerID {
        if let best = overlaps(of: span).first { return best.speaker }
        return speakerTimeline.last { $0.span.start < span.end }?.speaker ?? speaker
    }

    /// The speaker labeling most of `span`, their share of it, and the share labeled at all.
    public func speakerCoverage(of span: SourceSpan) -> (speaker: SpeakerID, share: Double, labeled: Double)? {
        let all = overlaps(of: span)
        guard let best = all.first, span.count > 0 else { return nil }
        let labeled = all.reduce(0) { $0 + $1.samples }
        return (best.speaker, Double(best.samples) / Double(span.count), Double(labeled) / Double(span.count))
    }

    /// Labeled samples of `span` per speaker, largest first; ties go to the most recent speaker.
    private func overlaps(of span: SourceSpan) -> [(speaker: SpeakerID, samples: Int64)] {
        var overlaps: [(speaker: SpeakerID, samples: Int64)] = []
        for entry in speakerTimeline.reversed() {
            if entry.span.end <= span.start { break }
            let o = min(entry.span.end, span.end) - max(entry.span.start, span.start)
            guard o > 0 else { continue }
            if let i = overlaps.firstIndex(where: { $0.speaker == entry.speaker }) { overlaps[i].samples += o }
            else { overlaps.append((entry.speaker, o)) }
        }
        return overlaps.enumerated().sorted { ($0.element.samples, -$0.offset) > ($1.element.samples, -$1.offset) }.map(\.element)
    }

    private mutating func rejectTranslation(_ id: UnitID, reason: String) -> [PipelineEffect] {
        finish(id, as: .originalOnly, reason: reason) + missingUnitFallback()
    }

    /// A unit that will never play. When the original bus can be heard, fall back so the
    /// listener hears what it covered rather than a silent translated bus. English-only
    /// playback would only trade the phrase being heard for silence, so it skips the unit.
    private mutating func missingUnitFallback() -> [PipelineEffect] {
        guard config.originalAudioEnabled else { return [] }
        return session(.health(HealthSample(debtSeconds: 0, sourceLagSeconds: 0, underrun: true)))
    }

    // MARK: PCM admission and handoff

    private mutating func pcm(_ header: PCMChunkHeader, frames: Int) -> [PipelineEffect] {
        let admission = gate.admit(header)
        guard admission.isAccepted, var r = units[header.unitID], r.stage.synthesisStarted else {
            let reason = admission.isAccepted ? "unknown_unit" : admission.reason
            return [trace(.pcmReject, [.unit(header.unitID), .int("chunk_gen", Int(header.playoutGeneration)),
                                       .string("reason", reason)]),
                    .dropPCM(header, reason: reason)]
        }
        let seconds = Double(frames) / Double(header.format.sampleRate)
        var out: [PipelineEffect] = []
        out.append(trace(r.bufferedSeconds == 0 ? .ttsFirstChunk : .ttsChunk,
                         [.unit(header.unitID), .int("frames", frames), .int("rate", header.format.sampleRate)]))
        r.bufferedSeconds += seconds
        if r.stage.onBus {
            units[header.unitID] = r
            out.append(.enqueuePCM(header))
            return out
        }
        if r.stage == .waiting {
            units[header.unitID] = r
            out.append(.holdPCM(header))
            return out
        }
        if case .translating(let spk) = machine.state, spk == r.speaker || config.multiSpeaker {
            if config.finishPhraseBeforeCatchUp {
                // Every phrase needs a jitter reserve, including after an empty bus.
                // promoteWaiting also releases completed units below the threshold.
                r.stage = .waiting
                units[header.unitID] = r
                out.append(.holdPCM(header))
                return out
            }
            r.stage = .queued
            r.scheduled = true
            units[header.unitID] = r
            out.append(.enqueuePCM(header))
            out.append(trace(.playScheduled, [.unit(header.unitID)]))
            return out
        }
        r.stage = .held
        units[header.unitID] = r
        out.append(.holdPCM(header))
        out += session(.unitReady(readiness(r, header.unitID)))
        return out
    }

    private func readiness(_ r: UnitRecord, _ id: UnitID) -> UnitReadiness {
        UnitReadiness(unit: id, speaker: r.speaker, generation: r.generation,
                      bufferedSeconds: r.bufferedSeconds, complete: r.synthesisComplete)
    }

    // MARK: State machine effects

    private mutating func session(_ input: SessionInput) -> [PipelineEffect] {
        if case .profileReady(let spk, let v) = input { profileVersions[spk] = v }
        var out: [PipelineEffect] = []
        for effect in machine.handle(input, at: now) {
            switch effect {
            case .transition(let from, let to, let reason):
                out.append(trace(.stateChange, [.string("from", from.description), .string("to", to.description),
                                                .string("reason", reason)]))
            case .rampGains(let gains, let ms):
                let audible = outputGains(gains)
                out.append(trace(.gainRamp, [.double("original", Double(audible.original)),
                                             .double("translated", Double(audible.translated)), .int("ms", ms)]))
                out.append(.rampGains(audible, durationMs: ms))
            case .advanceGeneration:
                gate.advanceGeneration()
                machine.syncGeneration(gate.generation)
            case .cancelPendingSynthesis:
                out.append(.flushTranslatedBus)
                out += cancelAll(disposition: .abandoned, reason: "fallback")
            case .fallbackEnter(let reason):
                out.append(trace(.fallbackEnter, [.string("reason", reason)]))
            case .fallbackExit(let unit):
                out.append(trace(.fallbackExit, [.unit(unit)]))
            case .handoff(let unit):
                out += handoff(unit)
            case .teardown:
                out.append(.teardown)
            }
        }
        return out
    }

    private mutating func handoff(_ id: UnitID) -> [PipelineEffect] {
        var out: [PipelineEffect] = []
        // Earlier units were already heard as original; their PCM is stale.
        for older in order where older < id {
            guard let r = units[older],
                  r.stage == .held || r.stage == .synthesizing || r.stage == .translating || r.stage == .awaitingVoice else { continue }
            out += cancel(older, disposition: .originalOnly, reason: "superseded_by_handoff")
        }
        guard var r = units[id] else { return out }
        r.stage = .queued
        r.scheduled = true
        r.handoff = true
        units[id] = r
        out.append(.releaseHeld(id))
        out.append(trace(.playScheduled, [.unit(id), .bool("handoff", true)]))
        return out
    }

    // MARK: Cancellation and disposition

    private mutating func cancelAll(disposition: SourceDisposition, reason: String) -> [PipelineEffect] {
        var out: [PipelineEffect] = []
        for id in order {
            guard let r = units[id] else { continue }
            if r.stage.isLive { out += cancel(id, disposition: disposition, reason: reason) }
        }
        return out
    }

    private mutating func cancel(_ id: UnitID, disposition: SourceDisposition, reason: String) -> [PipelineEffect] {
        guard var r = units[id] else { return [] }
        let wasSynth = r.stage.synthesisStarted
        r.stage = .canceled
        units[id] = r
        gate.cancel(id)
        var out: [PipelineEffect] = [trace(.unitCancel, [.unit(id), .string("reason", reason)])]
        if wasSynth { out += [.cancelSynthesis(id), .discardHeld(id)] }
        else { out.append(.cancelTranslation(id)) }
        out.append(trace(.sourceDisposition, [.unit(id), .span(r.source.span),
                                              .string("disposition", disposition.rawValue), .string("reason", reason)]))
        prune()
        return out
    }

    private mutating func finish(_ id: UnitID, as disposition: SourceDisposition, reason: String) -> [PipelineEffect] {
        guard var r = units[id] else { return [] }
        r.stage = disposition == .translated ? .finished : .rejected
        units[id] = r
        prune()
        return [trace(.sourceDisposition, [.unit(id), .span(r.source.span),
                                           .string("disposition", disposition.rawValue), .string("reason", reason)])]
    }

    /// Drop terminal records beyond a bounded history.
    private mutating func prune() {
        guard order.count > 256 else { return }
        let drop = order.prefix(order.count - 128).filter {
            guard let s = units[$0]?.stage else { return true }
            return s == .finished || s == .canceled || s == .rejected
        }
        for id in drop {
            units[id] = nil
            gate.forgetTerminalUnit(id)
        }
        let dropped = Set(drop)
        order.removeAll { dropped.contains($0) }
    }

    // MARK: Health

    private mutating func tick() -> [PipelineEffect] {
        var out: [PipelineEffect] = []
        switch machine.state {
        case .acquiringVoice, .degraded:
            // Held audio is not a recovery candidate once its source is stale. Keeping
            // it in the health denominator prevents the healthy retry interval from
            // ever starting, even when fresh units and a healthy worker are available.
            // Fluent playout keeps the newest candidate: long units are always older than
            // the lag limit by the time they commit, and would otherwise never play.
            let candidates = order.filter { id in
                guard let r = units[id], machine.hasProfile(r.speaker) else { return false }
                return r.stage == .translating || r.stage == .synthesizing || r.stage == .held
            }
            for id in config.finishPhraseBeforeCatchUp ? candidates.dropLast() : candidates[...] {
                guard let r = units[id], sourceAgeSeconds(of: r.source.span.start) > config.health.maxSourceLagSeconds
                else { continue }
                out += cancel(id, disposition: .originalOnly, reason: "source_expired")
            }
        default: break
        }
        // A unit waits for its speaker's profile only while its source is fresh.
        for id in order {
            guard let r = units[id], r.stage == .awaitingVoice,
                  sourceAgeSeconds(of: r.source.span.end) > config.health.maxSourceLagSeconds else { continue }
            out += finish(id, as: .originalOnly, reason: "no_profile")
        }
        var (pressure, pending) = queuePressure()
        out.append(trace(.queueDepth, [.int("debt_ms", Int((pressure.debt * 1000).rounded())),
                                       .int("oldest_source_age_ms", Int((pressure.age * 1000).rounded())),
                                       .int("units_pending", pending)]))
        let underrun = underrunPending
        underrunPending = false
        if config.finishPhraseBeforeCatchUp {
            // Soft pressure skips units nobody has heard yet; it never falls back. Recovery
            // from a hard failure then needs only a healthy worker and a fresh unit.
            if case .translating = machine.state { out += catchUp(pressure) }
            pressure = (0, 0)
        }
        out += session(.health(HealthSample(debtSeconds: pressure.debt, sourceLagSeconds: pressure.age,
                                            underrun: underrun, serviceLost: !workerHealthy)))
        // Retry handoff with the freshest held unit; older backlog is never replayed.
        switch machine.state {
        case .acquiringVoice, .degraded:
            if let id = order.last(where: { units[$0]?.stage == .held }), let r = units[id] {
                out += session(.unitReady(readiness(r, id)))
            }
        default:
            break
        }
        return out
    }

    /// Translated audio the listener has yet to hear (bus backlog plus estimates for pending
    /// work), and the age of the oldest source not yet covered by scheduled audio.
    private func queuePressure() -> (pressure: (debt: Double, age: Double), pending: Int) {
        var pendingEstimate = 0.0
        var oldest: Int64?
        var pending = 0
        for id in order {
            guard let r = units[id], r.stage.isLive else { continue }
            pending += 1
            pendingEstimate += unscheduledSeconds(r)
            // Scheduled audio is covered source; the mixer reports it as backlog.
            if r.stage.onBus || r.stage == .waiting { continue }
            oldest = min(oldest ?? r.source.span.start, r.source.span.start)
        }
        // Speech not yet committed is uncovered source too: blocked recognition shows up
        // here even when the audio queue is small.
        if speechOnset != nil, inSpeech || policy.uncommittedKeys > 0 {
            let start = policy.uncommittedStart
            oldest = min(oldest ?? start, start)
        }
        return ((backlogSeconds + pendingEstimate, oldest.map { sourceAgeSeconds(of: $0) } ?? 0), pending)
    }

    /// Debt a unit adds beyond the bus backlog: estimated audio still to generate, plus
    /// generated audio held off the bus while it waits.
    private func unscheduledSeconds(_ r: UnitRecord) -> Double {
        let total = Double(r.target?.count ?? 0) * config.estimatedSecondsPerTargetChar
        switch r.stage {
        case .translating: return Double(r.source.text.count) * config.estimatedSecondsPerSourceChar
        case .awaitingVoice, .synthesizing, .held: return max(0, total - r.bufferedSeconds)
        case .waiting: return max(total, r.bufferedSeconds)
        case .queued, .playing, .finished, .canceled, .rejected: return 0
        }
    }

    /// Fluent catch-up: skip unheard units, oldest first, while the listener is more than the
    /// debt limit behind or a unit's source ended longer ago than the lag limit. The phrase on
    /// the bus always finishes and the newest unit is always kept, so speech never stops dead.
    private mutating func catchUp(_ pressure: (debt: Double, age: Double)) -> [PipelineEffect] {
        let health = config.health
        guard pressure.debt > health.maxDebtSeconds || pressure.age > health.maxSourceLagSeconds else { return [] }
        let unheard = order.filter { id in
            guard let stage = units[id]?.stage else { return false }
            return stage == .translating || stage == .awaitingVoice || stage == .synthesizing || stage == .waiting
        }
        var debt = pressure.debt
        var skipped: [UnitID] = []
        // An empty bus must not continually cancel the generation about to refill it.
        // Keep the oldest synthesis in flight and skip later unheard backlog instead.
        let refilling = order.contains(where: { units[$0]?.stage.onBus == true }) ? nil
            : unheard.first(where: { units[$0]?.stage == .synthesizing })
        for id in unheard.dropLast() {
            if id == refilling { continue }
            guard let r = units[id] else { continue }
            let stale = sourceAgeSeconds(of: r.source.span.end) > health.maxSourceLagSeconds
            guard debt > health.maxDebtSeconds || stale else { break }
            debt -= unscheduledSeconds(r)
            skipped.append(id)
        }
        guard !skipped.isEmpty else { return [] }
        var out = [trace(.catchUp, [.int("units", skipped.count),
                                    .int("debt_ms", Int((pressure.debt * 1000).rounded())),
                                    .int("oldest_source_age_ms", Int((pressure.age * 1000).rounded()))])]
        for id in skipped { out += cancel(id, disposition: .originalOnly, reason: "catch_up") }
        return out
    }

    /// Fluent playout: the oldest waiting unit goes on the bus once nothing is scheduled ahead.
    private mutating func promoteWaiting() -> [PipelineEffect] {
        guard config.finishPhraseBeforeCatchUp, case .translating = machine.state,
              !order.contains(where: { units[$0]?.stage.onBus == true }),
              let id = order.first(where: { units[$0]?.stage == .waiting }),
              let r = units[id],
              r.synthesisComplete || r.bufferedSeconds >= config.health.prebufferSeconds else { return [] }
        units[id]?.stage = .queued
        units[id]?.scheduled = true
        return [.releaseHeld(id), trace(.playScheduled, [.unit(id)])]
    }

    /// Age of a source sample, mapped to event time through capture anchors.
    private func sourceAgeSeconds(of sample: Int64) -> Double {
        // Anchors ascend by sample: binary search for the first one at or after `sample`.
        var lo = 0, hi = anchors.count
        while lo < hi {
            let mid = (lo + hi) / 2
            if anchors[mid].sample < sample { lo = mid + 1 } else { hi = mid }
        }
        guard let anchor = lo < anchors.count ? anchors[lo] : anchors.last else { return 0 }
        let sampleNs = Double(anchor.tNs) - Double(anchor.sample - sample) * 1e9 / Double(canonicalSampleRate)
        return max(0, (Double(now) - sampleNs) / 1e9)
    }

    private func trace(_ kind: TraceKind, _ fields: [TraceEvent.Field]) -> PipelineEffect {
        .trace(TraceEvent(tNs: now, session: config.session, epoch: gate.epoch, gen: gate.generation,
                          kind: kind, fields: fields))
    }

    private func outputGains(_ gains: BusGains) -> BusGains {
        if playbackMode == .original { return .original }
        return BusGains(original: config.originalAudioEnabled ? gains.original : 0, translated: gains.translated)
    }
}
