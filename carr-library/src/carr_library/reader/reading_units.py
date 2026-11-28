"""Choose one source-exact reading unit around an explicit text cursor.

These are deterministic text boundaries, not language-aware tokenization or
word timing. The existing live reader still chunks, renders and owns playback.
"""
from __future__ import annotations

import re

from ..search.literal import _word
from ..search.presentation import _BREAK, _cluster_end, _cluster_start
from ..speech._io import text_value


# A CRLF is one newline, never two empty-line separators after backtracking.
_NEWLINE = r'(?:\r\n|\r(?!\n)|(?<!\r)\n)'
_PARAGRAPH = re.compile(_NEWLINE + r'(?:[^\S\r\n]*' + _NEWLINE + r')+|\u2029')


def _check(cancelled):
    if cancelled():
        raise InterruptedError('Reading-unit selection was cancelled.')


def _trim(raw, start, end, cancelled):
    while start < end and raw[start].isspace():
        if start % 1024 == 0:
            _check(cancelled)
        start += 1
    while end > start and raw[end - 1].isspace():
        if end % 1024 == 0:
            _check(cancelled)
        end -= 1
    if start == end:
        return start, end
    return _cluster_start(raw, start), _cluster_end(raw, end)


def _choose(raw, spans, offset, cancelled):
    previous = None
    for start, end in spans:
        _check(cancelled)
        start, end = _trim(raw, start, end, cancelled)
        if start < end:
            previous = start, end
            if offset < end:
                return previous
    _check(cancelled)
    if previous is None:
        raise ValueError('The current reading location has no spoken text.')
    return previous


def _paragraphs(raw):
    start = 0
    for match in _PARAGRAPH.finditer(raw):
        yield start, match.start()
        start = match.end()
    yield start, len(raw)


def _sentences(raw, start, end):
    # Reuse the source-exact excerpt engine's punctuation/closing-quote rule.
    # Abbreviations and language-specific sentence rules are not inferred.
    for match in _BREAK.finditer(raw, start, end):
        yield start, match.end(1) if match.group(1) else match.start()
        start = match.end()
    yield start, end


def _word_at(raw, position):
    if _word(raw[position]):
        return True
    return (raw[position] in "'’" and 0 < position < len(raw) - 1
            and _word(raw[position - 1]) and _word(raw[position + 1]))


def reading_unit_range(raw, offset, unit, *, cancelled=lambda: False):
    """Return original offsets; separators prefer the next unit, EOF the last.

    Words use the existing Unicode letter/number/mark/connector rule, retaining
    internal apostrophes. Sentences use the existing punctuation rule within one
    blank-line-delimited paragraph. Native UI positions must be mapped first.
    """
    _check(cancelled)
    text_value(raw, 'Reading text', empty=True)
    if type(offset) is not int or not 0 <= offset <= len(raw):
        raise ValueError('Place the reading cursor within the current text.')
    if unit not in ('word', 'sentence', 'paragraph'):
        raise ValueError('Choose a word, sentence or paragraph reading command.')
    _check(cancelled)
    if unit != 'word':
        start, end = _choose(raw, _paragraphs(raw), offset, cancelled)
        result = ((start, end) if unit == 'paragraph' else
                  _choose(raw, _sentences(raw, start, end), offset, cancelled))
    else:
        position = offset
        while position < len(raw) and not _word_at(raw, position):
            if position % 1024 == 0:
                _check(cancelled)
            position += 1
        if position == len(raw):
            position = min(offset, len(raw)) - 1
            while position >= 0 and not _word_at(raw, position):
                if position % 1024 == 0:
                    _check(cancelled)
                position -= 1
        if position < 0:
            raise ValueError('The current reading location has no word to read.')
        start, end = position, position + 1
        while start > 0 and _word_at(raw, start - 1):
            if start % 1024 == 0:
                _check(cancelled)
            start -= 1
        while end < len(raw) and _word_at(raw, end):
            if end % 1024 == 0:
                _check(cancelled)
            end += 1
        result = _cluster_start(raw, start), _cluster_end(raw, end)
    _check(cancelled)
    return result
