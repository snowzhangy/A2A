"""Spoken length of a translation. Port of apple/Sources/A2ACore/TargetBudget.swift.

What a target may cost in playback time, what is only a filler, and the deletion that fits
an over-long target into the slot its source audio took. A segment of source speech is ~3 s;
when the translation needs 6 s to speak, the translated bus falls further behind every line
and fluent playout catches up by skipping lines. Simultaneous interpreters solve this with
deletion: say the part that fits rather than nothing. This is the deterministic half of that:
no model, no text outside the process, every rule testable here.
"""

from __future__ import annotations

from typing import Protocol

from .text import CommitLexicon, char_count, graphemes, is_whitespace, key_for, trim_whitespace


class WordSegmenter(Protocol):
    """Decides whether a cut falls inside a word (CommitPolicy.swift `WordSegmenter`)."""

    def splits_word(self, text: str, offset: int) -> bool:
        """True when the character offset `offset` in `text` lies strictly inside a word."""


def _letterish(ch: str) -> bool:
    return any(c.isalpha() or c.isnumeric() for c in ch)


class FillerLexicon:
    """Words and interjections that carry no content when they are the whole translation.
    Music, laughter and pauses come back from a translator as one of these."""

    # Matched case-insensitively on whole words.
    english = frozenset([
        "yeah", "yeh", "yeahh", "yuh", "uh", "uhh", "um", "umm", "hmm", "hm", "hum", "mm", "mmm",
        "mhm", "ah", "ahh", "oh", "ohh", "ooh", "oooh", "eh", "ehh", "huh", "hah", "haha",
        "hahaha", "hehe", "hoho", "hihi", "tsk", "pfft", "hmph", "whoa", "wow",
    ])
    # Single characters; a whole-target Han filler is a run of these.
    mandarin_characters = frozenset("嗯哦噢啊喔呃唔呀哎哟哼哈嘿嘻哇呐")
    # A filler translation is at most this many English words / Han characters.
    max_words = 3
    max_han_characters = 6

    @classmethod
    def is_filler_only(cls, text: str, language) -> bool:
        """True when `text` says nothing but fillers in `language`. Content words anywhere in it
        ("oh, that is wrong") make it a real translation."""
        from .direction import TranslationLanguage

        body = trim_whitespace(text)
        if not body:
            return False
        if language is TranslationLanguage.MANDARIN:
            characters = [c for c in graphemes(body)
                          if not is_whitespace(c) and key_for(c) not in CommitLexicon.punctuation]
            return (bool(characters) and len(characters) <= cls.max_han_characters
                    and all(c in cls.mandarin_characters for c in characters))
        words = [cls._english_word(w) for w in _split_whitespace(body)]
        return bool(words) and len(words) <= cls.max_words and all(w in cls.english for w in words)

    @staticmethod
    def _english_word(word: str) -> str:
        """Lowercased word with surrounding punctuation stripped."""
        chars = graphemes(word.lower())
        while chars and not _letterish(chars[0]):
            chars.pop(0)
        while chars and not _letterish(chars[-1]):
            chars.pop()
        return "".join(chars)


def _split_whitespace(s: str) -> list[str]:
    """Swift `split(whereSeparator: \\.isWhitespace)` (omits empty pieces)."""
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


class TargetShortener:
    """Cuts a translation down to the audio time its source segment took."""

    # Delimiters a shortened target may end at (clause and sentence boundaries).
    delimiters = frozenset([",", ";", ":", ".", "!", "?", "，", "。", "！", "？", "；", "：", "、", "…", "-", "—"])
    # Trailing punctuation is removed after a cut: the synthesizer needs no ellipsis and a
    # dangling comma reads as a mistake.
    trimmable = frozenset([",", ";", ":", ".", "!", "?", "，", "。", "！", "？", "；", "：", "、", "…", "-", "—", ")",
                           "\"", "”", "’", "'"])
    # Words that leave an English sentence unfinished when the cut lands after them.
    dangling_english = frozenset([
        "a", "an", "the", "and", "or", "but", "as", "if", "than", "then", "so", "at", "in", "on",
        "of", "to", "for", "with", "by", "from", "into", "onto", "over", "under", "about",
        "after", "before", "between", "during", "per", "via", "that", "which", "who", "whom",
        "whose", "this", "these", "those", "my", "your", "our", "their", "its", "his", "her",
        "is", "are", "was", "were", "be", "been", "being", "have", "has", "had", "will", "would",
        "can", "could", "should", "may", "might", "must", "not", "very", "more", "most",
    ])

    @classmethod
    def shorten(cls, text: str, max_characters: int, word_segmenter: WordSegmenter | None = None,
                prefer_clause_fraction: float = 0.75, min_keep_fraction: float = 0.4) -> str | None:
        """Longest prefix of `text` that fits `max_characters`, cut at a clause boundary when
        that keeps at least `prefer_clause_fraction` of the budget and at the deepest word
        boundary otherwise. None when the longest safe prefix is a fragment (less than
        `min_keep_fraction` of the budget), so the caller rejects the unit instead of speaking
        a misleading half-sentence."""
        trimmed = trim_whitespace(text)
        if max_characters <= 0:
            return None
        characters = graphemes(trimmed)
        if len(characters) <= max_characters:
            return trimmed or None
        clause_cut = word_cut = 0
        for i, ch in enumerate(characters):
            if i + 1 > max_characters:
                continue
            if ch in cls.delimiters:
                clause_cut = max(clause_cut, i + 1)
            if is_whitespace(ch):
                word_cut = max(word_cut, i + 1)
        # Prefer a real clause ending only when it fills most of the slot; an early clause
        # ending left 1.4 s of speech in a 3 s slot, so a deeper word boundary wins below that.
        cut = clause_cut if clause_cut >= int(prefer_clause_fraction * max_characters) else max(clause_cut, word_cut)
        if cut == 0:
            # Han targets have no spaces: fall back to a character boundary the segmenter
            # (when injected) confirms is not inside a word.
            cut = cls._safe_han_cut(max_characters, trimmed, word_segmenter)
        if cut <= 0:
            return None
        kept = characters[:cut]
        cls._trim_tail(kept)
        # Never end on a dangling function word: "set the price of" is not a sentence. Han
        # text has no spaces, so there the back-off drops one character at a time.
        for _ in range(6):
            if not cls._ends_dangling(kept):
                break
            spaces = [i for i, c in enumerate(kept) if is_whitespace(c)]
            if spaces:
                del kept[spaces[-1]:]
            else:
                kept.pop()
            cls._trim_tail(kept)
            if not kept:
                return None
        result = trim_whitespace("".join(kept))
        if char_count(result) < min_keep_fraction * max_characters or char_count(result) < 2:
            return None
        return result

    @classmethod
    def _trim_tail(cls, kept: list[str]) -> None:
        while kept and (kept[-1] in cls.trimmable or is_whitespace(kept[-1])):
            kept.pop()

    @staticmethod
    def _safe_han_cut(limit: int, text: str, word_segmenter: WordSegmenter | None) -> int:
        """Largest offset <= `limit` that is not strictly inside a segmenter word."""
        for offset in range(limit, max(1, limit - 8) - 1, -1):
            if word_segmenter is None or not word_segmenter.splits_word(text, offset):
                return offset
        return 0

    @classmethod
    def _ends_dangling(cls, characters: list[str]) -> bool:
        from .validator import is_han

        words = _split_whitespace("".join(characters))
        if not words:
            return False
        word = graphemes(words[-1].lower())
        while word and word[-1] in cls.trimmable:
            word.pop()
        if not word:
            return False
        joined = "".join(word)
        if joined in cls.dangling_english or joined in CommitLexicon.dangling:
            return True
        # Han: a final single-character function word, e.g. "…的成本" cut to "…的".
        if not all(is_han(ord(c)) for c in joined):
            return False
        return word[-1] in CommitLexicon.dangling
