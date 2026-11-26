"""Explicit reading derivatives with exact original text and source-offset maps.

This module never updates a document or citation. Substitution rules are literal
and bounded; source text never becomes a regular expression or executable code.
Automatic punctuation/stress prediction is intentionally not claimed.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
import unicodedata

MAX_READING_CHARACTERS = 1_000_000
MAX_READING_EDITS = 10_000
MAX_PRONUNCIATIONS = 256


@dataclass(frozen=True)
class ReadingEdit:
    source_start: int
    source_end: int
    replacement: str
    reason: str = "explicit reading edit"


@dataclass(frozen=True)
class ReadingSpan:
    source_start: int
    source_end: int
    reading_start: int
    reading_end: int
    changed: bool


@dataclass(frozen=True)
class Pronunciation:
    original: str
    spoken: str
    whole_word: bool = True


@dataclass(frozen=True)
class NarrationDerivative:
    original_text: str
    text: str
    spans: tuple[ReadingSpan, ...]
    edits: tuple[ReadingEdit, ...]
    source_sha256: str
    derivative_sha256: str
    version: int = 1

    def source_span(self, start: int, end: int) -> tuple[int, int]:
        """Map a nonempty reading range to the containing original-source span.

        Inside a replacement the whole original edited span is returned, never
        an invented word-level alignment. Exact spans retain exact offsets.
        """
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(self.text):
            raise ValueError("Expected a nonempty range inside the reading derivative.")
        matches = [span for span in self.spans if span.reading_start < end and span.reading_end > start]
        first, last = matches[0], matches[-1]
        left = first.source_start if first.changed else first.source_start + max(0, start - first.reading_start)
        right = last.source_end if last.changed else last.source_start + min(last.reading_end, end) - last.reading_start
        return left, right


def _source(source: str):
    if not isinstance(source, str) or len(source) > MAX_READING_CHARACTERS:
        raise ValueError("Reading preparation requires a bounded Unicode text chunk.")
    try:
        source.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Reading text must contain valid Unicode scalar values.") from exc


def apply_reading_edits(source: str, edits=()) -> NarrationDerivative:
    """Build a versioned derivative; reject overlap and growth before concatenation."""
    _source(source)
    if not isinstance(edits, (tuple, list)) or len(edits) > MAX_READING_EDITS:
        raise ValueError("Too many reading edits or unsupported edit collection.")
    ordered = tuple(sorted(edits, key=lambda e: (e.source_start, e.source_end))
                    ) if all(isinstance(e, ReadingEdit) for e in edits) else None
    if ordered is None:
        raise TypeError("All reading edits must be ReadingEdit values.")
    cursor, predicted, previous = 0, len(source), None
    for edit in ordered:
        if (type(edit.source_start) is not int or type(edit.source_end) is not int
                or not 0 <= edit.source_start <= edit.source_end <= len(source)
                or edit.source_start < cursor or previous == (edit.source_start, edit.source_end)
                or not isinstance(edit.replacement, str) or not isinstance(edit.reason, str)
                or len(edit.reason) > 256):
            raise ValueError("Invalid, duplicate or overlapping reading edit.")
        edit.replacement.encode("utf-8")
        predicted += len(edit.replacement) - (edit.source_end - edit.source_start)
        if predicted > MAX_READING_CHARACTERS:
            raise ValueError("Reading derivative exceeds its character budget.")
        cursor = edit.source_end
        previous = (edit.source_start, edit.source_end)
    chunks, spans, cursor, output_cursor = [], [], 0, 0
    for edit in ordered:
        if edit.source_start > cursor:
            exact = source[cursor:edit.source_start]
            chunks.append(exact)
            spans.append(ReadingSpan(cursor, edit.source_start, output_cursor,
                                     output_cursor + len(exact), False))
            output_cursor += len(exact)
        chunks.append(edit.replacement)
        spans.append(ReadingSpan(edit.source_start, edit.source_end, output_cursor,
                                 output_cursor + len(edit.replacement), True))
        output_cursor += len(edit.replacement)
        cursor = edit.source_end
    if cursor < len(source):
        exact = source[cursor:]
        chunks.append(exact)
        spans.append(ReadingSpan(cursor, len(source), output_cursor,
                                 output_cursor + len(exact), False))
    text = "".join(chunks)
    return NarrationDerivative(source, text, tuple(spans), ordered,
                               sha256(source.encode("utf-8")).hexdigest(),
                               sha256(text.encode("utf-8")).hexdigest())


def _word_character(ch: str) -> bool:
    return ch == "_" or ch.isalnum() or unicodedata.category(ch).startswith("M")


def prepare_reading(source: str, pronunciations=(), *, normalize_line_endings=False) -> NarrationDerivative:
    """Apply explicit literal pronunciation overrides and optional CR/LF conversion.

    Matching is case-sensitive and nonrecursive. At the same position the
    longest eligible rule wins; no predicted stress, transliteration or quotation edit.
    """
    _source(source)
    if type(normalize_line_endings) is not bool:
        raise TypeError("normalize_line_endings must be bool.")
    if not isinstance(pronunciations, (tuple, list)) or len(pronunciations) > MAX_PRONUNCIATIONS:
        raise ValueError("Too many pronunciation rules or unsupported rule collection.")
    rules = {}
    for rule in pronunciations:
        if (not isinstance(rule, Pronunciation) or not isinstance(rule.original, str)
                or not 1 <= len(rule.original) <= 256 or not isinstance(rule.spoken, str)
                or len(rule.spoken) > 4096 or type(rule.whole_word) is not bool
                or rule.original in rules or (normalize_line_endings and "\r" in rule.original)):
            raise ValueError("Invalid or duplicate literal pronunciation rule.")
        rules[rule.original] = rule
    if not rules and not normalize_line_endings:
        return apply_reading_edits(source)
    # Escaped literal alternatives cannot introduce regex operators or backtracking
    # loops. The bounded alternatives have a maximum individual length of 256.
    literals = sorted(rules, key=lambda key: (-len(key), key))
    if normalize_line_endings:
        literals.extend(["\r\n", "\r"])
    pattern = re.compile("|".join(re.escape(value) for value in literals))
    # A longer literal can fail its whole-word boundary while a shorter rule at
    # the same position is eligible. Do not consume that rejected literal and
    # silently suppress the user's valid shorter pronunciation.
    candidates = {}
    for literal in sorted(rules, key=lambda key: (-len(key), key)):
        candidates.setdefault(literal[0], []).append(rules[literal])
    edits, cursor = [], 0
    while match := pattern.search(source, cursor):
        start, end, value = match.start(), match.end(), match.group()
        if normalize_line_endings and value in ("\r", "\r\n"):
            edit = ReadingEdit(start, end, "\n", "line ending normalization")
        else:
            selected = None
            for rule in candidates.get(source[start], ()):
                if not source.startswith(rule.original, start):
                    continue
                candidate_end = start + len(rule.original)
                if rule.whole_word and ((start and _word_character(source[start - 1]))
                        or (candidate_end < len(source) and _word_character(source[candidate_end]))):
                    continue
                selected, end = rule, candidate_end
                break
            if selected is None:
                cursor = start + 1
                continue
            edit = ReadingEdit(start, end, selected.spoken, "explicit pronunciation")
        edits.append(edit)
        cursor = end
        if len(edits) > MAX_READING_EDITS:
            raise ValueError("Reading preparation exceeds its edit budget.")
    return apply_reading_edits(source, edits)
