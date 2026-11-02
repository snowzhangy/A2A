"""Host-side admission of synthesized PCM. Port of apple/Sources/A2ACore/PlayoutGate.swift.

A chunk is accepted only when its epoch AND generation equal the host's current values and
its unit has not been canceled.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import UINT32_MASK, PCMChunkHeader, UnitID


@dataclass(frozen=True, slots=True)
class Admission:
    reason: str  # accepted | stale_epoch | stale_generation | canceled_unit | already_finished | retired_unit

    @property
    def is_accepted(self) -> bool:
        return self.reason == "accepted"


ACCEPTED = Admission("accepted")


class PlayoutGate:
    def __init__(self, epoch: int = 1, generation: int = 1) -> None:
        self.epoch = epoch
        self.generation = generation
        self._canceled: set[UnitID] = set()
        self._finished: set[UnitID] = set()
        # Pruned IDs coalesce into sorted, disjoint [lo, hi] ranges.
        self._retired: list[tuple[int, int]] = []

    def admit(self, header: PCMChunkHeader) -> Admission:
        if header.session_epoch != self.epoch:
            return Admission("stale_epoch")
        if header.playout_generation != self.generation:
            return Admission("stale_generation")
        if header.unit_id in self._canceled:
            return Admission("canceled_unit")
        if header.unit_id in self._finished:
            return Admission("already_finished")
        raw = header.unit_id.raw_value
        if any(lo <= raw <= hi for lo, hi in self._retired):
            return Admission("retired_unit")
        return ACCEPTED

    def advance_epoch(self) -> None:
        """Source/device discontinuity or restart. Unit bookkeeping restarts with the epoch."""
        self.epoch = (self.epoch + 1) & UINT32_MASK
        self.generation = (self.generation + 1) & UINT32_MASK
        self._canceled.clear()
        self._finished.clear()
        self._retired.clear()

    def advance_generation(self) -> None:
        """Fallback or speaker handoff."""
        self.generation = (self.generation + 1) & UINT32_MASK

    def cancel(self, unit: UnitID) -> None:
        self._canceled.add(unit)

    def mark_finished(self, unit: UnitID) -> None:
        """Once the last chunk of a unit is played the unit is immutable."""
        self._finished.add(unit)

    def is_canceled(self, unit: UnitID) -> bool:
        return unit in self._canceled

    def forget_terminal_unit(self, unit: UnitID) -> None:
        """The reducer has removed this terminal unit's record. Keep rejecting its PCM while
        replacing individual tombstones with compact, coalesced ranges."""
        self._canceled.discard(unit)
        self._finished.discard(unit)
        lo = hi = unit.raw_value
        retired = self._retired
        start = 0
        while start < len(retired) and retired[start][1] < lo and retired[start][1] + 1 < lo:
            start += 1
        end = start
        while end < len(retired):
            r_lo, r_hi = retired[end]
            if not (r_lo <= hi or r_lo == hi + 1):
                break
            lo, hi = min(lo, r_lo), max(hi, r_hi)
            end += 1
        retired[start:end] = [(lo, hi)]

    @property
    def retired_ranges(self) -> list[tuple[int, int]]:
        return list(self._retired)
