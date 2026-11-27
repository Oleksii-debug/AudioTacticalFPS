"""Finite language preparation uses real reading-copy history, never source edits."""
from hashlib import sha256

import pytest

from carr_library.speech import preparation as prep
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.reading_tools import ReadingSnapshot


def prepared(text, options, **kwargs):
    document = ReadingDocument(text)
    snapshot = ReadingSnapshot.capture(document)
    result = prep.preview_preparation(snapshot, options, **kwargs)
    assert document.text == text and not document.can_undo
    return document, result


@pytest.mark.parametrize(('language', 'expected'), [
    ('en', 'zero one two'), ('uk', 'нуль один два'), ('sk', 'nula jeden dva'),
])
def test_explicit_digit_names_preserve_leading_zero_and_source(language, expected):
    document, result = prepared('012', prep.PreparationOptions(language, numbers=True))
    assert result.preview.changed and result.preview.apply(document)
    assert document.text == expected and document.source_text == '012'
    assert result.counts == (('digit-by-digit integers', 1),)
    assert document.undo() and document.text == '012' and not document.can_undo
    assert document.redo() and document.text == expected


@pytest.mark.parametrize(('language', 'expected'), [
    ('en', 'year two zero two four, month zero two, day two nine'),
    ('uk', 'рік два нуль два чотири, місяць нуль два, день два дев’ять'),
    ('sk', 'rok dva nula dva štyri, mesiac nula dva, deň dva deväť'),
])
def test_valid_iso_date_uses_explicit_year_month_day_and_keeps_punctuation(language, expected):
    document, result = prepared('(2024-02-29).', prep.PreparationOptions(language, iso_dates=True))
    assert result.preview.apply(document)
    assert document.text == '(' + expected + ').'
    assert result.retained_iso_dates == 0
    assert document.source_text == '(2024-02-29).'


@pytest.mark.parametrize('value', [
    '2023-02-29', '0000-01-01', '2024-13-02', '2024-04-31',
], ids=['not-leap-year', 'zero-year', 'invalid-month', 'invalid-day'])
def test_invalid_dates_are_retained_even_with_numbers_and_symbols(value):
    document, result = prepared(value, prep.PreparationOptions(
        'en', numbers=True, symbols=True, iso_dates=True))
    assert not result.preview.changed and not result.preview.apply(document)
    assert result.retained_iso_dates == 1 and document.text == value
    assert 'retained verbatim' in result.description()


@pytest.mark.parametrize('value', [
    '03/04/2026', '12,500', '3.14', '-12', '2026-11-27T12:30',
    'https://example.invalid/123', 'name123@example.invalid', 'chapter_123',
    r'C:\sources\123.txt', 'v1.2.3', '١٢٣', '１２３', chr(96) + '123' + chr(96),
], ids=['ambiguous-date', 'comma-number', 'decimal', 'ascii-sign', 'datetime',
        'url', 'email', 'identifier', 'path', 'version', 'arabic-digits',
        'fullwidth-digits', 'inline-code'])
def test_unadmitted_number_date_or_identifier_syntax_is_not_guessed(value):
    document, result = prepared(value, prep.PreparationOptions(
        'en', numbers=True, symbols=True, iso_dates=True))
    assert not result.preview.changed and document.text == value


def test_unselected_iso_date_is_not_reinterpreted_as_a_math_expression():
    document, result = prepared('2026-11-27', prep.PreparationOptions(
        'en', numbers=True, symbols=True))
    assert not result.preview.changed and document.text == '2026-11-27'
    assert result.retained_iso_dates == 1


@pytest.mark.parametrize(('language', 'expected'), [
    ('en', 'two plus three equals five; percent sign'),
    ('uk', 'два плюс три дорівнює п’ять; знак відсотка'),
    ('sk', 'dva plus tri rovná sa päť; znak percenta'),
])
def test_explicit_finite_operator_names_do_not_evaluate_or_rewrite_a_source(language, expected):
    document, result = prepared('2+3=5; %', prep.PreparationOptions(language, numbers=True, symbols=True))
    assert result.preview.apply(document)
    assert document.text == expected and document.source_text == '2+3=5; %'
    assert not document.source_span(0, len(document.text)).exact


def test_symbol_only_choice_does_not_enable_digit_expansion():
    document, result = prepared('2+3=5', prep.PreparationOptions('en', symbols=True))
    result.preview.apply(document)
    assert document.text == '2 plus 3 equals 5'


def test_default_options_are_verbatim_for_each_supported_language():
    value = '012 2024-02-29 + foo-\nbar  soft\u00adhyphen'
    for language, _ in prep.LANGUAGES:
        document, result = prepared(value, prep.PreparationOptions(language))
        assert not result.preview.changed
        assert not result.preview.apply(document)
        assert document.text == value and result.counts == ()


def test_explicit_existing_cleanup_and_soft_hyphens_share_one_undo():
    value = 'Alpha \t beta-\r\nword\u00ad  12\nNext'
    document, result = prepared(value, prep.PreparationOptions(
        'uk', numbers=True, normalize_spaces=True, join_hyphenated=True,
        remove_soft_hyphens=True))
    assert result.preview.apply(document)
    assert document.text == 'Alpha betaword один два\nNext'
    assert len(document._edits) == 1
    assert document.undo() and document.text == value
    assert document.source_text == value


def test_already_single_spaces_are_not_reported_as_changes():
    document, result = prepared('One ordinary line', prep.PreparationOptions('en', normalize_spaces=True))
    assert result.counts == () and not result.preview.changed
    assert document.text == 'One ordinary line'


def test_scope_and_unchanged_source_offsets_survive_real_file_backed_preview(tmp_path):
    source = tmp_path / 'original.txt'
    original = '😀 prefix 12 suffix 34'
    source.write_text(original, encoding='utf-8')
    raw = source.read_bytes()
    document = ReadingDocument(original, source_path=source)
    begin = original.index('12')
    result = prep.preview_preparation(ReadingSnapshot.capture(document),
        prep.PreparationOptions('en', numbers=True), scope=(begin, begin + 2))
    assert (result.preview.start, result.preview.end) == (begin, begin + 2)
    assert result.preview.apply(document)
    assert document.text == '😀 prefix one two suffix 34'
    assert document.source_span(0, begin).exact
    assert document.source_span(0, begin).end == begin
    assert not document.source_span(begin, begin + len('one two')).exact
    assert document.source_sha256 == sha256(original.encode()).hexdigest()
    assert source.read_bytes() == raw
    assert document.undo() and document.text == original and source.read_bytes() == raw


@pytest.mark.parametrize('change', ['edit', 'same-text-other-document', 'cancel'])
def test_current_snapshot_and_cancellation_are_required_at_apply(change):
    document, result = prepared('12', prep.PreparationOptions('en', numbers=True))
    if change == 'edit':
        document.set_text('34')
    elif change == 'same-text-other-document':
        document = ReadingDocument('12')
    before = document.to_dict()
    with pytest.raises((ValueError, InterruptedError)):
        result.preview.apply(document, cancelled=lambda: change == 'cancel')
    assert document.to_dict() == before


@pytest.mark.parametrize('language', ['', 'auto', 'en-US', 'ru', None, False, []],
                         ids=['empty', 'auto', 'region-not-inferred', 'unsupported',
                              'null', 'boolean', 'array'])
def test_language_is_explicit_and_never_falls_back(language):
    with pytest.raises(ValueError, match='explicitly'):
        prep.PreparationOptions(language)


@pytest.mark.parametrize('field', ['numbers', 'iso_dates', 'symbols', 'normalize_spaces',
                                  'join_hyphenated', 'remove_soft_hyphens'])
def test_transform_flags_are_exact_booleans(field):
    with pytest.raises(ValueError, match='boolean'):
        prep.PreparationOptions('en', **{field: 1})


@pytest.mark.parametrize('scope', [(0, 0), (0, 3), (-1, 1), (True, 1), [0, 1]])
def test_invalid_scope_does_not_create_an_edit(scope):
    document = ReadingDocument('12')
    with pytest.raises(ValueError, match='range'):
        prep.preview_preparation(ReadingSnapshot.capture(document),
            prep.PreparationOptions('en', numbers=True), scope=scope)
    assert document.text == '12' and not document.can_undo


def test_input_output_and_change_budgets_precede_any_apply(monkeypatch):
    document = ReadingDocument('1 2 3')
    snapshot = ReadingSnapshot.capture(document)
    monkeypatch.setattr(prep, 'MAX_INPUT_CHARACTERS', 4)
    with pytest.raises(ValueError, match='200,000'):
        prep.preview_preparation(snapshot, prep.PreparationOptions('en', numbers=True))
    monkeypatch.setattr(prep, 'MAX_INPUT_CHARACTERS', 200_000)
    monkeypatch.setattr(prep, 'MAX_OUTPUT_CHARACTERS', 5)
    with pytest.raises(ValueError, match='output budget'):
        prep.preview_preparation(snapshot, prep.PreparationOptions('en', numbers=True))
    monkeypatch.setattr(prep, 'MAX_OUTPUT_CHARACTERS', 400_000)
    monkeypatch.setattr(prep, 'MAX_TRANSFORMATIONS', 2)
    with pytest.raises(ValueError, match='Too many'):
        prep.preview_preparation(snapshot, prep.PreparationOptions('en', numbers=True))
    assert document.text == '1 2 3' and not document.can_undo


def test_cancellation_during_real_bounded_scan_does_not_create_an_edit():
    document = ReadingDocument('12 ' * 1000)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 20
    with pytest.raises(InterruptedError):
        prep.preview_preparation(ReadingSnapshot.capture(document),
            prep.PreparationOptions('en', numbers=True), cancelled=cancelled)
    assert calls == 20 and not document.can_undo


def test_total_deadline_is_checked_before_result_delivery(monkeypatch):
    document = ReadingDocument('12')
    ticks = iter((0.0, 0.0, 0.0, 6.0))
    monkeypatch.setattr(prep.time, 'monotonic', lambda: next(ticks, 6.0))
    with pytest.raises(TimeoutError, match='time budget'):
        prep.preview_preparation(ReadingSnapshot.capture(document),
            prep.PreparationOptions('en', numbers=True))
    assert document.text == '12' and not document.can_undo


@pytest.mark.parametrize('timeout', [True, 0, -1, 6, float('nan'), float('inf')])
def test_deadline_cannot_be_disabled_or_extended(timeout):
    with pytest.raises(ValueError, match='deadline'):
        prep.preview_preparation(ReadingSnapshot.capture(ReadingDocument('12')),
            prep.PreparationOptions('en', numbers=True), timeout=timeout)
