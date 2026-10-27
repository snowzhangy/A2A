import A2ACore
import Testing

private let s1 = SpeakerID("S1")
private let s2 = SpeakerID("S2")
private let sec: UInt64 = 1_000_000_000

private func ready(_ unit: Int, spk: SpeakerID = s1, gen: PlayoutGeneration = 1, buffered: Double = 0.1,
                   complete: Bool = false) -> SessionInput {
    .unitReady(UnitReadiness(unit: UnitID(unit), speaker: spk, generation: gen,
                             bufferedSeconds: buffered, complete: complete))
}

private func listening() -> SessionStateMachine {
    var m = SessionStateMachine()
    _ = m.handle(.arm, at: 0)
    _ = m.handle(.armSucceeded, at: 0)
    return m
}

@Suite struct SessionStateMachineTests {
    @Test func armReachesListeningWithOriginalBus() {
        var m = SessionStateMachine()
        _ = m.handle(.arm, at: 0)
        #expect(m.state == .arming)
        let fx = m.handle(.armSucceeded, at: 1)
        #expect(m.state == .listening)
        #expect(fx.contains(.rampGains(.original, durationMs: 0)))
    }

    @Test func profileReadyAloneDoesNotStartTranslation() {
        var m = listening()
        _ = m.handle(.speech(s1), at: 0)
        #expect(m.state == .acquiringVoice(s1))
        let fx = m.handle(.profileReady(s1, version: 1), at: 1)
        #expect(fx.isEmpty)
        #expect(m.state == .acquiringVoice(s1))
    }

    @Test func handoffNeedsProfilePrebufferAndCurrentGeneration() {
        var m = listening()
        _ = m.handle(.speech(s1), at: 0)
        // PCM without a profile: stays acquiring.
        _ = m.handle(ready(1), at: 1)
        #expect(m.state == .acquiringVoice(s1))
        _ = m.handle(.profileReady(s1, version: 1), at: 2)
        // Below the 80 ms prebuffer and not complete.
        _ = m.handle(ready(2, buffered: 0.02), at: 3)
        #expect(m.state == .acquiringVoice(s1))
        // Stale generation.
        _ = m.handle(ready(3, gen: 0), at: 4)
        #expect(m.state == .acquiringVoice(s1))
        let fx = m.handle(ready(4), at: 5)
        #expect(m.state == .translating(s1))
        #expect(fx.contains(.handoff(unit: UnitID(4))))
        #expect(fx.contains(.rampGains(.translated, durationMs: 30)))
    }

    @Test func shortCompleteUnitIsReadyBelowPrebuffer() {
        var m = listening()
        _ = m.handle(.profileReady(s1, version: 1), at: 0)
        _ = m.handle(.speech(s1), at: 0)
        _ = m.handle(ready(1, buffered: 0.03, complete: true), at: 1)
        #expect(m.state == .translating(s1))
    }

    private func translating() -> SessionStateMachine {
        var m = listening()
        _ = m.handle(.profileReady(s1, version: 1), at: 0)
        _ = m.handle(.speech(s1), at: 0)
        _ = m.handle(ready(1), at: 0)
        return m
    }

    @Test func debtAboveThresholdDegradesAndAdvancesGeneration() {
        var m = translating()
        #expect(m.handle(.health(HealthSample(debtSeconds: 2.9, sourceLagSeconds: 1)), at: sec).isEmpty)
        let fx = m.handle(.health(HealthSample(debtSeconds: 3.1, sourceLagSeconds: 1)), at: 2 * sec)
        #expect(m.state == .degraded)
        #expect(fx.contains(.advanceGeneration))
        #expect(fx.contains(.cancelPendingSynthesis))
        #expect(fx.contains(.fallbackEnter(reason: "debt")))
        #expect(fx.contains(.rampGains(.original, durationMs: 30)))
    }

    @Test func sourceLagUnderrunAndServiceLossDegrade() {
        for sample in [HealthSample(debtSeconds: 0, sourceLagSeconds: 4.5),
                       HealthSample(debtSeconds: 0, sourceLagSeconds: 0, underrun: true),
                       HealthSample(debtSeconds: 0, sourceLagSeconds: 0, serviceLost: true)] {
            var m = translating()
            _ = m.handle(.health(sample), at: sec)
            #expect(m.state == .degraded)
        }
    }

    @Test func recoveryRequiresStableIntervalHealthyWorkerAndFreshUnit() {
        var m = translating()
        _ = m.handle(.health(HealthSample(debtSeconds: 5, sourceLagSeconds: 0)), at: sec)
        m.syncGeneration(2)
        let healthy = HealthSample(debtSeconds: 0, sourceLagSeconds: 0)
        _ = m.handle(.health(healthy), at: 2 * sec)
        // Fresh unit but stable for < 2 s.
        _ = m.handle(ready(5, gen: 2), at: 3 * sec)
        #expect(m.state == .degraded)
        // Unhealthy sample restarts the stable interval.
        _ = m.handle(.health(HealthSample(debtSeconds: 4, sourceLagSeconds: 0)), at: 3 * sec + 1)
        _ = m.handle(.health(healthy), at: 4 * sec)
        _ = m.handle(ready(6, gen: 2), at: 5 * sec)
        #expect(m.state == .degraded)
        // Stale-generation unit cannot be used for recovery.
        _ = m.handle(ready(7, gen: 1), at: 6 * sec + 1)
        #expect(m.state == .degraded)
        let fx = m.handle(ready(8, gen: 2), at: 6 * sec + 1)
        #expect(m.state == .translating(s1))
        #expect(fx.contains(.fallbackExit(unit: UnitID(8))))
    }

    @Test func workerLossBlocksRecovery() {
        var m = translating()
        _ = m.handle(.workerHealthy(false), at: sec)
        #expect(m.state == .degraded)
        m.syncGeneration(2)
        _ = m.handle(.health(HealthSample(debtSeconds: 0, sourceLagSeconds: 0)), at: 2 * sec)
        _ = m.handle(ready(2, gen: 2), at: 5 * sec)
        #expect(m.state == .degraded)
        _ = m.handle(.workerHealthy(true), at: 5 * sec)
        _ = m.handle(.health(HealthSample(debtSeconds: 0, sourceLagSeconds: 0)), at: 5 * sec)
        _ = m.handle(ready(3, gen: 2), at: 8 * sec)
        #expect(m.state == .translating(s1))
    }

    @Test func speakerChangeWithoutProfileReturnsToOriginal() {
        var m = translating()
        let fx = m.handle(.speakerChange(s2), at: sec)
        #expect(m.state == .acquiringVoice(s2))
        #expect(fx.contains(.advanceGeneration))
        #expect(fx.contains(.rampGains(.original, durationMs: 30)))
        // S1's PCM cannot hand off to S2.
        m.syncGeneration(2)
        _ = m.handle(ready(4, spk: s1, gen: 2), at: 2 * sec)
        #expect(m.state == .acquiringVoice(s2))
    }

    @Test func stopFromAnyStateTearsDown() {
        var m = translating()
        let fx = m.handle(.stop, at: sec)
        #expect(m.state == .idle)
        #expect(fx.contains(.teardown))
        #expect(m.handle(.stop, at: sec).isEmpty)
    }
}
