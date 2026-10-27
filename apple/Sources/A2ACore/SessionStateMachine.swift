// Session state machine (section 4.4).
//
// Idle ─arm→ Arming ─ok→ Listening ─speech→ AcquiringVoice(spk)
// AcquiringVoice ─profile ready + fresh unit PCM buffered + safe handoff→ Translating(spk)
// Translating ─debt / source lag / underrun / service lost→ Degraded
// Degraded ─healthy worker + stable interval + fresh unit PCM + safe boundary→ Translating
// Translating ─speaker change→ AcquiringVoice(new)
// any ─stop / error→ Idle
//
// Multi-speaker sessions (ADR-0005) never report speaker changes: each unit carries its
// speaker and waits for that speaker's profile, so dialogue turns do not fall back.

public enum SessionState: Hashable, Sendable, CustomStringConvertible {
    case idle
    case arming
    case listening
    case acquiringVoice(SpeakerID)
    case translating(SpeakerID)
    case degraded

    public var name: String {
        switch self {
        case .idle: "idle"
        case .arming: "arming"
        case .listening: "listening"
        case .acquiringVoice: "acquiring_voice"
        case .translating: "translating"
        case .degraded: "degraded"
        }
    }

    public var description: String {
        switch self {
        case .acquiringVoice(let s): "acquiring_voice(\(s))"
        case .translating(let s): "translating(\(s))"
        default: name
        }
    }

    /// Bus gains the mixer should hold in this state.
    public var gains: BusGains {
        if case .translating = self { return .translated }
        return .original
    }
}

public struct BusGains: Hashable, Sendable {
    public let original: Float
    public let translated: Float

    public static let original = BusGains(original: 1, translated: 0)
    public static let translated = BusGains(original: 0, translated: 1)
}

public struct HealthThresholds: Sendable {
    public var maxDebtSeconds = 3.0
    public var maxSourceLagSeconds = 4.0
    /// Healthy interval required in Degraded before retrying translation.
    public var stableRetryNs: UInt64 = 2_000_000_000
    /// Playable PCM needed before the original bus can be muted (80–120 ms).
    public var prebufferSeconds = 0.08
    public var rampMs = 30

    public init() {}
}

public struct HealthSample: Hashable, Sendable {
    public let debtSeconds: Double
    public let sourceLagSeconds: Double
    public let underrun: Bool
    public let serviceLost: Bool

    public init(debtSeconds: Double, sourceLagSeconds: Double, underrun: Bool = false, serviceLost: Bool = false) {
        self.debtSeconds = debtSeconds
        self.sourceLagSeconds = sourceLagSeconds
        self.underrun = underrun
        self.serviceLost = serviceLost
    }
}

/// Readiness of a candidate handoff unit.
public struct UnitReadiness: Hashable, Sendable {
    public let unit: UnitID
    public let speaker: SpeakerID
    public let generation: PlayoutGeneration
    public let bufferedSeconds: Double
    /// Synthesis finished, so a short unit is ready even below the prebuffer.
    public let complete: Bool
    /// The unit starts at a closed-unit boundary (always true for v0 closed units).
    public let atSafeBoundary: Bool

    public init(unit: UnitID, speaker: SpeakerID, generation: PlayoutGeneration,
                bufferedSeconds: Double, complete: Bool, atSafeBoundary: Bool = true) {
        self.unit = unit
        self.speaker = speaker
        self.generation = generation
        self.bufferedSeconds = bufferedSeconds
        self.complete = complete
        self.atSafeBoundary = atSafeBoundary
    }
}

public enum SessionInput: Hashable, Sendable {
    case arm
    case armSucceeded
    case armFailed(String)
    case speech(SpeakerID)
    case profileReady(SpeakerID, version: Int)
    case unitReady(UnitReadiness)
    case health(HealthSample)
    case workerHealthy(Bool)
    case speakerChange(SpeakerID)
    case stop
    case error(String)
}

public enum SessionEffect: Hashable, Sendable {
    case transition(from: SessionState, to: SessionState, reason: String)
    case rampGains(BusGains, durationMs: Int)
    /// Fallback or speaker handoff: invalidate pending playout.
    case advanceGeneration
    case cancelPendingSynthesis
    case fallbackEnter(reason: String)
    case fallbackExit(unit: UnitID)
    /// Start translated playback with this unit's buffered PCM.
    case handoff(unit: UnitID)
    case teardown
}

public struct SessionStateMachine: Sendable {
    public private(set) var state: SessionState = .idle
    public let thresholds: HealthThresholds
    /// Dialogue: units carry their own speaker, so the first ready unit of any speaker with
    /// a profile starts translation (the reducer never reports speaker turns).
    public let multiSpeaker: Bool
    private var profiles: [SpeakerID: Int] = [:]
    private var generation: PlayoutGeneration
    private var workerHealthy = true
    private var healthySince: UInt64?

    public init(thresholds: HealthThresholds = .init(), generation: PlayoutGeneration = 1, multiSpeaker: Bool = false) {
        self.thresholds = thresholds
        self.generation = generation
        self.multiSpeaker = multiSpeaker
    }

    public func hasProfile(_ speaker: SpeakerID) -> Bool { profiles[speaker] != nil }

    /// Keep the machine's view of the playout generation aligned with the host gate.
    public mutating func syncGeneration(_ g: PlayoutGeneration) { generation = g }

    public mutating func handle(_ input: SessionInput, at tNs: UInt64) -> [SessionEffect] {
        switch (state, input) {
        case (_, .stop):
            return state == .idle ? [] : move(to: .idle, reason: "stop") + [.rampGains(.original, durationMs: thresholds.rampMs), .teardown]
        case (_, .error(let message)):
            return state == .idle ? [] : move(to: .idle, reason: "error: \(message)") + [.rampGains(.original, durationMs: thresholds.rampMs), .teardown]

        case (.idle, .arm):
            return move(to: .arming, reason: "arm")
        case (.arming, .armSucceeded):
            return move(to: .listening, reason: "armed") + [.rampGains(.original, durationMs: 0)]
        case (.arming, .armFailed(let message)):
            return move(to: .idle, reason: "arm failed: \(message)") + [.teardown]

        case (_, .profileReady(let spk, let version)):
            profiles[spk] = version
            return []

        case (.listening, .speech(let spk)):
            return move(to: .acquiringVoice(spk), reason: "speech")

        case (.acquiringVoice(let spk), .unitReady(let r)):
            guard r.speaker == spk || multiSpeaker, handoffAllowed(r) else { return [] }
            return move(to: .translating(r.speaker), reason: "handoff \(r.unit)")
                + [.handoff(unit: r.unit), .rampGains(.translated, durationMs: thresholds.rampMs)]

        case (.acquiringVoice(let current), .speakerChange(let spk)) where spk != current:
            return move(to: .acquiringVoice(spk), reason: "speaker change")
        case (.translating(let current), .speakerChange(let spk)) where spk != current:
            return move(to: .acquiringVoice(spk), reason: "speaker change")
                + [.rampGains(.original, durationMs: thresholds.rampMs), .advanceGeneration, .cancelPendingSynthesis]

        case (.translating, .health(let h)):
            guard let reason = unhealthyReason(h) else { return [] }
            return enterDegraded(reason: reason, at: tNs)
        case (.translating, .workerHealthy(false)):
            workerHealthy = false
            return enterDegraded(reason: "service_lost", at: tNs)

        case (.degraded, .health(let h)):
            if unhealthyReason(h) == nil, workerHealthy {
                if healthySince == nil { healthySince = tNs }
            } else {
                healthySince = nil
            }
            return []
        case (.degraded, .unitReady(let r)):
            guard workerHealthy, let since = healthySince, tNs &- since >= thresholds.stableRetryNs,
                  handoffAllowed(r) else { return [] }
            return move(to: .translating(r.speaker), reason: "recovered \(r.unit)")
                + [.handoff(unit: r.unit), .fallbackExit(unit: r.unit),
                   .rampGains(.translated, durationMs: thresholds.rampMs)]

        case (_, .workerHealthy(let ok)):
            workerHealthy = ok
            if !ok { healthySince = nil }
            return []

        default:
            return []
        }
    }

    private func handoffAllowed(_ r: UnitReadiness) -> Bool {
        hasProfile(r.speaker)
            && r.generation == generation
            && r.atSafeBoundary
            && (r.bufferedSeconds >= thresholds.prebufferSeconds || r.complete)
    }

    private func unhealthyReason(_ h: HealthSample) -> String? {
        if h.serviceLost { return "service_lost" }
        if h.underrun { return "underrun" }
        if h.debtSeconds > thresholds.maxDebtSeconds { return "debt" }
        if h.sourceLagSeconds > thresholds.maxSourceLagSeconds { return "source_lag" }
        return nil
    }

    private mutating func enterDegraded(reason: String, at tNs: UInt64) -> [SessionEffect] {
        healthySince = nil
        return move(to: .degraded, reason: reason)
            + [.rampGains(.original, durationMs: thresholds.rampMs), .advanceGeneration,
               .cancelPendingSynthesis, .fallbackEnter(reason: reason)]
    }

    private mutating func move(to next: SessionState, reason: String) -> [SessionEffect] {
        let from = state
        state = next
        if next == .idle {
            healthySince = nil
            workerHealthy = true
        }
        return [.transition(from: from, to: next, reason: reason)]
    }
}
