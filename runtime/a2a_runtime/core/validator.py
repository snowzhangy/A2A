"""Translation output checks before scheduling: completion, language, duplication, and length
cap, plus the two verdicts that are not failures: nothing to speak, and a target shortened to
the time its source audio took. Port of apple/Sources/A2ACore/TranslationValidator.swift."""

from __future__ import annotations

from dataclasses import dataclass

from .direction import TranslationLanguage
from .target_budget import FillerLexicon, TargetShortener
from .text import char_count, trim_whitespace
from .types import TranslationCandidate


class TranslationRejection:
    EMPTY = "empty"
    INCOMPLETE = "incomplete"
    WRONG_LANGUAGE = "wrong_language"
    DUPLICATE = "duplicate"
    TOO_LONG = "too_long"


class TranslationSilence:
    """A translated unit that says nothing: the model answered music, laughter or a pause with
    an empty string or a filler. Speaking it would add nothing; failing it would report an
    error and trigger a fallback for audio that had no speech in it."""

    EMPTY = "empty"
    FILLER = "filler"


@dataclass(frozen=True, slots=True)
class Accepted:
    """Speak this text (the candidate's own text, or its shortened prefix)."""

    text: str


@dataclass(frozen=True, slots=True)
class Silent:
    """Say nothing. Not a failure: no fallback, no error, no context entry."""

    silence: str


@dataclass(frozen=True, slots=True)
class Rejected:
    rejection: str


TranslationVerdict = Accepted | Silent | Rejected


def is_cjk(cp: int) -> bool:
    return (0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF
            or 0x20000 <= cp <= 0x2FA1F or 0x3040 <= cp <= 0x30FF or 0xAC00 <= cp <= 0xD7AF)


def is_han(cp: int) -> bool:
    """Swift `TranslationValidator.isHanScalar`."""
    return 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x2FA1F


@dataclass
class TranslationValidator:
    target_language: TranslationLanguage = TranslationLanguage.ENGLISH
    # Fraction of CJK letters above which an English target is rejected.
    max_cjk_fraction: float = 0.3
    # A Mandarin target may include names and technical terms in Latin script.
    min_han_fraction: float = 0.3
    # Target length cap relative to source characters.
    max_target_chars_per_source_char: float = 6.0
    min_target_cap: int = 40
    # Characters per second the target is spoken at, for a source-span budget. Derived from
    # the pipeline's own `estimated_seconds_per_target_char`, so debt and budget agree.
    spoken_characters_per_second: float = 15.0
    # Audio time a target may run past its source span before it is shortened.
    spoken_budget_grace_seconds: float = 1.0
    # Cut an over-budget target at its last safe boundary instead of dropping the line.
    shortens_over_budget_targets: bool = True
    # Treat an empty or filler-only target as silence rather than a failed translation.
    silences_fillers: bool = True

    def max_target_characters(self, source: str) -> int:
        return max(self.min_target_cap, int(char_count(source) * self.max_target_chars_per_source_char))

    def spoken_budget_characters(self, source_duration_seconds: float,
                                 grace_seconds: float | None = None) -> int:
        """Characters of target text that fit the source span and this line's following silence."""
        grace = max(0.0, self.spoken_budget_grace_seconds if grace_seconds is None else grace_seconds)
        return max(2, int((max(0.0, source_duration_seconds) + grace) * self.spoken_characters_per_second))

    def evaluate(self, candidate: TranslationCandidate, source: str, previous_target: str | None,
                 source_duration_seconds: float | None = None, word_segmenter=None,
                 grace_seconds: float | None = None) -> TranslationVerdict:
        text = trim_whitespace(candidate.text)
        if not text:
            return Silent(TranslationSilence.EMPTY) if self.silences_fillers else Rejected(TranslationRejection.EMPTY)
        if not candidate.complete:
            return Rejected(TranslationRejection.INCOMPLETE)
        letters = [ord(c) for c in text if c.isalpha()]
        if letters:
            if self.target_language is TranslationLanguage.ENGLISH:
                cjk = sum(1 for cp in letters if is_cjk(cp))
                if cjk / len(letters) > self.max_cjk_fraction:
                    return Rejected(TranslationRejection.WRONG_LANGUAGE)
            else:
                han = sum(1 for cp in letters if is_han(cp))
                if han / len(letters) < self.min_han_fraction:
                    return Rejected(TranslationRejection.WRONG_LANGUAGE)
        if self.silences_fillers and FillerLexicon.is_filler_only(text, self.target_language):
            return Silent(TranslationSilence.FILLER)
        if previous_target is not None and trim_whitespace(previous_target).lower() == text.lower():
            return Rejected(TranslationRejection.DUPLICATE)
        if source_duration_seconds is not None and self.shortens_over_budget_targets:
            budget = self.spoken_budget_characters(source_duration_seconds, grace_seconds=grace_seconds)
            if char_count(text) > budget:
                shortened = TargetShortener.shorten(text, budget, word_segmenter=word_segmenter)
                if shortened is not None:
                    return Accepted(shortened)
        if source_duration_seconds is not None:
            cap = max(self.min_target_cap, int(source_duration_seconds * 35))
        else:
            cap = self.max_target_characters(source)
        if char_count(text) > cap:
            return Rejected(TranslationRejection.TOO_LONG)
        return Accepted(text)
