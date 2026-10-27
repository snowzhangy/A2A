// Host-side admission of synthesized PCM (section 4.2). A chunk is accepted only when its
// epoch AND generation equal the host's current values and its unit has not been canceled.

public enum Admission: Hashable, Sendable {
    case accepted
    case staleEpoch(chunk: SessionEpoch, current: SessionEpoch)
    case staleGeneration(chunk: PlayoutGeneration, current: PlayoutGeneration)
    case canceledUnit(UnitID)
    case alreadyFinished(UnitID)
    case retiredUnit(UnitID)

    public var isAccepted: Bool { self == .accepted }

    public var reason: String {
        switch self {
        case .accepted: "accepted"
        case .staleEpoch: "stale_epoch"
        case .staleGeneration: "stale_generation"
        case .canceledUnit: "canceled_unit"
        case .alreadyFinished: "already_finished"
        case .retiredUnit: "retired_unit"
        }
    }
}

public struct PlayoutGate: Sendable {
    public private(set) var epoch: SessionEpoch
    public private(set) var generation: PlayoutGeneration
    private var canceled: Set<UnitID> = []
    private var finished: Set<UnitID> = []
    /// Pruned IDs coalesce into ranges; unfinished units may leave holes between them.
    private var retired: [ClosedRange<Int>] = []

    public init(epoch: SessionEpoch = 1, generation: PlayoutGeneration = 1) {
        self.epoch = epoch
        self.generation = generation
    }

    public func admit(_ header: PCMChunkHeader) -> Admission {
        if header.sessionEpoch != epoch {
            return .staleEpoch(chunk: header.sessionEpoch, current: epoch)
        }
        if header.playoutGeneration != generation {
            return .staleGeneration(chunk: header.playoutGeneration, current: generation)
        }
        if canceled.contains(header.unitID) { return .canceledUnit(header.unitID) }
        if finished.contains(header.unitID) { return .alreadyFinished(header.unitID) }
        if retired.contains(where: { $0.contains(header.unitID.rawValue) }) { return .retiredUnit(header.unitID) }
        return .accepted
    }

    /// Source/device discontinuity or restart. Unit bookkeeping restarts with the epoch.
    public mutating func advanceEpoch() {
        epoch &+= 1
        generation &+= 1
        canceled.removeAll()
        finished.removeAll()
        retired.removeAll()
    }

    /// Fallback or speaker handoff.
    public mutating func advanceGeneration() {
        generation &+= 1
    }

    public mutating func cancel(_ unit: UnitID) {
        canceled.insert(unit)
    }

    /// Once the last chunk of a unit is played the unit is immutable.
    public mutating func markFinished(_ unit: UnitID) {
        finished.insert(unit)
    }

    public func isCanceled(_ unit: UnitID) -> Bool { canceled.contains(unit) }

    /// The reducer has removed this terminal unit's record. Keep rejecting its PCM
    /// while replacing individual tombstones with compact, coalesced ranges.
    mutating func forgetTerminalUnit(_ unit: UnitID) {
        canceled.remove(unit)
        finished.remove(unit)
        var lower = unit.rawValue, upper = unit.rawValue
        var start = 0
        while start < retired.count, retired[start].upperBound < lower,
              retired[start].upperBound + 1 < lower { start += 1 }
        var end = start
        while end < retired.count {
            let range = retired[end]
            guard range.lowerBound <= upper || (upper < Int.max && range.lowerBound == upper + 1) else { break }
            lower = min(lower, range.lowerBound)
            upper = max(upper, range.upperBound)
            end += 1
        }
        retired.replaceSubrange(start..<end, with: [lower...upper])
    }
}
