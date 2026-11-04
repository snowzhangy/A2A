// One-shot voice isolation for audio-only speaker references (ADR-0016). Runs Apple's
// AUSoundIsolation, so it is opt-in like the other framework-backed checks:
//   A2A_RUN_APPLE_TESTS=1 swift test --package-path apple --filter VoiceIsolationTests

import A2AModels
import Foundation
import Testing

@Suite(.enabled(if: ProcessInfo.processInfo.environment["A2A_RUN_APPLE_TESTS"] == "1", "set A2A_RUN_APPLE_TESTS=1"))
struct VoiceIsolationTests {
    private static func rmsDB(_ x: ArraySlice<Float>) -> Float {
        let mean = x.reduce(0) { $0 + $1 * $1 } / Float(max(1, x.count))
        return mean > 0 ? 10 * log10(mean) : -.infinity
    }

    @Test func outputIsAlignedToTheInputWithoutTheWarmUp() throws {
        let preroll = 24_000
        let input = (0..<(preroll + 96_000)).map { i in Float(0.1 * sin(Double(i) / 30)) }
        let voice = try BackgroundSeparator.isolateVoice(input, preroll: preroll)
        #expect(voice.count == input.count - preroll)
        #expect(voice.allSatisfy { $0.isFinite && abs($0) <= 1 })
    }

    @Test func aSteadyToneIsNotAVoice() throws {
        // The gate's premise: what is not speech leaves little after isolation.
        let input = (0..<120_000).map { i in Float(0.2 * sin(2 * Double.pi * 440 * Double(i) / 48_000)) }
        let voice = try BackgroundSeparator.isolateVoice(input, preroll: 24_000)
        #expect(Self.rmsDB(voice[...]) - Self.rmsDB(input[24_000...]) < -10)
    }

    @Test func emptyOrNonFiniteInputIsRefused() {
        #expect(throws: (any Error).self) { try BackgroundSeparator.isolateVoice([]) }
        #expect(throws: (any Error).self) { try BackgroundSeparator.isolateVoice([0.1, 0.2], preroll: 2) }
        #expect(throws: (any Error).self) { try BackgroundSeparator.isolateVoice([0.1, .nan, 0.2]) }
    }
}
