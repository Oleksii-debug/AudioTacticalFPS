"""Reviewed text-unit rules and existing native offset/recovery contracts."""
import sys

import pytest

from carr_library.speech.navigation import UNITS, unit_range
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui.reading_tools import (native_position, reading_offset,
                                           reading_selection, select_reading_range)


@pytest.mark.parametrize('text, needle, unit', [
    ("😀 Ім’я читача — Олексій.", 'Ім’я', 'word'),
    ('Červený kôň beží.', 'Červený', 'word'),
    ('A cafe\u0301 stays unchanged.', 'cafe\u0301', 'word'),
    ('An author-defined term.', 'author-defined', 'word'),
    ('First. “Second sentence!” Next.', '“Second sentence!”', 'sentence'),
    ('第一句。第二句！最後。', '第二句！', 'sentence'),
    ('Value 1.25 remains. Next.', 'Value 1.25 remains.', 'sentence'),
    ('First line\r\nsame paragraph\r\n\r\nAnother one.', 'First line\r\nsame paragraph', 'paragraph'),
    ('First line\nsame paragraph\n \nAnother one.', 'Another one.', 'paragraph'),
    ('One\u2029Two', 'Two', 'paragraph'),
], ids=['ukrainian-apostrophe', 'slovak', 'combining-mark', 'hyphenated',
        'quoted-sentence', 'cjk', 'decimal', 'crlf-paragraph', 'blank-line', 'paragraph-separator'])
def test_exact_current_units_preserve_unicode_and_original(text, needle, unit):
    # The reviewed donor's text rules are retained; native offsets use the
    # existing reading-tools implementation, not a second mapping algorithm.
    document = ReadingDocument(text)
    before = document.to_dict()
    position = text.index(needle) + min(1, len(needle) - 1)
    span = unit_range(text, position, unit)
    assert span is not None and text[slice(*span)] == needle
    assert document.to_dict() == before and document.source_text == text


def test_single_crlf_is_not_a_blank_paragraph_boundary():
    text = 'First\r\nSecond'
    assert unit_range(text, 8, 'paragraph') == (0, len(text))


def test_previous_next_and_eof_never_wrap_or_select_empty_ranges():
    text = 'One, two three.'
    assert unit_range(text, 0, 'word', direction=-1) is None
    assert unit_range(text, 0, 'word', direction=1) == (5, 8)
    assert unit_range(text, 6, 'word', direction=-1) == (0, 3)
    assert unit_range(text, 4, 'word') == (5, 8)
    assert unit_range(text, 4, 'word', direction=-1) == (0, 3)
    assert unit_range(text, len(text), 'word') == (9, 14)
    assert unit_range(text, len(text), 'word', direction=1) is None
    assert unit_range(text, len(text), 'word', direction=-1) == (9, 14)
    for unit in UNITS:
        assert unit_range(' \r\n\t', 0, unit) is None


@pytest.mark.parametrize('position, unit, direction', [
    (True, 'word', 0), (-1, 'word', 0), (100, 'word', 0),
    (1.2, 'word', 0), (0, 'chapter', 0), (0, 'word', True), (0, 'word', 2),
], ids=['boolean-offset', 'negative', 'past-end', 'fractional',
        'unknown-unit', 'boolean-direction', 'invalid-direction'])
def test_invalid_navigation_is_rejected(position, unit, direction):
    with pytest.raises(ValueError):
        unit_range('Bounded text.', position, unit, direction=direction)


def test_navigation_keeps_explicit_boundaries_and_no_unbounded_index():
    text = ('x ' * 200_000) + 'ending'
    assert unit_range(text, len(text) - 1, 'word') == (len(text) - 6, len(text))
    with pytest.raises(ValueError):
        unit_range('x' * 2_000_001, 0, 'word')
    with pytest.raises(ValueError):
        unit_range('x\x00y', 0, 'word')


@pytest.mark.parametrize('separator', ['\n', '\r\n', '\r', '\u2028', '\u2029'],
                         ids=['lf', 'crlf', 'cr', 'line-separator', 'paragraph-separator'])
def test_logical_line_is_not_a_wrapped_display_row(separator):
    text = '  First 😀 line  ' + separator + '\tSecond logical line.\t' + separator + '  '
    start = text.index('Second')
    span = unit_range(text, start + 1, 'line')
    assert text[slice(*span)] == 'Second logical line.'
    assert unit_range(text, start + 1, 'line', direction=-1) == (2, len('  First 😀 line'))
    assert unit_range(text, start + 1, 'line', direction=1) is None
    assert unit_range(text, len(text), 'line') == span


def test_line_range_excludes_only_boundary_whitespace_and_preserves_internal_spacing():
    text = '\n \t\n\t😀 one  two\tthree   \nnext'
    span = unit_range(text, text.index('two'), 'line')
    assert text[slice(*span)] == '😀 one  two\tthree'
    assert unit_range(text, 0, 'line') == span


def test_cancellation_is_checked_inside_a_long_single_word():
    checks = []
    def cancelled():
        checks.append(True)
        return len(checks) >= 6
    with pytest.raises(InterruptedError):
        unit_range('a' * 500_000, 400_000, 'word', cancelled=cancelled)
    assert len(checks) == 6


@pytest.mark.parametrize('unit', UNITS)
def test_pre_cancelled_work_cannot_produce_a_span(unit):
    with pytest.raises(InterruptedError):
        unit_range('Original unchanged.', 0, unit, cancelled=lambda: True)


class NativeText:
    """The current reading-tools mapping observes real prefix strings."""
    def __init__(self, text):
        self.text, self.selection = text, (0, 0)
        self.shown = None

    def GetValue(self):
        return self.text

    def GetRange(self, start, end):
        if sys.platform == 'win32':
            raw = self.text.replace('\n', '\r\n').encode('utf-16-le')
            return raw[start * 2:end * 2].decode('utf-16-le').replace('\r\n', '\n')
        return self.text[start:end]

    def XYToPosition(self, column, row):
        lines = self.text.split('\n')
        if not 0 <= row < len(lines):
            return -1
        prefix = '\n'.join(lines[:row]) + ('\n' if row else '')
        units = (len(prefix.replace('\n', '\r\n').encode('utf-16-le')) // 2
                 if sys.platform == 'win32' else len(prefix))
        return units + column

    def GetSelection(self):
        return self.selection

    def SetSelection(self, start, end):
        self.selection = start, end

    def ShowPosition(self, position):
        self.shown = position


def test_saved_cursor_after_emoji_uses_existing_mapping_and_exact_draft(tmp_path):
    text = '😀 First.\nДругий абзац.'
    document = ReadingDocument(text)
    field = NativeText(text)
    span = unit_range(text, text.index('Другий'), 'word')
    select_reading_range(field, *span)
    assert field.GetRange(*field.GetSelection()) == 'Другий'
    assert reading_selection(field) == span
    cursor = reading_offset(field, field.GetSelection()[1])
    assert cursor == span[1]
    if sys.platform == 'win32':
        assert cursor != field.GetSelection()[1]
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(document, cursor=cursor))
    before = store.path.read_bytes()
    recovered = ReaderDraftStore(tmp_path).load()
    reopened = NativeText(recovered.document.text)
    position = native_position(reopened, recovered.cursor)
    reopened.SetSelection(position, position)
    assert reading_selection(reopened) == (cursor, cursor)
    assert recovered.document.source_text == text and recovered.document.text == text
    assert store.path.read_bytes() == before
