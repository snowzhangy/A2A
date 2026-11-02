// System-voice synthesizer and adapter errors.

import A2ACore
import AVFoundation
import Foundation
import Synchronization

public enum AdapterError: Error, CustomStringConvertible {
    case assetsNotInstalled(String)
    case unsupported(String)
    case conversion(String)

    public var description: String {
        switch self {
        case .assetsNotInstalled(let s): "assets not installed: \(s) (run the setup path; runtime downloads are disabled)"
        case .unsupported(let s): "unsupported: \(s)"
        case .conversion(let s): "audio conversion failed: \(s)"
        }
    }
}

// MARK: System voice (plumbing only)

/// AVSpeechSynthesizer output for end-to-end plumbing. It does not preserve the source
/// speaker, so it can never satisfy an identity gate.
///
/// With a target duration (fluent playout), the voice speaks faster natively instead of being
/// time-stretched, and the ~0.2 s of digital silence AVSpeechSynthesizer appends to every
/// utterance is cut to a pause that fits the unit's ending.
///
/// A profile that carries the speaker's pitch (multi-speaker sessions) is cast its own
/// installed voice (`VoiceCasting`), so each speaker in a dialogue sounds different. Without
/// one, every speaker uses the language's default voice.
public final class SystemVoiceSynthesizer: VoiceSynthesizer, @unchecked Sendable {
    private let voiceIdentifier: String?
    private let warmUpText: String
    private let requestedLanguage: String
    private let maxSecondsPerCharacter: Double
    private let initialSecondsPerCharacter: Double
    /// Learned voiced seconds per character at the default rate, per voice, from completed paced units.
    private let secondsPerCharacter = Locked<[String: Double]>([:])
    private let casting: Locked<VoiceCasting>
    /// Cast role per speaker; a PreparedVoice handle is the role index plus one (0 = default voice).
    private let roles = Locked<[SpeakerID: Int]>([:])
    private let warmed = Locked<Set<String>>([])

    /// Speech speed-up for each `AVSpeechUtterance.rate`, measured with the compact en-US
    /// voice on the reference Mac (macOS 27): the same factor for short and long sentences.
    static let speedForRate: [(rate: Float, speed: Double)] = [
        (0.50, 1.0), (0.52, 1.077), (0.54, 1.234), (0.56, 1.354), (0.58, 1.424), (0.60, 1.591),
    ]
    /// Beyond this, faster speech costs more naturalness than the latency it saves; fluent
    /// catch-up skipping handles the rest of the debt.
    public static let maxSpeed = 1.3
    /// Samples below this are silence (AVSpeechSynthesizer pads with exact zeros).
    static let silenceLevel: Float = 1e-4

    private final class Job: @unchecked Sendable {
        let synthesizer = AVSpeechSynthesizer()
        let paced: Bool
        let postRollSeconds: Double
        struct Pending: Sendable {
            var sampleRate = 0
            var samples: [Float] = []
            var received = 0         // samples delivered by the synthesizer so far
            var firstVoiced: Int?    // absolute sample indices, for duration learning
            var lastVoiced = 0
            var stopped = false
        }
        let pending = Mutex(Pending())

        init(paced: Bool, postRollSeconds: Double) {
            self.paced = paced
            self.postRollSeconds = postRollSeconds
        }

        // AVSpeechSynthesizer can burst many tiny buffers before the consumer resumes.
        // Coalesce to 100 ms so the stream limit bounds audio duration, not framework
        // callback frequency. Flush the short final chunk without padding it. Paced jobs
        // hold back trailing silence until voice follows, and keep only a short post-roll.
        func coalesce(_ samples: UnsafeBufferPointer<Float>, sampleRate: Int, final: Bool) -> [SynthesizedAudio] {
            pending.withLock { state in
                state.sampleRate = sampleRate
                let voiced = { (x: Float) in abs(x) >= SystemVoiceSynthesizer.silenceLevel }
                if let last = samples.lastIndex(where: voiced) {
                    if state.firstVoiced == nil { state.firstVoiced = state.received + (samples.firstIndex(where: voiced) ?? last) }
                    state.lastVoiced = state.received + last + 1
                }
                state.received += samples.count
                state.samples.append(contentsOf: samples)
                let voicedEnd = paced ? max(0, state.samples.count - (state.received - state.lastVoiced)) : state.samples.count
                let frames = max(1, sampleRate / 10)
                let format = PCMFormat(sampleRate: sampleRate, channelCount: 1)
                var chunks: [SynthesizedAudio] = []
                var offset = 0
                while voicedEnd - offset >= frames {
                    chunks.append(SynthesizedAudio(samples: Array(state.samples[offset..<offset + frames]), format: format))
                    offset += frames
                }
                if final {
                    let end = paced ? min(state.samples.count, voicedEnd + Int(postRollSeconds * Double(sampleRate)))
                        : state.samples.count
                    if offset < end {
                        chunks.append(SynthesizedAudio(samples: Array(state.samples[offset..<end]), format: format))
                    }
                    offset = state.samples.count
                }
                if offset > 0 { state.samples.removeFirst(offset) }
                return chunks
            }
        }

        /// Voiced duration of a job that ran to completion, or nil.
        var voicedSeconds: Double? {
            pending.withLock { s in
                guard !s.stopped, let first = s.firstVoiced, s.sampleRate > 0, s.lastVoiced > first else { return nil }
                return Double(s.lastVoiced - first) / Double(s.sampleRate)
            }
        }
    }

    public init(language: String = "en-US") {
        voiceIdentifier = AVSpeechSynthesisVoice(language: language)?.identifier
        requestedLanguage = language
        let mandarin = language.lowercased().hasPrefix("zh")
        maxSecondsPerCharacter = mandarin ? 0.6 : 0.2
        warmUpText = mandarin ? TranslationLanguage.mandarin.warmUpText : TranslationLanguage.english.warmUpText
        // Initial estimates only; completed units learn each voice's actual cadence.
        initialSecondsPerCharacter = mandarin ? 0.22 : 0.055
        // Only voices this process can open: the listing can include voices whose assets
        // are not available to it.
        let installed = AVSpeechSynthesisVoice.speechVoices().compactMap { v -> VoiceCasting.InstalledVoice? in
            guard AVSpeechSynthesisVoice(identifier: v.identifier) != nil else { return nil }
            let gender: VoiceCasting.Gender? = switch v.gender {
            case .male: .male
            case .female: .female
            default: nil
            }
            return VoiceCasting.InstalledVoice(identifier: v.identifier, name: v.name, language: v.language,
                                               gender: gender, quality: v.quality.rawValue,
                                               novelty: v.voiceTraits.contains(.isNoveltyVoice),
                                               personal: v.voiceTraits.contains(.isPersonalVoice))
        }
        casting = Locked(VoiceCasting(candidates: VoiceCasting.candidates(from: installed, locale: language)))
    }

    public var capabilities: SynthesisCapabilities {
        SynthesisCapabilities(audioOutputStreaming: true, incrementalTextInput: false,
                              continuationAcrossUnits: false, preservesSpeakerIdentity: false)
    }

    /// With the speaker's pitch, casts (once per speaker) and warms that speaker's voice.
    public func prepare(_ profile: VoiceProfile) async throws -> PreparedVoice {
        var handle = 0
        if let pitch = profile.pitchHz {
            let speaker = profile.speakerID
            let role = roles.withLock { $0[speaker] } ?? casting.withLock { c -> Int? in c.cast(pitchHz: pitch) }
            if let role {
                roles.withLock { $0[speaker] = role }
                handle = role + 1
            }
        }
        let voice = PreparedVoice(speakerID: profile.speakerID, profileVersion: profile.version, handle: handle)
        if handle > 0 { await warmUp(voice) }
        return voice
    }

    /// Name of the installed voice a prepared voice speaks with, for logs.
    public func voiceName(for voice: PreparedVoice) -> String? {
        role(for: voice).flatMap { AVSpeechSynthesisVoice(identifier: $0.identifier)?.name }
    }

    /// Identifier and pitch for a prepared voice; handle 0 is the language's default voice.
    func role(for voice: PreparedVoice) -> (identifier: String, pitchMultiplier: Float)? {
        if voice.handle > 0, let role = casting.withLock({ c in
            voice.handle <= c.roles.count ? c.roles[voice.handle - 1] : nil
        }) {
            return (role.identifier, role.pitchMultiplier)
        }
        return voiceIdentifier.map { ($0, 1) }
    }

    /// Loads the voice before the first unit: its first utterance takes about 0.7 s to start,
    /// against about 0.3 s once warm (reference Mac).
    public func warmUp() async {
        await warmUp(PreparedVoice(speakerID: SpeakerID("warm-up"), profileVersion: 0, handle: 0))
    }

    private func warmUp(_ voice: PreparedVoice) async {
        guard let id = role(for: voice)?.identifier, warmed.withLock({ $0.insert(id).inserted }) else { return }
        do { for try await _ in synthesize(unit: UnitID(0), text: warmUpText, voice: voice) {} } catch {}
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        synthesize(unit: unit, text: text, voice: voice, targetSeconds: nil)
    }

    public func synthesize(unit: UnitID, text: String, voice: PreparedVoice,
                           targetSeconds: Double?) -> AsyncThrowingStream<SynthesizedAudio, Error> {
        guard let role = role(for: voice), let selectedVoice = AVSpeechSynthesisVoice(identifier: role.identifier) else {
            return AsyncThrowingStream { $0.finish(throwing: AdapterError.unsupported("system voice for \(requestedLanguage)")) }
        }
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = selectedVoice
        if role.pitchMultiplier != 1 { utterance.pitchMultiplier = role.pitchMultiplier }
        // A sentence keeps a sentence-length pause; a clause flows into the next one.
        let postRoll = text.last.map { ".!?。！？".contains($0) } == true ? 0.18 : 0.09
        var paced = false
        var speed = 1.0
        let voiceID = role.identifier
        let initial = initialSecondsPerCharacter
        if let targetSeconds, targetSeconds.isFinite, targetSeconds > 0 {
            paced = true
            speed = Self.speed(text: text, targetSeconds: targetSeconds, postRollSeconds: postRoll,
                               secondsPerCharacter: secondsPerCharacter.withLock { $0[voiceID] ?? initial })
            utterance.rate = Self.rate(forSpeed: speed)
        }
        let job = Job(paced: paced, postRollSeconds: postRoll)
        let learned = secondsPerCharacter
        let maxSecondsPerCharacter = self.maxSecondsPerCharacter
        return AsyncThrowingStream(bufferingPolicy: .bufferingOldest(128)) { continuation in
            continuation.onTermination = { _ in
                job.pending.withLock { $0.stopped = true }
                DispatchQueue.main.async { job.synthesizer.stopSpeaking(at: .immediate) }
            }
            job.synthesizer.write(utterance) { buffer in
                guard let pcm = buffer as? AVAudioPCMBuffer else { return }
                let n = Int(pcm.frameLength)
                var samples = [Float](repeating: 0, count: n)
                if n > 0, let f = pcm.floatChannelData {
                    samples.withUnsafeMutableBufferPointer { $0.baseAddress!.update(from: f[0], count: n) }
                } else if let s = pcm.int16ChannelData {
                    for i in 0..<n { samples[i] = Float(s[0][i]) / 32_768 }
                }
                let chunks = samples.withUnsafeBufferPointer {
                    job.coalesce($0, sampleRate: Int(pcm.format.sampleRate), final: n == 0)
                }
                for chunk in chunks {
                    if case .dropped = continuation.yield(chunk) {
                        continuation.finish(throwing: AdapterError.conversion("synthesis consumer exceeded 12.8 s of buffered audio"))
                        return
                    }
                }
                guard n == 0 else { return }
                if paced, let voiced = job.voicedSeconds, !text.isEmpty {
                    let observed = min(maxSecondsPerCharacter, max(0.02, voiced * speed / Double(text.count)))
                    learned.withLock { $0[voiceID] = 0.7 * ($0[voiceID] ?? initial) + 0.3 * observed }
                }
                continuation.finish()
            }
        }
    }

    /// Speed-up that fits the text into its source duration, within `maxSpeed`. Small
    /// overruns are tolerated so short acknowledgements keep their natural cadence.
    static func speed(text: String, targetSeconds: Double, postRollSeconds: Double, secondsPerCharacter: Double) -> Double {
        let natural = Double(text.count) * secondsPerCharacter + postRollSeconds
        return min(maxSpeed, max(1, natural / (max(0.5, targetSeconds) + 0.15)))
    }

    /// Inverse of `speedForRate`, interpolated linearly between measured points.
    static func rate(forSpeed speed: Double) -> Float {
        let table = speedForRate
        guard speed > table[0].speed else { return table[0].rate }
        for (a, b) in zip(table, table.dropFirst()) where speed <= b.speed {
            let f = (speed - a.speed) / (b.speed - a.speed)
            return a.rate + Float(f) * (b.rate - a.rate)
        }
        return table[table.count - 1].rate
    }
}
