"""Commit policy v0: closed source units, translated once. Port of
apple/Sources/A2ACore/CommitPolicy.swift.

1. Source stability: two consecutive hypotheses agree (LocalAgreement-2) after
   normalization; only material beyond a monotonic frontier is committed.
2. A unit closes at a clause boundary or at a finalized utterance. A VAD end only
   requests finalization. The reconsideration window never forces an unsafe split.
3. Unfinished numbers, negation, and dangling function words wait for context.
4. An un-finalized unit that already covers `max_unit_source_seconds` of speech closes at its
   last safe boundary rather than holding the whole utterance (M4).
"""

from __future__ import annotations

from dataclasses import dataclass

from .target_budget import WordSegmenter
from .text import CommitLexicon, NormalizedText, is_letter, is_number, unsafe_ending
from .types import CANONICAL_SAMPLE_RATE, ASRHypothesis, SourceSpan, UnitID, wrapping_sub_u64


@dataclass
class CommitPolicyConfig:
    # Stable-but-uncommitted material older than this asks for weaker boundary evidence.
    reconsider_window_ns: int = 900_000_000
    # A comma closes a unit only once it carries at least this many keys.
    min_comma_unit_keys: int = 4
    # Safety cap: an unbounded clause is split at the best available position.
    max_unit_keys: int = 60
    # Space-delimited source languages should split at a word before the length cap.
    prefer_word_boundaries: bool = False
    # Live mode may use stable conjunctions and timed pauses before sentence finality.
    streaming_clauses: bool = False
    # A unit that covers this much source speech closes at its last safe boundary without
    # waiting for finalization. Key counts mean different speech durations across languages,
    # so the budget is measured in source seconds. Zero disables the rule.
    max_unit_source_seconds: float = 4.0
    # Stable keys a length-forced cut leaves behind, so the rest of the utterance stays
    # splittable and a cut never strands one or two characters.
    min_tail_keys: int = 3
    # Keys the clause must already carry before the seconds budget may cut it. A recognizer
    # that keeps VAD open over music advances its audio frontier without new text, and the
    # floor keeps that from cutting a short clause nobody meant to end.
    length_clause_min_keys: int = 12
    # Silence between consecutive timed tokens that closes a clause (180 ms of source).
    min_clause_pause_samples: int = 8_640


class CommitReason:
    SENTENCE_END = "sentence_end"
    CLAUSE_COMMA = "clause_comma"
    FINALIZED = "finalized"
    CONJUNCTION = "conjunction"
    FORCED_LENGTH = "forced_length"
    LENGTH_CLAUSE = "length_clause"
    PROSODIC_PAUSE = "prosodic_pause"
    AUDIO_SEGMENT = "audio_segment"


@dataclass(frozen=True, slots=True)
class SourceUnit:
    id: UnitID
    utterance: int
    span: SourceSpan
    text: str
    reason: str
    # True when the recognizer gave no token timing and the span is the conservative bound.
    timing_estimated: bool
    # Time between the unit's first stable key and its commit.
    semantic_wait_ns: int


class HoldReason:
    NEGATION = "negation"
    NUMBER = "number"
    DANGLING = "dangling"
    # Stable text without any acceptable clause boundary yet.
    NO_BOUNDARY = "no_boundary"

    @staticmethod
    def of(ending: str | None) -> str:
        return ending if ending is not None else HoldReason.NO_BOUNDARY


# MARK: Outputs


@dataclass(frozen=True, slots=True)
class Commit:
    unit: SourceUnit


@dataclass(frozen=True, slots=True)
class RequestFinalizeOut:
    """VAD ended: the recognizer should finalize; its last partial is not final."""

    utterance: int


@dataclass(frozen=True, slots=True)
class Hold:
    """Stable material is waiting for context beyond the reconsideration window."""

    utterance: int
    reason: str
    pending_keys: int
    waited_ns: int


@dataclass(frozen=True, slots=True)
class RevisionBehindFrontier:
    """The recognizer revised text that was already committed (irreversible)."""

    utterance: int
    committed_keys: int


@dataclass(frozen=True, slots=True)
class Abandoned:
    """An utterance ended without a final result; this text was never committed."""

    utterance: int
    text: str


CommitOutput = Commit | RequestFinalizeOut | Hold | RevisionBehindFrontier | Abandoned


def _word_part(key: str) -> bool:
    if not key:
        return False
    c = key[0]
    return is_letter(c) or is_number(c) or c in "'’-"


class CommitPolicy:
    def __init__(self, config: CommitPolicyConfig | None = None, first_unit: int = 1,
                 word_segmenter: WordSegmenter | None = None) -> None:
        self.config = config or CommitPolicyConfig()
        # None keeps the Latin-only rule. Han hosts inject a segmenter: keys are one per Han
        # character while Mandarin words run one to four, so no key rule can see a word cut.
        self._word_segmenter = word_segmenter
        self._next_unit = first_unit
        self._utterance: int | None = None
        self._previous_keys: tuple[str, ...] | None = None
        self._committed_keys: tuple[str, ...] = ()
        self._key_stable_at: list[int] = []
        self._last_text: NormalizedText | None = None
        self._speech_onset = 0
        # End of the previous unit's span in this utterance (start bound for estimated spans).
        self._last_unit_end: int | None = None
        self._last_hold: str | None = None
        self._revision_reported = False

    @property
    def frontier(self) -> int:
        return len(self._committed_keys)

    @property
    def uncommitted_keys(self) -> int:
        """Keys of the latest hypothesis beyond the frontier (tentative or stable)."""
        total = len(self._last_text.keys) if self._last_text is not None else 0
        return max(0, total - len(self._committed_keys))

    @property
    def uncommitted_start(self) -> int:
        """Earliest source sample not yet covered by a committed unit in this utterance."""
        end = self._last_unit_end if self._last_unit_end is not None else self._speech_onset
        return max(self._speech_onset, end)

    def speech_started(self, sample: int) -> None:
        self._speech_onset = sample

    def speech_ended(self, sample: int) -> list[CommitOutput]:
        if self._utterance is None:
            return []
        return [RequestFinalizeOut(self._utterance)]

    def reset(self) -> list[CommitOutput]:
        """Epoch boundary: drop comparison state; uncommitted text is reported as abandoned."""
        out: list[CommitOutput] = []
        text = self._last_text
        if self._utterance is not None and text is not None and len(self._committed_keys) < len(text.keys):
            out.append(Abandoned(self._utterance, text.raw_text(len(self._committed_keys), len(text.keys))))
        self._utterance = None
        self._previous_keys = None
        self._committed_keys = ()
        self._key_stable_at = []
        self._last_text = None
        self._last_hold = None
        self._last_unit_end = None
        self._revision_reported = False
        return out

    def process(self, h: ASRHypothesis, t_ns: int) -> list[CommitOutput]:
        out: list[CommitOutput] = []
        if self._utterance != h.utterance:
            if self._utterance is not None:
                out += self.reset()
            self._utterance = h.utterance
            # Music or continuous background audio may keep VAD open across many recognizer
            # utterances. Timed tokens establish the new utterance's real onset.
            starts = [t.span.start for t in h.tokens if t.span is not None]
            if starts:
                self._speech_onset = min(starts)
        text = NormalizedText.of(h.text)
        self._last_text = text
        keys = text.keys

        # The committed prefix must survive; otherwise report once and try to re-align.
        committed = len(self._committed_keys)
        if len(keys) < committed or keys[:committed] != self._committed_keys:
            if not self._revision_reported:
                out.append(RevisionBehindFrontier(h.utterance, committed))
                self._revision_reported = True
            # The usual revision is punctuation the recognizer adds late, which inserts keys
            # into text that was already spoken. Without re-alignment the frontier index points
            # into the inserted keys and the next unit speaks the words after it a second time.
            aligned = self._re_sync(self._committed_keys, keys)
            if aligned is not None:
                self._committed_keys = keys[:aligned]
                if len(self._key_stable_at) > aligned:
                    del self._key_stable_at[aligned:]
                while len(self._key_stable_at) < aligned:
                    self._key_stable_at.append(t_ns)
        f = len(self._committed_keys)

        if h.is_final:
            stable = len(keys)
        elif self._previous_keys is not None:
            prev = self._previous_keys
            n = f
            while n < len(keys) and n < len(prev) and keys[n] == prev[n]:
                n += 1
            stable = n
        else:
            stable = f
        self._previous_keys = keys

        if len(self._key_stable_at) > stable:
            del self._key_stable_at[stable:]
        while len(self._key_stable_at) < stable:
            self._key_stable_at.append(t_ns)

        out += self._close_units(text, h, stable, t_ns)
        return out

    def _close_units(self, text: NormalizedText, h: ASRHypothesis, stable: int, t_ns: int) -> list[CommitOutput]:
        out: list[CommitOutput] = []
        keys = text.keys
        cfg = self.config
        while len(self._committed_keys) < stable:
            f = len(self._committed_keys)
            pending_keys = stable - f
            end = self._boundary(text, f, stable)
            if end is not None:
                out.append(Commit(self._make_unit(text, h, f, end[0], end[1], t_ns)))
                continue
            if h.is_final:
                if all(k in CommitLexicon.punctuation for k in keys[f:stable]):
                    self._committed_keys = keys[:stable]
                    break
                out.append(Commit(self._make_unit(text, h, f, stable, CommitReason.FINALIZED, t_ns)))
                continue
            if cfg.streaming_clauses:
                split = self._timed_pause_split(text, h, f, stable)
                if split is not None:
                    out.append(Commit(self._make_unit(text, h, f, split, CommitReason.PROSODIC_PAUSE, t_ns)))
                    continue
                # A confirmed connective with a stable suffix is stronger evidence than a timer.
                if stable - f >= 8:
                    split = self._conjunction_split(text, f, stable)
                    if split is not None and stable - split >= cfg.min_tail_keys:
                        out.append(Commit(self._make_unit(text, h, f, split, CommitReason.CONJUNCTION, t_ns)))
                        continue
            # Meetings produce recognizer utterances of 10-14 s. Once a stable unit exceeds its
            # budget, holding it costs a whole utterance of lag and projects English 1.7x its own
            # length into the debt, so close it at the latest safe position instead.
            if pending_keys >= cfg.length_clause_min_keys:
                seconds = self._pending_source_seconds(text, h, stable)
                if seconds is not None and seconds >= cfg.max_unit_source_seconds:
                    split = self._late_boundary(text, f, stable)
                    if split is not None:
                        out.append(Commit(self._make_unit(text, h, f, split, CommitReason.LENGTH_CLAUSE, t_ns)))
                        continue
            if pending_keys >= cfg.max_unit_keys:
                cut = self._forced_split(text, f, stable)
                out.append(Commit(self._make_unit(text, h, f, cut, CommitReason.FORCED_LENGTH, t_ns)))
                continue
            waited = wrapping_sub_u64(t_ns, self._key_stable_at[f])
            if waited >= cfg.reconsider_window_ns:
                split = self._conjunction_split(text, f, stable)
                if split is not None:
                    out.append(Commit(self._make_unit(text, h, f, split, CommitReason.CONJUNCTION, t_ns)))
                    continue
                reason = HoldReason.of(unsafe_ending(text.raw_text(f, stable)))
                if reason != self._last_hold:
                    out.append(Hold(h.utterance, reason, pending_keys, waited))
                    self._last_hold = reason
            break
        return out

    def _boundary(self, text: NormalizedText, lo: int, hi: int) -> tuple[int, str] | None:
        """Earliest acceptable clause boundary in `(lo, hi]`."""
        i = lo
        while i < hi:
            k = text.keys[i]
            end = i + 1
            if k in CommitLexicon.sentence_end:
                # Merge punctuation-only fragments into the next clause, and never split a
                # decimal number ("3.5"), including one whose fraction is not stable yet.
                if self._content_keys(text, lo, end) > 0 and not self._is_decimal_point(text, i, hi):
                    return end, CommitReason.SENTENCE_END
            elif k in CommitLexicon.clause_comma:
                candidate = text.raw_text(lo, end)
                if self._content_keys(text, lo, end) >= self.config.min_comma_unit_keys and unsafe_ending(candidate) is None:
                    return end, CommitReason.CLAUSE_COMMA
            i += 1
        return None

    @staticmethod
    def _re_sync(committed: tuple[str, ...], keys: tuple[str, ...]) -> int | None:
        """Index just past the committed keys in a revised hypothesis, skipping keys the
        recognizer inserted (late punctuation). None when the change is in spoken content,
        where no alignment is safe and the frontier must stay where it is."""
        i = 0
        for k in committed:
            while i < len(keys) and keys[i] != k:
                if keys[i] not in CommitLexicon.punctuation:
                    return None
                i += 1
            if i >= len(keys):
                return None
            i += 1
        return i

    def _pending_source_seconds(self, text: NormalizedText, h: ASRHypothesis, stable: int) -> float | None:
        """Source speech the pending unit covers, from where uncovered source begins to the
        newest stable key. That key's own token timing is preferred: an utterance held open
        over music advances the audio frontier without any speech."""
        if self.config.max_unit_source_seconds <= 0 or stable <= 0:
            return None
        start = self.uncommitted_start
        spans = self._char_spans(h)
        raw = text.raw_index[stable - 1]
        timed = spans[raw] if raw < len(spans) else None
        end = timed.end if timed is not None else h.audio_frontier
        if end <= start:
            return None
        return (end - start) / CANONICAL_SAMPLE_RATE

    @staticmethod
    def _char_spans(h: ASRHypothesis) -> list[SourceSpan | None]:
        """Recognizer timing for every raw character of the hypothesis (None where a token has none)."""
        spans: list[SourceSpan | None] = []
        for token in h.tokens:
            spans.extend([token.span] * len(NormalizedText.of(token.text).raw))
        return spans

    def _late_boundary(self, text: NormalizedText, lo: int, hi: int) -> int | None:
        """Latest boundary in `(lo, hi)` that leaves a splittable tail and a safe ending.
        Cutting as late as possible keeps the clause as natural as a forced split can be."""
        p = hi - self.config.min_tail_keys
        while p - lo >= self.config.min_comma_unit_keys:
            if unsafe_ending(text.raw_text(lo, p)) is None and not self._splits_any_word(text, p):
                return p
            p -= 1
        return None

    @staticmethod
    def _splits_word(text: NormalizedText, p: int) -> bool:
        """True when cutting before key `p` would split a Latin word in space-delimited text."""
        if not (0 < p < len(text.keys)) or text.raw_index[p] != text.raw_index[p - 1] + 1:
            return False
        return _word_part(text.keys[p - 1]) and _word_part(text.keys[p])

    def _splits_any_word(self, text: NormalizedText, p: int) -> bool:
        """True when cutting before key `p` would split a word. The Latin rule stays behind
        `prefer_word_boundaries` because there spaces are the only cue; an injected segmenter
        always applies, because per-character keys reveal nothing about Han words."""
        if self.config.prefer_word_boundaries and self._splits_word(text, p):
            return True
        if self._word_segmenter is None or not (0 < p < len(text.keys)):
            return False
        return self._word_segmenter.splits_word("".join(text.raw), text.raw_index[p])

    def _conjunction_split(self, text: NormalizedText, lo: int, hi: int) -> int | None:
        keys = text.keys
        p = hi - 1
        while p > lo:
            rest = "".join(keys[p:hi])
            if (any(rest.startswith(c) for c in CommitLexicon.clause_conjunctions)
                    and self._content_keys(text, lo, p) >= self.config.min_comma_unit_keys
                    and unsafe_ending(text.raw_text(lo, p)) is None):
                return p
            p -= 1
        return None

    def _timed_pause_split(self, text: NormalizedText, h: ASRHypothesis, lo: int, hi: int) -> int | None:
        """First silence between timed tokens that closes a safe clause in `(lo, hi)`, leaving
        at least two stable keys after it. Recognizers must report gaps between tokens."""
        key = 0
        for token, nxt in zip(h.tokens, h.tokens[1:]):
            key += len(NormalizedText.of(token.text).keys)
            if key + 2 > hi:
                break
            previous, following = token.span, nxt.span
            if (key <= lo or previous is None or following is None
                    or following.start - previous.end < self.config.min_clause_pause_samples
                    or self._content_keys(text, lo, key) < self.config.min_comma_unit_keys
                    or unsafe_ending(text.raw_text(lo, key)) is not None):
                continue
            return key
        return None

    def _forced_split(self, text: NormalizedText, lo: int, hi: int) -> int:
        cfg = self.config
        limit = min(hi, lo + cfg.max_unit_keys)
        p = limit
        while p > lo + cfg.min_comma_unit_keys:
            if self._splits_any_word(text, p):
                p -= 1
                continue
            if unsafe_ending(text.raw_text(lo, p)) is None:
                return p
            p -= 1
        return limit  # nowhere safe at all: split rather than hold a unit forever

    @staticmethod
    def _is_decimal_point(text: NormalizedText, i: int, stable_end: int) -> bool:
        raw = text.raw[text.raw_index[i]]
        if raw not in (".", "．") or i <= 0 or not is_number(text.keys[i - 1][:1]):
            return False
        if i + 1 >= stable_end:
            return True
        return is_number(text.keys[i + 1][:1])

    @staticmethod
    def _content_keys(text: NormalizedText, lo: int, hi: int) -> int:
        return sum(1 for k in text.keys[lo:hi] if k not in CommitLexicon.punctuation)

    def _make_unit(self, text: NormalizedText, h: ASRHypothesis, lo: int, hi: int, reason: str,
                   t_ns: int) -> SourceUnit:
        span, estimated = self._span_for(text, h, lo, hi)
        unit = SourceUnit(
            id=UnitID(self._next_unit), utterance=h.utterance, span=span, text=text.raw_text(lo, hi),
            reason=reason, timing_estimated=estimated,
            semantic_wait_ns=wrapping_sub_u64(t_ns, self._key_stable_at[lo]))
        self._next_unit += 1
        self._committed_keys = text.keys[:hi]
        self._last_hold = None
        self._last_unit_end = span.end
        return unit

    def _span_for(self, text: NormalizedText, h: ASRHypothesis, lo: int, hi: int) -> tuple[SourceSpan, bool]:
        """Token timing when every covered token has it. Otherwise the estimate runs from the
        later of speech onset and the previous unit's end to the latest accepted frame."""
        start = max(self._speech_onset, self._last_unit_end if self._last_unit_end is not None else self._speech_onset)
        estimate = SourceSpan(start, max(start, h.audio_frontier))
        char_spans = self._char_spans(h)
        merged: SourceSpan | None = None
        for k in range(lo, hi):
            ri = text.raw_index[k]
            s = char_spans[ri] if ri < len(char_spans) else None
            if s is None:
                return estimate, True
            merged = s if merged is None else merged.union(s)
        return (merged if merged is not None else estimate), merged is None
