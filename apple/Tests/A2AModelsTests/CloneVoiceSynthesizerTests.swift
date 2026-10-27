// Clone worker protocol contract, against research/tts_worker.py --fake (standard library
// tone generator; no model, MLX, or downloads). The real model runs only on request.

import A2ACore
import A2AModels
import Foundation
import Testing

private let repository = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
private let python = "/usr/bin/python3"
private let worker = repository.appendingPathComponent("research/tts_worker.py").path

private func fakeWorker(_ extra: [String] = [], targetLanguage: TranslationLanguage = .english) async throws -> CloneVoiceSynthesizer {
    try await CloneVoiceSynthesizer(executable: URL(fileURLWithPath: python), arguments: [worker, "--fake"] + extra,
                                    targetLanguage: targetLanguage,
                                    readyTimeout: .seconds(30))
}

private func profile(version: Int = 1, seconds: Double = 3, rate: Int = 48_000) -> VoiceProfile {
    let pcm = (0..<Int(seconds * Double(rate))).map { Float(0.1 * sin(Double($0) / 9)) }
    return VoiceProfile(speakerID: SpeakerID("S1"), version: version, referencePCM: pcm, referenceText: "我们下周开会。",
                        sampleRate: rate, channelCount: 1,
                        quality: ReferenceQuality(durationSeconds: seconds, snrEstimateDB: .nan, clippedFraction: 0, overlapSuspected: false))
}

@Suite(.enabled(if: FileManager.default.isExecutableFile(atPath: python)), .serialized)
struct CloneVoiceSynthesizerTests {
    @Test(arguments: ExperimentalVoiceModel.allCases)
    func selectedBackendReportsItsActualStreamingContract(_ model: ExperimentalVoiceModel) async throws {
        let synth = try await fakeWorker(["--model", model.rawValue])
        defer { synth.shutdown() }
        #expect(synth.capabilities.audioOutputStreaming == (model == .qwen))
        let voice = try await synth.prepare(profile())
        var frames = 0
        for try await chunk in synth.synthesize(unit: UnitID(1), text: "We will meet next week.", voice: voice) {
            frames += chunk.frames
        }
        #expect(frames > 0 && synth.failure == nil)
    }

    @Test func chineseTargetReachesTheWorker() async throws {
        let synth = try await fakeWorker(targetLanguage: .mandarin)
        defer { synth.shutdown() }
        let voice = try await synth.prepare(profile())
        var frames = 0
        for try await chunk in synth.synthesize(unit: UnitID(1), text: "我们下周开会。", voice: voice) {
            frames += chunk.frames
        }
        // The fake speaks Chinese per character and English per space-separated word.
        // This bounds the output above the one-word English default (0.17 s + pre-roll).
        #expect(frames > 24_000 / 4)
        #expect(synth.failure == nil)
    }

    @Test func streamsTrimmedPCMForAPreparedVoice() async throws {
        let synth = try await fakeWorker()
        defer { synth.shutdown() }
        #expect(synth.info?.model == "fake-tone")
        #expect(synth.capabilities.preservesSpeakerIdentity && synth.capabilities.audioOutputStreaming)
        let voice = try await synth.prepare(profile())
        #expect(voice.speakerID == SpeakerID("S1") && voice.profileVersion == 1)
        var frames = 0
        var firstSample: Float?
        for try await chunk in synth.synthesize(unit: UnitID(1), text: "We will meet next week.", voice: voice) {
            #expect(chunk.format == PCMFormat(sampleRate: 24_000, channelCount: 1))
            if firstSample == nil, let s = chunk.samples.first { firstSample = s }
            frames += chunk.frames
        }
        #expect(frames > 0)
        // The fake leads with 50 ms of silence; the worker keeps at most a 30 ms pre-roll.
        #expect(frames < Int(24_000 * (0.12 + 0.05 * 5 + 0.05)))
        #expect(synth.failure == nil)
    }

    @Test func unpreparedVoiceFailsOnlyThatUnit() async throws {
        let synth = try await fakeWorker()
        defer { synth.shutdown() }
        let missing = PreparedVoice(speakerID: SpeakerID("S9"), profileVersion: 3, handle: 3)
        await #expect(throws: (any Error).self) {
            for try await _ in synth.synthesize(unit: UnitID(1), text: "Hello.", voice: missing) {}
        }
        let voice = try await synth.prepare(profile())
        var frames = 0
        for try await chunk in synth.synthesize(unit: UnitID(2), text: "Hello there.", voice: voice) { frames += chunk.frames }
        #expect(frames > 0 && synth.failure == nil)
    }

    @Test func invalidReferenceIsRejected() async throws {
        let synth = try await fakeWorker()
        defer { synth.shutdown() }
        await #expect(throws: (any Error).self) { _ = try await synth.prepare(profile(seconds: 0.5)) }
        #expect(synth.failure == nil)
    }

    @Test func abandoningAStreamCancelsAndLaterUnitsStillRun() async throws {
        let synth = try await fakeWorker()
        defer { synth.shutdown() }
        let voice = try await synth.prepare(profile())
        let long = String(repeating: "word ", count: 400)
        for try await _ in synth.synthesize(unit: UnitID(1), text: long, voice: voice) { break }
        let started = ContinuousClock.now
        var frames = 0
        for try await chunk in synth.synthesize(unit: UnitID(2), text: "Next.", voice: voice) { frames += chunk.frames }
        // Uncanceled, the long unit alone would stream for about 5 s in the fake (RTF 0.25).
        #expect(ContinuousClock.now - started < .seconds(3))
        #expect(frames > 0)
    }

    @Test func workerExitFailsPendingAndLaterRequests() async throws {
        let synth = try await fakeWorker()
        let voice = try await synth.prepare(profile())
        synth.shutdown()
        await #expect(throws: (any Error).self) {
            for try await _ in synth.synthesize(unit: UnitID(1), text: "After exit.", voice: voice) {}
        }
        #expect(synth.failure != nil)
    }

    @Test func readyTimeoutFailsStartup() async throws {
        await #expect(throws: (any Error).self) {
            _ = try await CloneVoiceSynthesizer(executable: URL(fileURLWithPath: "/bin/sleep"), arguments: ["30"],
                                                readyTimeout: .milliseconds(300))
        }
    }
}
