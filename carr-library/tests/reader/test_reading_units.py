"""Exact text ranges, without claiming linguistic or spoken-word alignment."""
import pytest

from carr_library.reader.reading_units import reading_unit_range


@pytest.mark.parametrize(('raw', 'cursor', 'unit', 'expected'), [
    ('п’ять і Cafe\u0301.', 2, 'word', 'п’ять'),
    ("don't alter", 3, 'word', "don't"),
    ('😀 Cafe\u0301 kept.', 6, 'word', 'Cafe\u0301'),
    ('name_12 value', 5, 'word', 'name_12'),
    ('first  next', 6, 'word', 'next'),
    ('first ... ', 10, 'word', 'first'),
    ('日本語。 Next.', 1, 'word', '日本語'),
    ('First sentence. Second?', 8, 'sentence', 'First sentence.'),
    ('First sentence. Second?', 15, 'sentence', 'Second?'),
    ('First sentence. Second?', 23, 'sentence', 'Second?'),
    ('He said “Yes!” Next.', 10, 'sentence', 'He said “Yes!”'),
    ('Dr. Smith speaks.', 5, 'sentence', 'Smith speaks.'),
    ('One\r\nsoft wrap. Next!', 6, 'sentence', 'One\r\nsoft wrap.'),
    ('One\r\nsoft wrap\r\n\r\nTwo', 8, 'paragraph', 'One\r\nsoft wrap'),
    ('One\r\nsoft wrap\r\n\r\nTwo', 16, 'paragraph', 'Two'),
    ('One\r\nsoft wrap\r\n\r\nTwo', 21, 'paragraph', 'Two'),
    ('One\n \t\n  Two\nsoft wrap\n', 7, 'paragraph', 'Two\nsoft wrap'),
    ('One\r\rTwo', 5, 'paragraph', 'Two'),
    ('One\u2029Two', 5, 'paragraph', 'Two'),
    ('  One.\n\n  Two without punctuation  ', 12, 'sentence', 'Two without punctuation'),
])
def test_cursor_units_preserve_the_exact_unicode_source_slice(raw, cursor, unit, expected):
    original = raw.encode('utf-8')
    start, end = reading_unit_range(raw, cursor, unit)
    assert raw[start:end] == expected
    assert raw.encode('utf-8') == original
    assert 0 <= start < end <= len(raw)


@pytest.mark.parametrize('newline', ['\r\n', '\n', '\r'])
def test_one_newline_is_a_soft_wrap_and_only_blank_lines_split_paragraphs(newline):
    first = 'Alpha' + newline + 'Beta'
    raw = first + newline + ' \t' + newline + 'Gamma'
    assert reading_unit_range(raw, raw.index('Beta') + 2, 'paragraph') == (0, len(first))
    start, end = reading_unit_range(raw, raw.index('Gamma'), 'paragraph')
    assert raw[start:end] == 'Gamma'


@pytest.mark.parametrize('unit', ['word', 'sentence', 'paragraph'])
@pytest.mark.parametrize('raw', ['', ' \r\n\r\n\t '])
def test_empty_units_do_not_turn_into_a_whole_document_command(unit, raw):
    with pytest.raises(ValueError, match='no .*text|no word'):
        reading_unit_range(raw, len(raw), unit)


@pytest.mark.parametrize('cursor', [-1, 4, True, 0.5, '1', None])
def test_invalid_source_offsets_are_rejected(cursor):
    with pytest.raises(ValueError, match='cursor'):
        reading_unit_range('abc', cursor, 'word')


@pytest.mark.parametrize('raw', ['bad\0text', '\ud800', 'x' * 2_000_001])
def test_invalid_or_oversized_source_text_is_not_selected(raw):
    with pytest.raises(ValueError):
        reading_unit_range(raw, 0, 'sentence')


def test_unknown_unit_does_not_silently_read_another_scope():
    with pytest.raises(ValueError, match='word, sentence or paragraph'):
        reading_unit_range('source', 0, 'document')


@pytest.mark.parametrize('unit', ['word', 'sentence', 'paragraph'])
def test_range_selection_checks_initial_cancellation(unit):
    with pytest.raises(InterruptedError, match='cancelled'):
        reading_unit_range('Exact source.', 0, unit, cancelled=lambda: True)


@pytest.mark.parametrize('unit', ['word', 'sentence', 'paragraph'])
def test_large_cursor_search_is_incrementally_cancellable(unit):
    raw = ('x' * 180_000 if unit == 'word' else ('One paragraph.\n\n' * 20_000))
    checks = []
    def cancelled():
        checks.append(True)
        return len(checks) >= 6
    with pytest.raises(InterruptedError, match='cancelled'):
        reading_unit_range(raw, len(raw) - 3, unit, cancelled=cancelled)
    assert len(checks) == 6
