// Stage interfaces (IMPLEMENTATION_PLAN.md section 4.2). The semantics are the contract;
// adapters live in A2AModels and are verified by adapter contract tests.

public protocol VoiceActivityDetector {
    mutating func process(_ frame: AudioFrame16k) -> [VADEvent]
}

public protocol SpeakerTracker {
    mutating func process(_ frame: AudioFrame16k, vad: [VADEvent]) -> [SpeakerEvent]
}

public protocol StreamingRecognizer: AnyObject {
    func accept(_ frame: AudioFrame16k)
    /// Ask the recognizer to finalize the current utterance (after VAD end).
    /// A VAD end does not make the last partial final by itself.
    func requestFinalize()
    /// Every hypothesis update since the last call, oldest first. Finals are never skipped.
    func drainHypotheses() -> [ASRHypothesis]
    func reset()
}

public protocol IncrementalTranslator: Sendable {
    /// Optional experimental continuation capability, verified by adapter contract tests.
    var supportsPrefix: Bool { get }
    var supportsContext: Bool { get }
    func translate(_ req: TranslationRequest) async throws -> TranslationCandidate
}

/// Runtime capabilities are verified independently; a paper claim is not evidence.
public struct SynthesisCapabilities: Hashable, Sendable {
    public let audioOutputStreaming: Bool
    public let incrementalTextInput: Bool
    public let continuationAcrossUnits: Bool
    /// False for plumbing adapters that do not preserve the source speaker (e.g. a system voice).
    public let preservesSpeakerIdentity: Bool

    public init(audioOutputStreaming: Bool, incrementalTextInput: Bool,
                continuationAcrossUnits: Bool, preservesSpeakerIdentity: Bool) {
        self.audioOutputStreaming = audioOutputStreaming
        self.incrementalTextInput = incrementalTextInput
        self.continuationAcrossUnits = continuationAcrossUnits
        self.preservesSpeakerIdentity = preservesSpeakerIdentity
    }
}

/// One block of synthesized mono or interleaved PCM in the model's native format.
public struct SynthesizedAudio: Sendable {
    public let samples: [Float]
    public let format: PCMFormat

    public init(samples: [Float], format: PCMFormat) {
        self.samples = samples
        self.format = format
    }

    public var frames: Int { samples.count / max(1, format.channelCount) }
}

public protocol VoiceSynthesizer: Sendable {
    var capabilities: SynthesisCapabilities { get }
    func prepare(_ profile: VoiceProfile) async throws -> PreparedVoice
    /// v0 sends one complete committed unit; PCM arrives as it is generated.
    func synthesize(unit: UnitID, text: String, voice: PreparedVoice) -> AsyncThrowingStream<SynthesizedAudio, Error>
    func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?) -> AsyncThrowingStream<SynthesizedAudio, Error>
    /// `targetSeconds` is the source speech this text translates. `allowFill` says the host
    /// plays each phrase in its own slot (ADR-0013), so a take shorter than that speech may be
    /// slowed to end with the actor instead of leaving the tail of their turn silent. A host
    /// that plays as soon as audio is ready must pass false: there a short take is saved
    /// latency, and slowing it would push every later phrase back.
    func synthesize(unit: UnitID, text: String, voice: PreparedVoice,
                    targetSeconds: Double?, allowFill: Bool) -> AsyncThrowingStream<SynthesizedAudio, Error>
    /// `delivery` is the actor's own audio of this very line, with the words they said
    /// (ADR-0015). A reference-conditioned synthesizer may append it to the speaker's pinned
    /// reference so the take keeps the line's delivery — a shout stays a shout, a murmur stays a
    /// murmur — instead of the average of everything else that speaker said. nil means speak on
    /// the pinned profile. A synthesizer with no reference ignores it, which is why the default
    /// implementation drops it.
    func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?,
                    allowFill: Bool, delivery: DeliveryClip?) -> AsyncThrowingStream<SynthesizedAudio, Error>
}

/// One line's source audio for delivery conditioning (ADR-0015). Kept in memory for the unit:
/// the actor's voice is participant data, and nothing here is written or traced.
public struct DeliveryClip: Hashable, Sendable {
    public let pcm: [Float]
    public let sampleRate: Int
    /// What the actor said over this audio. In-context conditioning pairs audio with its
    /// transcript, so a clip without words cannot be appended to a transcript reference.
    public let sourceText: String

    public init(pcm: [Float], sampleRate: Int, sourceText: String = "") {
        self.pcm = pcm
        self.sampleRate = sampleRate
        self.sourceText = sourceText
    }
}

extension VoiceSynthesizer {
    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice,
                           targetSeconds: Double?, allowFill: Bool) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: targetSeconds)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?,
                           allowFill: Bool, delivery: DeliveryClip?) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        // Only a reference-conditioned voice can take a delivery; every other adapter speaks the
        // text as it does without one.
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: targetSeconds, allowFill: allowFill)
    }
}
