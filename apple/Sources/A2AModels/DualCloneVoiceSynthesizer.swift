import A2ACore
import Foundation

/// Two independent resident workers, each with its own speaker-conditioning cache.
/// The driver owns lane scheduling and never sends more than one active job per lane.
public final class DualCloneVoiceSynthesizer: VoiceSynthesizer, @unchecked Sendable {
    private let workers: [CloneVoiceSynthesizer]

    public init(_ first: CloneVoiceSynthesizer, _ second: CloneVoiceSynthesizer) {
        workers = [first, second]
    }

    public var capabilities: SynthesisCapabilities { workers[0].capabilities }
    public var failure: String? { workers.compactMap(\.failure).first }
    public var info: CloneWorkerInfo? { workers[0].info }

    public func prepare(_ profile: VoiceProfile) async throws -> PreparedVoice {
        async let first = workers[0].prepare(profile)
        async let second = workers[1].prepare(profile)
        let (a, b) = try await (first, second)
        guard a.speakerID == b.speakerID, a.profileVersion == b.profileVersion else {
            throw AdapterError.unsupported("voice worker profiles disagree")
        }
        return a
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: nil)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: targetSeconds, lane: 0)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?, lane: Int) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: targetSeconds, allowFill: false, lane: lane)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?,
                           allowFill: Bool) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: targetSeconds, allowFill: allowFill, lane: 0)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?,
                           allowFill: Bool, lane: Int) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: targetSeconds, allowFill: allowFill,
                   delivery: nil, lane: lane)
    }

    /// Delivery conditioning is per line (ADR-0015), so it follows the lane that owns the unit:
    /// both workers hold the same prepared profile, and the clip goes to whichever speaks.
    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?,
                           allowFill: Bool, delivery: DeliveryClip?) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: targetSeconds, allowFill: allowFill,
                   delivery: delivery, lane: 0)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice, targetSeconds: Double?,
                           allowFill: Bool, delivery: DeliveryClip?,
                           lane: Int) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        precondition((0..<workers.count).contains(lane))
        return workers[lane].synthesize(unit: unit, text: text, voice: voice, targetSeconds: targetSeconds,
                                        allowFill: allowFill, delivery: delivery)
    }

    public func takeStats(for unit: UnitID) -> CloneSynthesisStats? {
        for worker in workers {
            if let stats = worker.takeStats(for: unit) { return stats }
        }
        return nil
    }

    public func shutdown() { workers.forEach { $0.shutdown() } }
}
