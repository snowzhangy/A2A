// Duration control of the system voice in fluent playout. Pure mapping only: no
// AVSpeechSynthesizer is created, so these run headless.

@testable import A2AModels
import Testing

@Suite struct SystemVoicePacingTests {
    @Test func rateInvertsTheMeasuredSpeedTable() {
        for point in SystemVoiceSynthesizer.speedForRate {
            #expect(abs(SystemVoiceSynthesizer.rate(forSpeed: point.speed) - point.rate) < 1e-4)
        }
        #expect(SystemVoiceSynthesizer.rate(forSpeed: 0.5) == 0.5)   // never slower than default
        let rates = stride(from: 1.0, through: 1.6, by: 0.05).map { SystemVoiceSynthesizer.rate(forSpeed: $0) }
        #expect(zip(rates, rates.dropFirst()).allSatisfy { $0 <= $1 })
    }

    @Test func speedFitsTheSourceDurationWithinTheNaturalnessCap() {
        let text = String(repeating: "word ", count: 12)   // 60 characters, about 3.5 s natural
        let tight = SystemVoiceSynthesizer.speed(text: text, targetSeconds: 1, postRollSeconds: 0.18,
                                                 secondsPerCharacter: 0.055)
        #expect(tight == SystemVoiceSynthesizer.maxSpeed)
        let fits = SystemVoiceSynthesizer.speed(text: text, targetSeconds: 3, postRollSeconds: 0.18,
                                                secondsPerCharacter: 0.055)
        #expect(fits > 1 && fits < SystemVoiceSynthesizer.maxSpeed)
        // A short acknowledgement keeps its cadence within the tolerance.
        #expect(SystemVoiceSynthesizer.speed(text: "Okay.", targetSeconds: 0.3, postRollSeconds: 0.18,
                                             secondsPerCharacter: 0.055) == 1)
    }
}
