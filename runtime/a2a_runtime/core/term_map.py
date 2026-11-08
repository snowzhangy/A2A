"""Literal term replacement for glossaries and pronunciations. Port of
apple/Sources/A2ACore/TermMap.swift.

Glossaries replace source spellings with the agreed target term before translation;
pronunciations replace a target word with a respelling the voice says correctly, before
synthesis only (captions keep the real spelling).

Longest terms win and replaced text is never re-scanned. A term edge made of ASCII letters
or digits only matches at a word boundary, so "Li" never matches inside "Lily"; CJK terms
match anywhere, because Chinese has no spaces between words.
"""

from __future__ import annotations

from .text import graphemes, trim_whitespace


def _is_word_character(c: str) -> bool:
    """Swift `c.isASCII && (c.isLetter || c.isNumber)`."""
    return len(c) == 1 and c.isascii() and c.isalnum()


class TermMap:
    def __init__(self, terms: dict[str, str], ignores_case: bool = False) -> None:
        self.ignores_case = ignores_case
        entries = []
        for term, replacement in terms.items():
            key = trim_whitespace(term)
            if key:
                entries.append((tuple(graphemes(key)), trim_whitespace(replacement)))
        # Longest first; equal lengths in term order (Swift compares Strings by scalars).
        entries.sort(key=lambda e: (-len(e[0]), "".join(e[0])))
        self._entries = entries

    @property
    def is_empty(self) -> bool:
        return not self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def apply(self, text: str) -> str:
        if not self._entries:
            return text
        chars = graphemes(text)
        out: list[str] = []
        i = 0
        while i < len(chars):
            for term, replacement in self._entries:
                if self._matches(term, chars, i):
                    out.append(replacement)
                    i += len(term)
                    break
            else:
                out.append(chars[i])
                i += 1
        return "".join(out)

    def _matches(self, term: tuple[str, ...], chars: list[str], start: int) -> bool:
        end = start + len(term)
        if end > len(chars):
            return False
        for k, c in enumerate(term):
            if not self._same(c, chars[start + k]):
                return False
        if _is_word_character(term[0]) and start > 0 and _is_word_character(chars[start - 1]):
            return False
        if _is_word_character(term[-1]) and end < len(chars) and _is_word_character(chars[end]):
            return False
        return True

    def _same(self, a: str, b: str) -> bool:
        return a == b or (self.ignores_case and a.lower() == b.lower())
