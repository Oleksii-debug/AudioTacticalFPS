"""Exact text selection boundaries; no speech engine or native-device substitution."""
import pytest

from carr_library.speech.navigation import (native_cursor_position, native_insertion_offset,
    native_selection, native_text_range, select_text_unit, unit_range)
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume


class NativeText:
    """Windows-style UTF-16 offsets over actual strings, including CRLF."""
    def __init__(self, text):
        self.text, self.selection, self.cursor = text, (0, 0), 0
        self.focused, self.shown = False, None

    def GetValue(self):
        return self.text

    def GetRange(self, start, end):
        return self.text.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')

    def GetSelection(self):
        return self.selection

    def GetInsertionPoint(self):
        return self.cursor

    def GetLastPosition(self):
        return len(self.text.encode('utf-16-le')) // 2

    def SetSelection(self, start, end):
        self.selection, self.cursor = (start, end), end

    def ShowPosition(self, value):
        self.shown = value

    def SetFocus(self):
        self.focused = True

    def place(self, position):
        self.cursor = len(self.text[:position].encode('utf-16-le')) // 2
        self.selection = (self.cursor, self.cursor)

    def selected(self):
        return self.GetRange(*self.selection)


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
])
def test_exact_current_units_preserve_unicode_and_original(text, needle, unit):
    document = ReadingDocument(text)
    before = document.to_dict()
    position = text.index(needle) + min(1, len(needle) - 1)
    span = unit_range(text, position, unit)
    assert span is not None and text[slice(*span)] == needle
    editor = NativeText(text); editor.place(position)
    assert select_text_unit(editor, document.text, unit) == span
    assert editor.selected() == needle and editor.focused
    assert native_selection(editor, text) == span
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
    for unit in ('word', 'sentence', 'paragraph'):
        assert unit_range(' \r\n\t', 0, unit) is None


@pytest.mark.parametrize('position, unit, direction', [
    (True, 'word', 0), (-1, 'word', 0), (100, 'word', 0),
    (1.2, 'word', 0), (0, 'chapter', 0), (0, 'word', True), (0, 'word', 2)])
def test_invalid_navigation_is_rejected(position, unit, direction):
    with pytest.raises(ValueError):
        unit_range('Bounded text.', position, unit, direction=direction)


def test_navigation_keeps_explicit_boundaries_and_no_unbounded_index():
    text = ('x ' * 200_000) + 'ending'
    assert unit_range(text, len(text) - 1, 'word') == (len(text) - 6, len(text))
    with pytest.raises(ValueError, match='at most'):
        unit_range('x' * 2_000_001, 0, 'word')
    with pytest.raises(ValueError):
        unit_range('x\x00y', 0, 'word')


@pytest.mark.parametrize('displayed', ['😀 A\r\nБета словом.', '😀 A\nБета словом.'])
def test_native_prefix_mapping_roundtrips_emoji_and_normalized_line_endings(displayed):
    raw = '😀 A\r\nБета словом.'
    editor = NativeText(displayed)
    start, end = raw.index('Бета'), raw.index('Бета') + 4
    first, last = native_cursor_position(editor, raw, start), native_cursor_position(editor, raw, end)
    editor.SetSelection(first, last)
    assert editor.selected() == 'Бета'
    assert native_selection(editor, raw) == (start, end)
    assert native_insertion_offset(editor, raw) == end
    assert first == len(displayed[:displayed.index('Бета')].encode('utf-16-le')) // 2


def test_invalid_native_prefix_or_display_cannot_move_selection():
    editor = NativeText('😀 Second word.')
    editor.place(4)
    before = editor.selection
    with pytest.raises(ValueError, match='changed'):
        select_text_unit(editor, '😀 Changed word.', 'word')
    assert editor.selection == before and not editor.focused
    with pytest.raises(ValueError, match='source text'):
        native_text_range('abc', 'abc', 'ax', 'ax')
    with pytest.raises(ValueError):
        native_text_range('abc', 'abc', 'ab', 'a')


def test_saved_cursor_is_python_offset_after_emoji_and_restores_exact_word(tmp_path):
    text = '😀 First.\r\nДругий абзац.'
    editor = NativeText(text)
    editor.place(text.index('Другий'))
    span = select_text_unit(editor, text, 'word')
    cursor = native_insertion_offset(editor, text)
    assert cursor == span[1] and cursor != editor.GetInsertionPoint()
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(ReadingDocument(text), cursor=cursor))
    recovered = ReaderDraftStore(tmp_path).load()
    reopened = NativeText(text)
    position = native_cursor_position(reopened, recovered.document.text, recovered.cursor)
    reopened.SetSelection(position, position)
    assert native_insertion_offset(reopened, recovered.document.text) == cursor
    assert recovered.document.source_text == text and recovered.document.text == text


def test_end_of_document_selection_failure_preserves_previous_selection():
    editor = NativeText('Only word')
    editor.place(6)
    assert select_text_unit(editor, editor.text, 'word') == (5, 9)
    before = editor.selection
    assert select_text_unit(editor, editor.text, 'word', direction=1) is None
    assert editor.selection == before

@pytest.mark.parametrize('normalized', [False, True])
def test_every_complete_native_crlf_and_surrogate_boundary_roundtrips(normalized):
    raw = '😀 A\r\n🧭Бета\r\nZ'
    displayed = raw.replace('\r\n', '\n') if normalized else raw
    editor = NativeText(displayed)
    for offset in range(len(raw) + 1):
        if raw[max(0, offset - 1):offset + 1] == '\r\n':
            # There is no displayed insertion boundary inside a source CRLF.
            with pytest.raises(ValueError, match='complete native text boundary'):
                native_cursor_position(editor, raw, offset)
            continue
        prefix = raw[:offset].replace('\r\n', '\n') if normalized else raw[:offset]
        expected = len(prefix.encode('utf-16-le')) // 2
        position = native_cursor_position(editor, raw, offset)
        assert position == expected
        editor.SetSelection(position, position)
        assert native_selection(editor, raw) == (offset, offset)
        if position < editor.GetLastPosition():
            # The returned complete prefix must not leave an LF from its CRLF.
            assert not (editor.GetRange(0, position).endswith('\r')
                        and editor.GetRange(position, editor.GetLastPosition()).startswith('\n'))
