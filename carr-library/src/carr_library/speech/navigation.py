"""Bounded reading-copy navigation with observed native Unicode offsets.

The native prefix/cursor mapping is reused from canonical-compatible PR149
906aa1a95a8f40ef119682d2d5c805c017c327f4. No live-reader or speech adapter is
copied. Text boundaries remain deterministic navigation aids, not word timing.
"""
import re
import unicodedata

from ._io import text_value


def _normalized(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def native_text_range(raw, displayed, prefix_start, prefix_end):
    """Map wx.GetRange prefixes to original offsets, including UTF-16/CRLF UI.

    Native insertion positions are never used as Python string indexes. wx does
    the native indexing when obtaining each prefix; only those Unicode strings
    are matched against the exact original text.
    """
    values = (raw, displayed, prefix_start, prefix_end)
    if any(type(value) is not str or "\x00" in value for value in values):
        raise ValueError("Reading selection must contain valid source text.")
    try:
        for value in values:
            value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError("Place the reading cursor between complete Unicode characters.") from exc
    if _normalized(raw) != _normalized(displayed):
        raise ValueError("The displayed reading text changed. Open the source location again.")

    def offset(prefix):
        index = 0
        for character in _normalized(prefix):
            if index >= len(raw):
                raise ValueError("The native selection no longer matches the source text.")
            actual = "\n" if raw[index] == "\r" else raw[index]
            if character != actual:
                raise ValueError("The native selection no longer matches the source text.")
            index += 2 if raw[index:index + 2] == "\r\n" else 1
        return index

    start, end = offset(prefix_start), offset(prefix_end)
    if end < start:
        raise ValueError("The native reading selection has an invalid order.")
    return start, end



def native_cursor_position(control, raw, offset):
    """Restore a Python source offset through observed native prefixes."""
    if type(offset) is not int or not 0 <= offset <= len(raw):
        raise ValueError('The saved cursor is outside the reading text.')
    displayed = control.GetValue()
    last = control.GetLastPosition()

    def mapped(position):
        try:
            prefix = control.GetRange(0, position)
            return native_text_range(raw, displayed, prefix, prefix)[0], position
        except (UnicodeError, ValueError):
            # A native UTF-16 position can split a surrogate pair. Probe its
            # complete boundary for comparison, then verify the final result.
            if position >= last:
                raise
            prefix = control.GetRange(0, position + 1)
            return native_text_range(raw, displayed, prefix, prefix)[0], position + 1

    low, high = 0, last
    while low < high:
        middle = (low + high) // 2
        actual, _position = mapped(middle)
        if actual < offset:
            low = middle + 1
        else:
            high = middle
    actual, position = mapped(low)
    if actual != offset:
        raise ValueError('The saved cursor is not a complete native text boundary.')
    return position


def native_selection(control, text):
    """Return Python character offsets from the native control's own prefixes."""
    start, end = control.GetSelection()
    return native_text_range(text, control.GetValue(),
                             control.GetRange(0, start), control.GetRange(0, end))


def native_insertion_offset(control, text):
    position = control.GetInsertionPoint()
    prefix = control.GetRange(0, position)
    return native_text_range(text, control.GetValue(), prefix, prefix)[0]


def _word_character(value):
    return value.isalnum() or value == '_' or unicodedata.category(value).startswith('M')


def _word_ranges(text):
    cursor = 0
    while cursor < len(text):
        if not _word_character(text[cursor]):
            cursor += 1
            continue
        first = cursor
        cursor += 1
        while cursor < len(text):
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


def _separated_ranges(text, separator):
    first = 0
    for match in separator.finditer(text):
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


def _sentence_ranges(text):
    first = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.start() if match.group()[0] in '\r\n\u2028\u2029' else match.end()
        span = _trimmed(text, first, end)
        if span is not None:
            yield span
        first = match.end()
    span = _trimmed(text, first, len(text))
    if span is not None:
        yield span


def unit_range(text, position, unit, *, direction=0):
    """Locate one current/adjacent unit without building an unbounded index.

    A cursor between units chooses the following one (or the last at EOF).
    Previous/next never wrap. At an inter-unit gap, they select the adjacent
    unit in that direction. Sentence boundaries are punctuation/newline rules;
    abbreviations and languages without word separators need explicit review.
    """
    text_value(text, 'reading text', empty=True)
    if type(position) is not int or not 0 <= position <= len(text):
        raise ValueError('The reading cursor is outside the current text.')
    if type(direction) is not int or direction not in (-1, 0, 1):
        raise ValueError('Choose the current, previous or next text unit.')
    if unit == 'word':
        spans = _word_ranges(text)
    elif unit == 'sentence':
        spans = _sentence_ranges(text)
    elif unit == 'paragraph':
        spans = _separated_ranges(text, _PARAGRAPH_GAP)
    else:
        raise ValueError('Choose word, sentence or paragraph navigation.')
    previous, after_current = None, False
    for span in spans:
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
    return previous if direction <= 0 and not after_current else None


def select_text_unit(control, text, unit, *, direction=0):
    """Select exact native text only; never speak, edit or save on navigation."""
    selection = native_selection(control, text)
    position = selection[0] if selection[0] != selection[1] else native_insertion_offset(control, text)
    span = unit_range(text, position, unit, direction=direction)
    if span is None:
        return None
    start = native_cursor_position(control, text, span[0])
    end = native_cursor_position(control, text, span[1])
    # All offsets were observed against this exact display, including UTF-16.
    if _normalized(control.GetValue()) != _normalized(text):
        raise ValueError('The displayed reading text changed before selection.')
    control.SetSelection(start, end)
    control.ShowPosition(end)
    control.SetFocus()
    return span
