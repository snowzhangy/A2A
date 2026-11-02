// DSP speaker tracking (VoiceprintSpeakerTracker) on synthetic source-filter voices, and
// system-voice casting (VoiceCasting).

import A2ACore
import A2AMedia
import Foundation
import Testing
@testable import A2AModels

/// Vowel-like speech: a glottal pulse train with a spectral tilt through three formant
/// resonators. `scale` stretches the formants (shorter vocal tract above 1).
private func voice(f0: Double, scale: Double, syllables: Int, offset: Int = 0, pitchDrift: Double = 0) -> [Float] {
    let rate = 16_000.0
    let vowels: [(Double, Double, Double)] = [(730, 1090, 2440), (270, 2290, 3010), (300, 870, 2240),
                                             (530, 1840, 2480), (570, 840, 2410)]
    var out: [Float] = []
    var phase = 0.0
    for s in 0..<syllables {
        let v = vowels[(s * 3 + offset) % vowels.count]
        let n = Int(0.2 * rate)
        let f = f0 * (1 + pitchDrift * sin(Double(s + offset)))
        var x = [Double](repeating: 0, count: n)
        var lp1 = 0.0, lp2 = 0.0
        for i in 0..<n {
            phase += f / rate
            var pulse = 0.0
            if phase >= 1 { phase -= 1; pulse = 1 }
            lp1 = 0.9 * lp1 + pulse
            lp2 = 0.9 * lp2 + lp1
            x[i] = lp2
        }
        for (fc, bw) in [(v.0 * scale, 80.0), (v.1 * scale, 100.0), (v.2 * scale, 120.0)] {
            let r = exp(-Double.pi * bw / rate), theta = 2 * Double.pi * fc / rate
            let a1 = 2 * r * cos(theta), a2 = -r * r
            var y1 = 0.0, y2 = 0.0
            for i in 0..<n {
                let y = (1 - r) * x[i] + a1 * y1 + a2 * y2
                y2 = y1; y1 = y; x[i] = y
            }
        }
        let ramp = 0.03 * rate
        for i in 0..<n { x[i] *= min(1, Double(min(i, n - 1 - i)) / ramp) }
        out += x.map { Float($0) }
    }
    // Speech level about -20 dBFS: pulse trains are spiky, so normalize by RMS, not peak.
    let rms = (out.reduce(0) { $0 + $1 * $1 } / Float(out.count)).squareRoot()
    return out.map { $0 * 0.1 / rms }
}

/// Feeds 16 kHz audio in 20 ms frames through EnergyVAD and the tracker.
private func track(_ audio: [Float], tracker: inout VoiceprintSpeakerTracker) -> [(SpeakerID, SourceSpan)] {
    var vad = EnergyVAD()
    var labels: [(SpeakerID, SourceSpan)] = []
    let chunk = 320
    for start in stride(from: 0, to: audio.count, by: chunk) {
        let samples = Array(audio[start..<min(audio.count, start + chunk)])
        let frame = AudioFrame16k(span: SourceSpan(start: Int64(start * 3), end: Int64((start + samples.count) * 3)),
                                  samples: samples)
        for e in tracker.process(frame, vad: vad.process(frame)) {
            if case .assign(let s, let span) = e { labels.append((s, span)) }
        }
    }
    return labels
}

private let gap = [Float](repeating: 0, count: 9_600)   // 0.6 s

@Suite struct SpeakerTrackingTests {
    @Test func analyzerFindsThePitchOfVoicedSpeech() throws {
        var analyzer = VoiceprintAnalyzer()
        var print: Voiceprint? = Voiceprint()
        analyzer.process(voice(f0: 140, scale: 1, syllables: 8), into: &print)
        let p = try #require(print)
        #expect(p.voicedFrames > 100)
        let pitch = try #require(p.medianPitchHz)
        #expect(abs(pitch - 140) / 140 < 0.04)

        var fresh = VoiceprintAnalyzer()
        var silent: Voiceprint? = Voiceprint()
        fresh.process([Float](repeating: 0, count: 16_000), into: &silent)
        #expect(silent?.voicedFrames == 0)
    }

    /// Alternating turns of two voices.
    private func dialogue(_ a: (Double, Double), _ b: (Double, Double), turns: Int = 8) -> [Float] {
        var audio: [Float] = []
        for turn in 0..<turns {
            let (f0, scale) = turn % 2 == 1 ? b : a
            audio += voice(f0: f0, scale: scale, syllables: 8, offset: turn) + gap
        }
        return audio
    }

    @Test func aSecondVoiceIsConfirmedByItsSecondTurn() {
        var tracker = VoiceprintSpeakerTracker()
        let labels = track(dialogue((115, 1), (215, 1.18)), tracker: &tracker)
        // Clearly different but not overwhelmingly: the first turn stays with S1 until the
        // second one confirms the new voice; every later turn alternates.
        #expect(labels.map(\.0.rawValue) == ["S1", "S1", "S1", "S2", "S1", "S2", "S1", "S2"])
        #expect(tracker.speakerCount == 2)
        #expect(abs((tracker.pitchHz(of: SpeakerID("S1")) ?? 0) - 115) < 8)
        #expect(abs((tracker.pitchHz(of: SpeakerID("S2")) ?? 0) - 215) < 12)
    }

    @Test func twoOddSegmentsFarApartDoNotMakeASpeaker() {
        var tracker = VoiceprintSpeakerTracker()
        var audio: [Float] = []
        // The other voice appears twice, about 20 s apart: too far apart to confirm each other.
        for turn in 0..<11 {
            let odd = turn == 1 || turn == 10
            audio += (odd ? voice(f0: 215, scale: 1.18, syllables: 8, offset: turn)
                          : voice(f0: 115, scale: 1, syllables: 8, offset: turn)) + gap
        }
        let labels = track(audio, tracker: &tracker)
        #expect(labels.count == 11)
        #expect(Set(labels.map(\.0)) == [SpeakerID("S1")])
    }

    @Test func aVeryDifferentVoiceIsANewSpeakerAtOnce() {
        var tracker = VoiceprintSpeakerTracker()
        let labels = track(dialogue((100, 0.9), (260, 1.35)), tracker: &tracker)
        #expect(labels.map(\.0.rawValue) == ["S1", "S2", "S1", "S2", "S1", "S2", "S1", "S2"])
    }

    @Test func oneVoiceWithVaryingPitchAndVowelsStaysOneSpeaker() {
        var tracker = VoiceprintSpeakerTracker()
        var audio: [Float] = []
        for turn in 0..<12 { audio += voice(f0: 125, scale: 1, syllables: 6 + turn % 4, offset: turn, pitchDrift: 0.12) + gap }
        let labels = track(audio, tracker: &tracker)
        #expect(labels.count == 12)
        #expect(Set(labels.map(\.0)) == [SpeakerID("S1")])
    }

    @Test func shortBlipsAreNotLabeledAndSpeakersAreCapped() {
        var tracker = VoiceprintSpeakerTracker()
        // 0.2 s of voice is below the evidence needed to label anything.
        #expect(track(voice(f0: 120, scale: 1, syllables: 1) + gap, tracker: &tracker).isEmpty)
        var audio: [Float] = []
        let voices: [(Double, Double)] = [(100, 0.92), (230, 1.25), (150, 1.05), (300, 1.4), (180, 1.12), (90, 0.85)]
        for round in 0..<2 {
            for (f0, scale) in voices { audio += voice(f0: f0, scale: scale, syllables: 8, offset: round) + gap }
        }
        _ = track(audio, tracker: &tracker)
        #expect(tracker.speakerCount <= VoiceprintSpeakerTracker.Config().maxSpeakers)
        #expect(tracker.speakerCount >= 2)
    }

    /// Two speakers talk, a passing voice takes the last slot, and after a long quiet a new voice
    /// arrives. The passing voice gives up its slot; its ID is never handed out again.
    @Test func aPassingVoiceGivesItsSlotToANewSpeaker() {
        let regulars: [(Double, Double)] = [(100, 0.9), (260, 1.35)]
        var audio: [Float] = []
        for turn in 0..<8 { audio += voice(f0: regulars[turn % 2].0, scale: regulars[turn % 2].1, syllables: 8, offset: turn) + gap }
        for turn in 0..<2 { audio += voice(f0: 300, scale: 1.5, syllables: 8, offset: turn) + gap }
        let opening = audio.count
        audio += [Float](repeating: 0, count: 16_000 * 31)
        for turn in 0..<4 {
            audio += voice(f0: 190, scale: 0.8, syllables: 8, offset: turn) + gap
            audio += voice(f0: regulars[turn % 2].0, scale: regulars[turn % 2].1, syllables: 8, offset: turn) + gap
        }
        var config = VoiceprintSpeakerTracker.Config()
        config.maxSpeakers = 3
        func run(_ config: VoiceprintSpeakerTracker.Config) -> (before: Set<String>, after: [String], count: Int) {
            var tracker = VoiceprintSpeakerTracker(config: config)
            let labels = track(audio, tracker: &tracker)
            let split = Int64(opening * 3)
            return (Set(labels.filter { $0.1.end <= split }.map(\.0.rawValue)),
                    labels.filter { $0.1.start > split }.map(\.0.rawValue), tracker.speakerCount)
        }
        let recycled = run(config)
        #expect(recycled.before == ["S1", "S2", "S3"])
        // Once its second turn confirms it, the newcomer is S4; the regulars keep their own IDs.
        #expect(recycled.after.dropFirst() == ["S1", "S4", "S2", "S4", "S1", "S4", "S2"])
        #expect(recycled.count == 3)

        config.staleSegments = 0
        let capped = run(config)
        #expect(capped.before == recycled.before)
        // With no slot to free, the newcomer is read as someone already known.
        #expect(Set(capped.after).isSubset(of: capped.before))
    }

    @Test func shortRepliesCanReuseAKnownVoiceWithoutInventingAnotherSpeaker() {
        var tracker = VoiceprintSpeakerTracker()
        let initial = track(voice(f0: 120, scale: 1, syllables: 8) + gap, tracker: &tracker)
        #expect(initial.count == 1)
        let replies = track(voice(f0: 120, scale: 1, syllables: 2) + gap, tracker: &tracker)
        #expect(replies.count == 1)
        #expect(replies.first?.0 == SpeakerID("S1"))
        #expect(tracker.speakerCount == 1)
    }

    @Test func longSpeechIsLabeledInPieces() {
        var tracker = VoiceprintSpeakerTracker()
        let labels = track(voice(f0: 120, scale: 1, syllables: 40) + gap, tracker: &tracker)
        #expect(labels.count >= 2)
        #expect(labels.allSatisfy { $0.1.count <= 3 * canonicalSampleRate + 960 })
        #expect(Set(labels.map(\.0)).count == 1)
    }
}

@Suite struct VoiceCastingTests {
    private func installed(_ id: String, _ name: String, _ language: String, _ gender: VoiceCasting.Gender?,
                           quality: Int = 1, novelty: Bool = false, personal: Bool = false) -> VoiceCasting.InstalledVoice {
        VoiceCasting.InstalledVoice(identifier: id, name: name, language: language, gender: gender, quality: quality,
                                    novelty: novelty, personal: personal)
    }

    private var english: [VoiceCasting.InstalledVoice] {
        [installed("com.apple.voice.compact.en-US.Samantha", "Samantha", "en-US", .female),
         installed("com.apple.voice.compact.en-GB.Daniel", "Daniel", "en-GB", .male),
         installed("com.apple.voice.super-compact.en-AU.Karen", "Karen", "en-AU", .female),
         installed("com.apple.speech.synthesis.voice.Fred", "Fred", "en-US", .male),
         installed("com.apple.eloquence.en-US.Reed", "Reed", "en-US", nil),
         installed("com.apple.speech.synthesis.voice.Albert", "Albert", "en-US", nil, novelty: true),
         installed("com.apple.voice.personal.me", "My Voice", "en-US", .female, personal: true),
         installed("com.apple.voice.compact.zh-CN.Tingting", "Tingting", "zh-CN", .female)]
    }

    @Test func candidatesExcludeNoveltyPersonalAndOtherLanguages() {
        let c = VoiceCasting(candidates: VoiceCasting.candidates(from: english, locale: "en-US")).candidates
        #expect(c.map(\.name) == ["Samantha", "Daniel", "Karen", "Fred", "Reed"])
        #expect(c.first { $0.name == "Reed" }?.gender == .male)
        let mandarin = VoiceCasting.candidates(from: english + [
            installed("com.apple.voice.compact.zh-HK.Sinji", "Sinji", "zh-HK", .female),
            installed("com.apple.eloquence.zh-CN.Rocko", "Rocko", "zh-CN", nil)], locale: "zh-CN")
        #expect(mandarin.map(\.name).sorted() == ["Rocko", "Tingting"])
    }

    @Test func speakersGetDistinctVoicesMatchedToTheirPitch() {
        var casting = VoiceCasting(candidates: VoiceCasting.candidates(from: english, locale: "en-US"))
        let low = casting.cast(pitchHz: 120)!
        let high = casting.cast(pitchHz: 230)!
        let high2 = casting.cast(pitchHz: 250)!
        #expect(casting.roles[low].identifier.hasSuffix("Daniel"))
        #expect(casting.roles[high].identifier.hasSuffix("Samantha"))
        #expect(casting.roles[high2].identifier.hasSuffix("Karen"))
        #expect(Set(casting.roles.map(\.identifier)).count == 3)
        // The female pool is exhausted: the next female speaker shares a voice at another pitch.
        let high3 = casting.cast(pitchHz: 260)!
        #expect(casting.roles[high3].pitchMultiplier != 1)
    }

    @Test func ambiguousPitchGoesToTheLessCastGender() {
        var casting = VoiceCasting(candidates: VoiceCasting.candidates(from: english, locale: "en-US"))
        _ = casting.cast(pitchHz: 220)
        let next = casting.cast(pitchHz: 185)!
        #expect(casting.roles[next].identifier.hasSuffix("Daniel"))
    }
}

@Suite struct VoiceMemoryTests {
    private func printFor(_ samples: [Float]) -> Voiceprint {
        var analyzer = VoiceprintAnalyzer()
        var print: Voiceprint? = Voiceprint()
        analyzer.process(samples, into: &print)
        return print!
    }

    private func profile(_ samples: [Float]) -> VoiceProfile {
        VoiceProfile(speakerID: SpeakerID("S1"), version: 1, referencePCM: samples,
                     referenceText: "Synthetic reference.", sampleRate: 16_000, channelCount: 1,
                     quality: ReferenceQuality(durationSeconds: Double(samples.count) / 16_000,
                                               snrEstimateDB: .nan, clippedFraction: 0, overlapSuspected: false))
    }

    @Test func savedReferenceMatchesTheVoiceRatherThanItsSessionNumber() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("voice-memory-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let samples = voice(f0: 120, scale: 1, syllables: 16)
        let store = try VoiceMemoryStore(directory: root)
        let id = UUID()
        store.remember(profile(samples), id: id)
        store.flush()
        #expect(store.count == 1 && !store.saveFailed)
        let reopened = try VoiceMemoryStore(directory: root)
        let saved = try #require(reopened.match(printFor(samples)))
        #expect(saved.id == id)
        let restored = try #require(saved.profile(for: SpeakerID("S4")))
        #expect(restored.speakerID == SpeakerID("S4"))
        #expect(restored.referencePCM == samples)
        #expect(reopened.match(printFor(voice(f0: 260, scale: 1.35, syllables: 16))) == nil)
        let file = root.appendingPathComponent(id.uuidString + ".voice")
        let permissions = try FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? NSNumber
        #expect(permissions?.intValue == 0o600)
    }

    @Test func ambiguousMatchesAreRejectedAndCorruptFilesAreIgnored() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("voice-memory-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let samples = voice(f0: 120, scale: 1, syllables: 16)
        let store = try VoiceMemoryStore(directory: root)
        store.remember(profile(samples), id: UUID())
        store.remember(profile(samples), id: UUID())
        store.flush()
        #expect(store.count == 2)
        #expect(store.match(printFor(samples)) == nil)
        let bad = root.appendingPathComponent(UUID().uuidString + ".voice")
        try Data("invalid plist".utf8).write(to: bad)
        #expect(try VoiceMemoryStore(directory: root).count == 2)
    }

    @Test func retainedVoicesAreBounded() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("voice-memory-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let p = profile(voice(f0: 120, scale: 1, syllables: 8))
        let store = try VoiceMemoryStore(directory: root)
        for _ in 0...VoiceMemoryStore.maximumVoices { store.remember(p, id: UUID()) }
        store.flush()
        #expect(store.count == VoiceMemoryStore.maximumVoices)
        #expect(try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil).count == VoiceMemoryStore.maximumVoices)
    }

    @Test func upgradingAReferencePreservesTheFirstExemplarWithoutCreatingAmbiguity() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("voice-memory-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let samples = voice(f0: 120, scale: 1, syllables: 16)
        let first = profile(samples)
        let upgraded = VoiceProfile(speakerID: first.speakerID, version: 2, referencePCM: samples,
                                    referenceText: first.referenceText, sampleRate: first.sampleRate,
                                    channelCount: 1, quality: first.quality)
        let store = try VoiceMemoryStore(directory: root), identity = UUID()
        store.remember(first, id: identity)
        store.remember(upgraded, id: identity)
        store.flush()
        #expect(store.count == 1)
        #expect(try FileManager.default.contentsOfDirectory(at: root, includingPropertiesForKeys: nil).count == 2)
        #expect(try VoiceMemoryStore(directory: root).match(printFor(samples))?.identityID == identity)
    }
}

/// The energy VAD, the segmenter and the stream feed together: a streaming translator hears every
/// closed span exactly once, in order, and never a sample past the end of the span it belongs to.
@Suite struct AudioStreamFeedVADTests {
    @Test func streamedAudioTilesEveryClosedSpanAndNothingElse() {
        var rng = SystemRandomNumberGenerator()
        for trial in 0..<20 {
            var vad = EnergyVAD()
            var segments = AudioSegmentPolicy(maxSeconds: 3, minSeconds: 0.25)
            var feed = AudioStreamFeed(maxSamples: segments.maxSamples, minAppendSeconds: trial % 2 == 0 ? 0.1 : 0)
            var pending: [SourceSpan] = []   // appends since the last close or discard
            var closed: [SourceSpan] = []
            var heard: [SourceSpan] = []
            var position: Int64 = 0
            var voiced = false
            var run = 0
            for _ in 0..<1_500 {
                if run == 0 {
                    voiced.toggle()
                    run = Int.random(in: voiced ? 3...400 : 1...60, using: &rng)   // 10 ms blocks
                }
                run -= 1
                let block = Int64([480, 512, 960].randomElement(using: &rng)!)
                let span = SourceSpan(start: position, end: position + block)
                position += block
                let frame = AudioFrame16k(span: span, samples: [Float](repeating: voiced ? 0.1 : 0.0001, count: Int(block / 3)))
                let events = vad.process(frame)
                let spans = segments.process(events: events, frontier: span.end)
                closed += spans
                for action in feed.update(closed: spans, openStart: segments.openStart, voicedEnd: vad.voicedEnd) {
                    switch action {
                    case .append(let s): pending.append(s)
                    case .discard: pending.removeAll()
                    case .close(let s):
                        // Contiguous appends from the span's start to its end.
                        #expect(pending.first?.start == s.start && pending.last?.end == s.end)
                        #expect(zip(pending, pending.dropFirst()).allSatisfy { $0.end == $1.start })
                        heard.append(s)
                        pending.removeAll()
                    }
                }
            }
            #expect(heard == closed)
        }
    }
}
