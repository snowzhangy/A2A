"""Scripted reducer driver shared by the core tests (mirrors the Swift test `Scenario`)."""

from __future__ import annotations

from a2a_runtime.core import pipeline as p
from a2a_runtime.core.types import ASRHypothesis, ASRToken, PCMChunkHeader, PCMFormat, SourceSpan, SpeakerID, UnitID

MS = 1_000_000
S1 = SpeakerID("S1")
TTS_FORMAT = PCMFormat(24_000, 1)


class Scenario:
    def __init__(self, config: p.PipelineConfig | None = None) -> None:
        if config is None:
            config = p.PipelineConfig(session="golden")
            config.trace_text = True
        self.pipeline = p.SessionPipeline(config)
        self.t = 0
        self.lines: list[str] = []
        self.effects: list = []
        self.events = []

    def at(self, milliseconds: int, inp) -> list:
        self.t = milliseconds * MS
        out = self.pipeline.handle(inp, self.t)
        for e in out:
            if isinstance(e, p.TraceOut):
                self.lines.append(e.event.json_line())
                self.events.append(e.event)
            else:
                self.effects.append(e)
        return out

    def partial(self, ms: int, text: str, final: bool = False, utt: int = 1, frontier: int | None = None) -> list:
        frontier = ms * 48 if frontier is None else frontier
        tokens = tuple(ASRToken(ch) for ch in text)
        return self.at(ms, p.Hypothesis(ASRHypothesis(utt, tokens, final, frontier)))

    @staticmethod
    def header(unit: int, gen: int, epoch: int = 1, last: bool = False, speaker: SpeakerID = S1,
               prof: int = 1) -> PCMChunkHeader:
        return PCMChunkHeader(UnitID(unit), speaker, prof, epoch, gen, TTS_FORMAT, SourceSpan(0, 0), last)

    def armed(self, profile: bool = True) -> None:
        self.at(0, p.Arm())
        self.at(0, p.ArmSucceeded())
        if profile:
            self.at(0, p.ProfileReady(S1, 1))

    def kinds(self) -> list[str]:
        return [e.kind.value for e in self.events]


def success(unit: int, text: str) -> p.Translation:
    return p.Translation(UnitID(unit), p.Success(p.TranslationCandidate(UnitID(unit), text)))
