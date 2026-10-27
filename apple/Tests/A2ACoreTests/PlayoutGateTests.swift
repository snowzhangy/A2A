@testable import A2ACore
import Testing

private func header(unit: Int, epoch: SessionEpoch = 1, gen: PlayoutGeneration = 1) -> PCMChunkHeader {
    PCMChunkHeader(unitID: UnitID(unit), speakerID: SpeakerID("S1"), profileVersion: 1, sessionEpoch: epoch,
                   playoutGeneration: gen, format: PCMFormat(sampleRate: 24_000, channelCount: 1),
                   span: SourceSpan(start: 0, end: 48_000), isLast: false)
}

@Suite struct PlayoutGateTests {
    @Test func retiredRangesPreserveHolesForUnfinishedUnits() {
        var gate = PlayoutGate()
        for id in [1, 3, 5, 2, 6, 7] {
            gate.markFinished(UnitID(id))
            gate.forgetTerminalUnit(UnitID(id))
        }
        for id in [1, 2, 3, 5, 6, 7] {
            #expect(gate.admit(header(unit: id)) == .retiredUnit(UnitID(id)))
        }
        #expect(gate.admit(header(unit: 4)) == .accepted)
        gate.forgetTerminalUnit(UnitID(4)) // joins the two ranges
        #expect(gate.admit(header(unit: 4)) == .retiredUnit(UnitID(4)))
        gate.advanceEpoch()
        #expect(gate.admit(header(unit: 4, epoch: 2, gen: gate.generation)) == .accepted)
    }

    @Test func acceptsCurrentEpochAndGeneration() {
        let gate = PlayoutGate()
        #expect(gate.admit(header(unit: 1)) == .accepted)
    }

    @Test func rejectsStaleGenerationAfterFallback() {
        var gate = PlayoutGate()
        gate.advanceGeneration()
        #expect(gate.admit(header(unit: 1, gen: 1)) == .staleGeneration(chunk: 1, current: 2))
        #expect(gate.admit(header(unit: 1, gen: 2)) == .accepted)
    }

    @Test func rejectsStaleEpochEvenWithMatchingGeneration() {
        var gate = PlayoutGate()
        gate.advanceEpoch()
        #expect(gate.epoch == 2)
        #expect(gate.admit(header(unit: 1, epoch: 1, gen: gate.generation)) == .staleEpoch(chunk: 1, current: 2))
    }

    @Test func rejectsCanceledAndFinishedUnits() {
        var gate = PlayoutGate()
        gate.cancel(UnitID(3))
        gate.markFinished(UnitID(4))
        #expect(gate.admit(header(unit: 3)) == .canceledUnit(UnitID(3)))
        #expect(gate.admit(header(unit: 4)) == .alreadyFinished(UnitID(4)))
        #expect(gate.admit(header(unit: 5)).isAccepted)
    }

    @Test func epochAdvanceClearsUnitBookkeeping() {
        var gate = PlayoutGate()
        gate.cancel(UnitID(1))
        gate.advanceEpoch()
        #expect(gate.admit(header(unit: 1, epoch: 2, gen: gate.generation)) == .accepted)
    }
}
