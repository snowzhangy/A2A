"""Translation output checks before scheduling: completion, language, duplication, and
length cap. Port of apple/Sources/A2ACore/TranslationValidator.swift."""

from __future__ import annotations

from dataclasses import dataclass

from .direction import TranslationLanguage
from .text import char_count, trim_whitespace
from .types import TranslationCandidate


class TranslationRejection:
    EMPTY = "empty"
    INCOMPLETE = "incomplete"
    WRONG_LANGUAGE = "wrong_language"
    DUPLICATE = "duplicate"
    TOO_LONG = "too_long"


def is_cjk(cp: int) -> bool:
    return (0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF
            or 0x20000 <= cp <= 0x2FA1F or 0x3040 <= cp <= 0x30FF or 0xAC00 <= cp <= 0xD7AF)


def is_han(cp: int) -> bool:
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

    def max_target_characters(self, source: str) -> int:
        return max(self.min_target_cap, int(char_count(source) * self.max_target_chars_per_source_char))

    def validate(self, candidate: TranslationCandidate, source: str, previous_target: str | None) -> str | None:
        text = trim_whitespace(candidate.text)
        if not text:
            return TranslationRejection.EMPTY
        if not candidate.complete:
            return TranslationRejection.INCOMPLETE
        letters = [ord(c) for c in text if c.isalpha()]
        if letters:
            if self.target_language is TranslationLanguage.ENGLISH:
                cjk = sum(1 for cp in letters if is_cjk(cp))
                if cjk / len(letters) > self.max_cjk_fraction:
                    return TranslationRejection.WRONG_LANGUAGE
            else:
                han = sum(1 for cp in letters if is_han(cp))
                if han / len(letters) < self.min_han_fraction:
                    return TranslationRejection.WRONG_LANGUAGE
        if previous_target is not None and trim_whitespace(previous_target).lower() == text.lower():
            return TranslationRejection.DUPLICATE
        if char_count(text) > self.max_target_characters(source):
            return TranslationRejection.TOO_LONG
        return None
