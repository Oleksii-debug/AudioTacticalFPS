"""Bounded text-unit boundaries reused from the reviewed PR152 navigation core.

Native selection mapping remains in ui.reading_tools. These punctuation/space
rules locate reading-copy spans; they do not infer linguistic or spoken timing.
A line is a nonempty logical text line, independent of visual word wrapping.
"""
import re
import unicodedata

from ._io import check_cancelled, text_value

UNITS = ('word', 'line', 'sentence', 'paragraph')

def _word_character(value):
    return value.isalnum() or value == '_' or unicodedata.category(value).startswith('M')


def _word_ranges(text, cancelled):
    cursor = next_check = 0
    while cursor < len(text):
        if cursor >= next_check:
            check_cancelled(cancelled)
            next_check = cursor + 1024
        if not _word_character(text[cursor]):
            cursor += 1
            continue
        first = cursor
        cursor += 1
        while cursor < len(text):
            if cursor >= next_check:
                check_cancelled(cancelled)
                next_check = cursor + 1024
            if _word_character(text[cursor]):
                cursor += 1
            elif (text[cursor] in "'\u2019\u02bc-\u2010\u2011" and cursor + 1 < len(text)
                  and _word_character(text[cursor + 1])):
                cursor += 2
            else:
                break
        yield first, cursor


def _trimmed(text, start, end):
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _separated_ranges(text, separator, cancelled):
    first = 0
    for match in separator.finditer(text):
        check_cancelled(cancelled)
        span = _trimmed(text, first, match.start())
        if span is not None:
            yield span
        first = match.end()
    span = _trimmed(text, first, len(text))
    if span is not None:
        yield span


# Deterministic text boundaries, not a language or acoustic alignment model.
# Sentence endings retain punctuation/closing quotes; newlines also end a unit.
_SENTENCE_END = re.compile(r'''[.!?\u2026\u3002\uff01\uff1f]+["'\u2019\u201d\u00bb)\]]*(?=\s|$)|[\u3002\uff01\uff1f]+["'\u2019\u201d\u00bb)\]]*|\r\n|\r|\n|\u2028|\u2029''')
_LINE_BREAK = r'(?:\r\n|\r(?!\n)|(?<!\r)\n)'
_PARAGRAPH_GAP = re.compile(_LINE_BREAK + r'[ \t]*' + _LINE_BREAK
                            + r'(?:[ \t]*' + _LINE_BREAK + r')*|\u2029')


_LOGICAL_LINE = re.compile(r'\r\n|\r|\n|\u2028|\u2029')


def _sentence_ranges(text, cancelled):
    first = 0
    for match in _SENTENCE_END.finditer(text):
        check_cancelled(cancelled)
        end = match.start() if match.group()[0] in '\r\n\u2028\u2029' else match.end()
        span = _trimmed(text, first, end)
        if span is not None:
            yield span
        first = match.end()
    span = _trimmed(text, first, len(text))
    if span is not None:
        yield span


def unit_range(text, position, unit, *, direction=0, cancelled=lambda: False):
    """Locate one current/adjacent unit without building an unbounded index.

    A cursor between units chooses the following one (or the last at EOF).
    Previous/next never wrap. At an inter-unit gap, they select the adjacent
    unit in that direction. Sentence boundaries are punctuation/newline rules;
    abbreviations and languages without word separators need explicit review.
    """
    check_cancelled(cancelled)
    text_value(text, 'reading text', empty=True)
    check_cancelled(cancelled)
    if type(position) is not int or not 0 <= position <= len(text):
        raise ValueError('The reading cursor is outside the current text.')
    if type(direction) is not int or direction not in (-1, 0, 1):
        raise ValueError('Choose the current, previous or next text unit.')
    if unit == 'word':
        spans = _word_ranges(text, cancelled)
    elif unit == 'line':
        spans = _separated_ranges(text, _LOGICAL_LINE, cancelled)
    elif unit == 'sentence':
        spans = _sentence_ranges(text, cancelled)
    elif unit == 'paragraph':
        spans = _separated_ranges(text, _PARAGRAPH_GAP, cancelled)
    else:
        raise ValueError('Choose word, logical line, sentence or paragraph navigation.')
    previous, after_current = None, False
    for span in spans:
        check_cancelled(cancelled)
        start, end = span
        if after_current:
            return span
        if start <= position < end:
            if direction < 0:
                return previous
            if direction == 0:
                return span
            after_current = True
        elif start > position:
            return previous if direction < 0 else span
        previous = span
    check_cancelled(cancelled)
    return previous if direction <= 0 and not after_current else None


