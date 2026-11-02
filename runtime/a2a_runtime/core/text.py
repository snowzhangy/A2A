"""Consistent text normalization for hypothesis comparison. Port of
apple/Sources/A2ACore/TextNormalization.swift.

Comparison keys drop whitespace and fold width/case; the raw text is kept so a committed
unit reproduces exactly what the recognizer emitted. Swift iterates extended grapheme
clusters (`Character`); `graphemes` approximates that for the scripts this project sees
(combining marks, variation selectors, ZWJ sequences, CRLF). CJK and ASCII are identical.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

_ZWJ = "‍"


def _extends(ch: str) -> bool:
    o = ord(ch)
    if 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF or 0x1F3FB <= o <= 0x1F3FF:
        return True
    return ch == _ZWJ or unicodedata.category(ch) in ("Mn", "Me", "Mc")


def graphemes(s: str) -> list[str]:
    out: list[str] = []
    for ch in s:
        if out and (_extends(ch) or out[-1].endswith(_ZWJ) or (out[-1] == "\r" and ch == "\n")):
            out[-1] += ch
        else:
            out.append(ch)
    return out


def char_count(s: str) -> int:
    """Swift `String.count`."""
    return len(graphemes(s))


def is_whitespace(ch: str) -> bool:
    """Swift `Character.isWhitespace` (first scalar has the White_Space property)."""
    return bool(ch) and ch[0].isspace()


def is_number(ch: str) -> bool:
    """Swift `Character.isNumber` (first scalar has a Numeric_Type, including Unihan numerals)."""
    return bool(ch) and ch[0].isnumeric()


def is_letter(ch: str) -> bool:
    return bool(ch) and ch[0].isalpha()


def trim_whitespace(s: str) -> str:
    chars = graphemes(s)
    lo, hi = 0, len(chars)
    while lo < hi and is_whitespace(chars[lo]):
        lo += 1
    while hi > lo and is_whitespace(chars[hi - 1]):
        hi -= 1
    return "".join(chars[lo:hi])


def key_for(ch: str) -> str:
    match ch:
        case "，":
            return ","
        case "。":
            return "."
        case "！":
            return "!"
        case "？":
            return "?"
        case "；":
            return ";"
        case "：":
            return ":"
    # Fold full-width ASCII forms (U+FF01–FF5E) and case.
    if len(ch) == 1 and 0xFF01 <= ord(ch) <= 0xFF5E:
        return chr(ord(ch) - 0xFEE0).lower()
    return ch.lower()


@dataclass(frozen=True, slots=True)
class NormalizedText:
    raw: tuple[str, ...]
    # One comparison key per non-whitespace raw character.
    keys: tuple[str, ...]
    # Index into `raw` for each key.
    raw_index: tuple[int, ...]

    @staticmethod
    def of(text: str) -> NormalizedText:
        raw = graphemes(text)
        keys: list[str] = []
        raw_index: list[int] = []
        for i, ch in enumerate(raw):
            if not is_whitespace(ch):
                keys.append(key_for(ch))
                raw_index.append(i)
        return NormalizedText(tuple(raw), tuple(keys), tuple(raw_index))

    def raw_text(self, lo: int, hi: int) -> str:
        """Raw text covering keys `[lo, hi)`, including interior whitespace."""
        if hi <= lo:
            return ""
        start = 0 if lo == 0 else self.raw_index[lo]
        end = self.raw_index[hi - 1] + 1
        return trim_whitespace("".join(self.raw[start:end]))


class CommitLexicon:
    sentence_end = frozenset([".", "!", "?", ";"])
    clause_comma = frozenset([","])
    punctuation = frozenset([".", "!", "?", ";", ",", ":", "、", "…", '"', "“", "”", "'"])
    # Negation markers that must not end a committed clause (rule 1).
    negation = ("不", "没", "没有", "别", "未", "非", "不是", "并不", "不要", "not", "no", "don't", "never")
    # Function words that leave a phrase incomplete when they end a clause.
    dangling = ("的", "和", "跟", "与", "或", "或者", "还是", "但", "但是", "而", "而且", "因为",
                "所以", "如果", "虽然", "在", "把", "被", "对", "给", "从", "向", "比", "让",
                "是", "就是", "第", "the", "a", "an", "and", "or", "to", "of")
    # Conjunctions that start a new clause; splitting before them is boundary evidence only.
    clause_conjunctions = ("但是", "所以", "然后", "而且", "因为", "如果", "可是", "不过", "并且")
    # A clause ending in one of these may still be inside a number or date.
    # Magnitude suffixes (百千万亿) and "%" close a number, so they are not listed.
    numeral_characters = frozenset("0123456789零一二三四五六七八九十两点.")


class UnsafeEnding:
    NEGATION = "negation"
    NUMBER = "number"
    DANGLING = "dangling"


def unsafe_ending(text: str) -> str | None:
    """Why a candidate clause must wait for more context, or None."""
    body = graphemes(trim_whitespace(text))
    while body and key_for(body[-1]) in CommitLexicon.punctuation:
        body.pop()
    s = trim_whitespace("".join(body))
    chars = graphemes(s)
    if not chars:
        return None
    last = chars[-1]
    lowered = s.lower()
    words = _split_whitespace(lowered)

    def ends_with(marker: str) -> bool:
        if marker.isascii():
            return bool(words) and words[-1] == marker
        return lowered.endswith(marker)

    if any(ends_with(m) for m in CommitLexicon.negation):
        return UnsafeEnding.NEGATION
    if last in CommitLexicon.numeral_characters:
        return UnsafeEnding.NUMBER
    if any(ends_with(m) for m in CommitLexicon.dangling):
        return UnsafeEnding.DANGLING
    return None


def _split_whitespace(s: str) -> list[str]:
    words, current = [], []
    for ch in graphemes(s):
        if is_whitespace(ch):
            if current:
                words.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        words.append("".join(current))
    return words
