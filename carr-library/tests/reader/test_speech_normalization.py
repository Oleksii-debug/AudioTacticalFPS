"""Exact finite language profiles and complete-or-fail proposal boundaries."""
from dataclasses import replace
from hashlib import sha256
import pytest

from carr_library.reader import speech_normalization as module
from carr_library.reader.speech_normalization import (
    SpeechEdit, SpeechOptions, prepare_speech, validate_preview,
)


def spoken(locale='en-US', **kwargs):
    return SpeechOptions(mode='speech', locale=locale, **kwargs)


@pytest.mark.parametrize('text', [
    '  001\r\n2026-11-27 e.g. 1.25 kg 😀 e\u0301 最後。\r\n',
    '# Title 2\n| Name | Count |\n| --- | --- |\n| A | 12 |\n',
    '```python\nx=1+2\n```\nNote[^a].\n[^a]: 42',
    '',
])
def test_verbatim_is_exact_not_normalized_and_never_invents_text(text):
    result = prepare_speech(text)
    assert result.text == text and not result.edits and not result.changed
    assert result.source_sha256 == result.output_sha256 == sha256(text.encode()).hexdigest()


@pytest.mark.parametrize(('text', 'expected'), [
    ('0 19 20 21 101', 'zero nineteen twenty twenty-one one hundred one'),
    ('1001; 2000000.', 'one thousand one; two million.'),
    ('-5; +2; 1.05.', 'minus five; plus two; one point zero five.'),
    ('999999999', 'nine hundred ninety-nine million nine hundred ninety-nine thousand nine hundred ninety-nine'),
])
def test_explicit_english_cardinals_preserve_existing_separators(text, expected):
    result = prepare_speech(text, spoken(numbers=True))
    assert result.text == expected and validate_preview(result) is result
    assert result.edits == tuple(sorted(result.edits, key=lambda edit: edit.start))
    for edit in result.edits:
        assert text[edit.start:edit.end] == edit.before


@pytest.mark.parametrize(('text', 'expected'), [
    ('21; 1001; 2002.', 'двадцять один; одна тисяча один; дві тисячі два.'),
    ('112000; 2000000.', 'сто дванадцять тисяч; два мільйони.'),
    ('1,5; 2,01; 0,000005.', "одна ціла п'ять десятих; дві цілих одна сота; нуль цілих п'ять мільйонних."),
    ('-12; +32.', 'мінус дванадцять; плюс тридцять два.'),
])
def test_explicit_ukrainian_cardinals_and_fraction_precision(text, expected):
    assert prepare_speech(text, spoken('uk-UA', numbers=True)).text == expected


def test_leading_zeroes_are_only_read_in_explicit_digit_mode():
    source = '007 0123.04'
    literal = prepare_speech(source, spoken(numbers=True))
    assert literal.text == source and literal.notices
    result = prepare_speech(source, spoken(numbers=True, number_style='digits'))
    assert result.text == 'zero zero seven zero one two three point zero four'
    assert prepare_speech('001,02', spoken('uk-UA', numbers=True, number_style='digits')).text == 'нуль нуль один кома нуль два'


@pytest.mark.parametrize(('locale', 'source', 'expected'), [
    ('en-US', '1 kg; 2 kg; 1.5 kg; 60 km/h; 2 m².', 'one kilogram; two kilograms; one point five kilograms; sixty kilometers per hour; two square meters.'),
    ('uk-UA', '1 кг; 2 кг; 5 кг; 21 кг.', "один кілограм; два кілограми; п'ять кілограмів; двадцять один кілограм."),
    ('uk-UA', '2 s; 21 хв; 1,5 kg.', "дві секунди; двадцять одна хвилина; одна ціла п'ять десятих кілограма."),
    ('en-US', '25%; -2 °C; 5 GB.', 'twenty-five percent; minus two degrees Celsius; five gigabytes.'),
])
def test_only_explicit_case_sensitive_units_are_expanded(locale, source, expected):
    assert prepare_speech(source, spoken(locale, numbers=True, units=True)).text == expected


def test_independent_unit_and_number_choices_do_not_silently_enable_each_other():
    assert prepare_speech('2 kg', spoken(units=True)).text == '2 kilograms'
    assert prepare_speech('2 kg', spoken(numbers=True)).text == 'two kg'
    assert prepare_speech('2 кг', spoken(numbers=True, units=True)).text == '2 кг'
    assert prepare_speech('25%', spoken(symbols=True)).text == '25 percent sign'
    assert prepare_speech('1,5 kg', spoken(numbers=True, units=True)).text == '1,5 kg'


@pytest.mark.parametrize(('locale', 'source', 'format', 'expected'), [
    ('en-US', '2026-11-27', 'iso', 'November twenty-seventh, year two thousand twenty-six'),
    ('uk-UA', '2026-11-27', 'iso', 'двадцять сьомого листопада, рік дві тисячі двадцять шість'),
    ('en-US', '02/29/2024', 'locale', 'February twenty-ninth, year two thousand twenty-four'),
    ('uk-UA', '29.02.2024', 'locale', 'двадцять дев’ятого лютого, рік дві тисячі двадцять чотири'),
])
def test_dates_require_exact_selected_format_and_real_calendar_day(locale, source, format, expected):
    assert prepare_speech(source, spoken(locale, dates=True, date_format=format)).text == expected


@pytest.mark.parametrize('source', ['2023-02-29', '27.11.2026', '2026/11/27', '1/2/2026', '2026-13-01'])
def test_nonselected_or_invalid_date_is_never_partially_read_as_math(source):
    result = prepare_speech(source, spoken(numbers=True, dates=True, math=True))
    assert result.text == source and result.notices


def test_date_recognition_is_not_implicitly_enabled_by_numbers():
    text = '2026-11-27'
    assert prepare_speech(text, spoken(numbers=True, math=True)).text == text


@pytest.mark.parametrize('source', [
    'id007 A12B v1.2.3 127.0.0.1 1e9 a2026-11-27 2foo-12 978-0-12-345678-9',
    'https://example.org/a/12?x=5&y=4 user42@example.org',
    '`x = 12 + 3` Renée Łukasz Іван 😀 e\u0301',
    '1000000000 1.1234567 123456789012345678901234567890',
    '1,234 2,500',
])
def test_uninterpreted_identifiers_names_code_and_ambiguous_numbers_remain_literal(source):
    result = prepare_speech(source, spoken(numbers=True, dates=True, units=True, math=True, symbols=True, abbreviations=True))
    assert result.text == source


def test_decimal_override_is_explicit_and_does_not_guess_from_neighbouring_text():
    assert prepare_speech('1.5', spoken('uk-UA', numbers=True)).text == '1.5'
    assert prepare_speech('1.5', spoken('uk-UA', numbers=True, decimal_mark='.')).text == "одна ціла п'ять десятих"
    assert prepare_speech('1,5', spoken(numbers=True, decimal_mark=',')).text == 'one point five'


@pytest.mark.parametrize(('locale', 'source', 'expected'), [
    ('en-US', '2+3=5; 2--3; 2×4≤9', 'two plus three equals five; two minus minus three; two times four less than or equal to nine'),
    ('uk-UA', '2 + 3 = 5', "два плюс три дорівнює п'ять"),
])
def test_numeric_math_is_spoken_without_evaluation(locale, source, expected):
    assert prepare_speech(source, spoken(locale, numbers=True, math=True)).text == expected
    assert prepare_speech(source, spoken(locale, numbers=True)).text == source


def test_math_and_symbols_do_not_infer_missing_terms_or_compute_values():
    result = prepare_speech('2+3=99; A&B; √ 9; €5', spoken(numbers=True, math=True, symbols=True))
    assert result.text == 'two plus three equals ninety-nine; A ampersand B; square root sign nine; euro sign five'
    assert prepare_speech('x+y=5', spoken(math=True)).text == 'x+y=5'


@pytest.mark.parametrize(('locale', 'source', 'expected'), [
    ('en-US', 'e.g. 2 items, approx. 3 etc.', 'for example two items, approximately three et cetera.'),
    ('uk-UA', 'напр. 2, прибл. 3 і т. д.', 'наприклад два, приблизно три і так далі.'),
])
def test_small_locale_abbreviation_list_has_no_title_or_name_guessing(locale, source, expected):
    assert prepare_speech(source, spoken(locale, numbers=True, abbreviations=True)).text == expected
    assert prepare_speech('Dr. Mr. ст. р. Renée', spoken(locale, abbreviations=True)).text == 'Dr. Mr. ст. р. Renée'


def test_keep_structures_protects_the_whole_block_from_numeric_expansions():
    source = '# Title 12\r\n\r\n| A | B |\r\n| --- | --- |\r\n| 1 | 2 |\r\n\r\n```py\r\nx=12\r\n```\r\n[^a]: 45\r\n    46\r\nText 9[^a].'
    result = prepare_speech(source, spoken(numbers=True, symbols=True))
    assert result.text == source.replace('Text 9', 'Text nine')
    assert len(result.edits) == 1


def test_structural_read_labels_preserve_cell_values_and_code_content():
    source = '# Heading 2\n| Name | Count |\n| --- | --- |\n| A | 12 |\n```py\nx=2+3\n```\nText[^a].\n[^a]: Count 5\n'
    options = spoken(numbers=True, math=True, headings='read', tables='read', code='read', footnotes='read')
    result = prepare_speech(source, options)
    assert result.text == ('Heading level one: Heading two\nTable.\n'
        'Row one. Column one: Name; Column two: Count\n'
        'Row two. Column one: A; Column two: twelve\n'
        'Code.\nx=2+3\nEnd code.\nText Footnote a.\nFootnote a: Count five\n')
    assert {edit.kind for edit in result.edits} == {'heading', 'table', 'code', 'footnote'}


def test_explicit_skip_removes_only_recognized_structure_and_keeps_line_separation():
    source = 'before\r\n# title 2\r\n```\rx=5\r```\r[^a]: note\r\n    extra\r\nText[^a] after'
    result = prepare_speech(source, spoken(headings='skip', code='skip', footnotes='skip'))
    assert result.text == 'before\r\n\r\n\r\r\nText after'
    assert validate_preview(result) is result


@pytest.mark.parametrize(('source', 'choices'), [
    ('```py\nunclosed', {'code': 'skip'}),
    ('| A | B |\n| --- | --- |\n| only |\n', {'tables': 'read'}),
    ('| A | B |\n| --- | --- |\n| a\\|b | c |\n', {'tables': 'skip'}),
    ('| A | B |\n| --- | --- |\n| `x` | c |\n', {'tables': 'read'}),
])
def test_unknown_or_lossy_structural_forms_refuse_the_complete_proposal(source, choices):
    with pytest.raises(ValueError):
        prepare_speech(source, spoken(numbers=True, **choices))
    assert prepare_speech(source, spoken(numbers=True)).text == source


@pytest.mark.parametrize('kwargs', [
    {'mode': 'guess'}, {'mode': 'speech'}, {'mode': 'speech', 'locale': 'ru-RU'},
    {'numbers': 1}, {'numbers': True}, {'headings': 'skip'}, {'decimal_mark': 'automatic'},
    {'number_style': 'scientific'}, {'date_format': 'automatic'}, {'locale': False},
])
def test_invalid_or_hidden_verbatim_options_are_rejected(kwargs):
    with pytest.raises(ValueError):
        SpeechOptions(**kwargs)


@pytest.mark.parametrize('mutation', ['output', 'source', 'span', 'digest', 'overlap'])
def test_in_memory_preview_cannot_be_rebound_or_silently_edited(mutation):
    preview = prepare_speech('12 13', spoken(numbers=True))
    if mutation == 'output':
        changed = replace(preview, text='hidden edit')
    elif mutation == 'source':
        changed = replace(preview, source='13 12')
    elif mutation == 'span':
        changed = replace(preview, edits=(replace(preview.edits[0], start=1),))
    elif mutation == 'digest':
        changed = replace(preview, source_sha256='0' * 64)
    else:
        changed = replace(preview, edits=(preview.edits[0], preview.edits[0]))
    with pytest.raises(ValueError):
        validate_preview(changed)


def test_cancellation_after_real_progress_returns_no_partial_proposal():
    checks = [0]
    def stop():
        checks[0] += 1
        return checks[0] >= 12
    with pytest.raises(InterruptedError):
        prepare_speech(' '.join(str(value) for value in range(100)), spoken(numbers=True), cancelled=stop)
    assert checks[0] == 12


def test_exact_deadline_and_post_transform_deadline_fail_closed(monkeypatch):
    ticks = iter([0, 1_000_000])
    monkeypatch.setattr(module, 'perf_counter_ns', lambda: next(ticks))
    with pytest.raises(TimeoutError):
        prepare_speech('12', spoken(numbers=True), timeout=.001)


def test_edit_output_and_token_allowances_never_return_truncated_success(monkeypatch):
    with pytest.raises(ValueError):
        prepare_speech('x' * (module.MAX_TEXT_CHARACTERS + 1))
    monkeypatch.setattr(module, 'MAX_EDITS', 2)
    with pytest.raises(ValueError, match='5,000'):
        prepare_speech('1 2 3', spoken(numbers=True))
    monkeypatch.setattr(module, 'MAX_EDITS', 5000)
    monkeypatch.setattr(module, 'MAX_OUTPUT_CHARACTERS', 9)
    with pytest.raises(ValueError, match='output allowance'):
        prepare_speech('999', spoken(numbers=True))
    monkeypatch.setattr(module, 'MAX_OUTPUT_CHARACTERS', 1000000)
    monkeypatch.setattr(module, 'MAX_TOKENS', 2)
    with pytest.raises(ValueError, match='token allowance'):
        prepare_speech('a@example.org b@example.org c@example.org', spoken())


def test_long_uninterpreted_token_does_not_repeat_an_unanchored_email_scan():
    # A single contiguous candidate is checked once; no quadratic suffix scan.
    source = 'a' * 199000
    result = prepare_speech(source, spoken(numbers=True), timeout=10)
    assert result.text == source and not result.edits


def test_forged_shared_replacements_are_bounded_before_reconstruction(monkeypatch):
    preview = prepare_speech('1 2', spoken(numbers=True))
    # The same replacement is reused, so constructing the malicious envelope
    # itself is small. Validation must reject before any oversized join.
    monkeypatch.setattr(module, 'MAX_OUTPUT_CHARACTERS', 64)
    replacement = 'x' * 64
    edits = tuple(replace(edit, after=replacement) for edit in preview.edits)
    with pytest.raises(ValueError, match='output allowance'):
        validate_preview(replace(preview, edits=edits))


def test_forged_replacement_invalid_unicode_is_refused_before_publication():
    preview = prepare_speech('12', spoken(numbers=True))
    with pytest.raises(ValueError, match='Unicode'):
        validate_preview(replace(preview, edits=(replace(preview.edits[0], after='\ud800'),)))



# The complete PR99 EN/UK oracle above is retained. These cases qualify only
# the additive SK/layout profile, not native pronunciation quality.
@pytest.mark.parametrize(('source', 'expected'), [
    ('0 1 2 21 101', 'nula jeden dva dvadsaťjeden stojeden'),
    ('-5; +2; 1,05.', 'mínus päť; plus dva; jeden čiarka nula päť.'),
    ('1 kg; 2 kg; 5 kg; 2 s.', 'jeden kilogram; dva kilogramy; päť kilogramov; dve sekundy.'),
    ('2024-02-29', 'deň dvadsaťdeväť, mesiac dva, rok dvetisícdvadsaťštyri'),
    ('2+3=5', 'dva plus tri rovná sa päť'),
    ('napr. 2; t. j. 5', 'napríklad dva; to jest päť'),
    ('@ € ∞', 'zavináč znak eura nekonečno'),
])
def test_slovak_finite_cardinal_and_labelled_forms(source, expected):
    result = prepare_speech(source, spoken('sk-SK', numbers=True, dates=True,
        units=True, abbreviations=True, math=True, symbols=True))
    assert result.text == expected
    assert validate_preview(result) is result


def test_slovak_explicit_decimal_and_calendar_formats_do_not_guess():
    assert prepare_speech('001,02', spoken('sk-SK', numbers=True, number_style='digits')).text == 'nula nula jeden čiarka nula dva'
    assert prepare_speech('29.02.2024', spoken('sk-SK', dates=True, date_format='locale')).text == 'deň dvadsaťdeväť, mesiac dva, rok dvetisícdvadsaťštyri'
    for source in ('02/29/2024', '31.02.2024', '1.234', '001'):
        result = prepare_speech(source, spoken('sk-SK', dates=True, date_format='locale', numbers=True))
        assert result.text == source and result.notices
    assert prepare_speech('1,5 kg', spoken('sk-SK', numbers=True, units=True)).text == 'jeden čiarka päť jednotka kilogram'


@pytest.mark.parametrize('locale,expected', [
    ('en-US', 'Heading level one: Title two\nFootnote a: five\n'),
    ('uk-UA', 'Заголовок рівня один: Title два\nПримітка a: п\'ять\n'),
    ('sk-SK', 'Nadpis úrovne jeden: Title dva\nPoznámka a: päť\n'),
])
def test_explicit_structure_labels_keep_same_original_offsets_in_three_locales(locale, expected):
    source = '# Title 2\n[^a]: 5\n'
    result = prepare_speech(source, spoken(locale, headings='read', footnotes='read', numbers=True))
    assert result.text == expected
    assert [(e.start, e.end, e.before) for e in result.edits] == [(0, 10, '# Title 2\n'), (10, 18, '[^a]: 5\n')]


def test_layout_choices_preserve_blank_paragraphs_punctuation_and_structures():
    source = 'hello\r\nworld\r\n\r\nsecond para?\r\n# lower heading\r\n~~~\nlower code\n~~~\n'
    result = prepare_speech(source, spoken(capitalize_paragraphs=True, join_wrapped_lines=True))
    assert result.text == 'Hello world\r\n\r\nSecond para?\r\n# lower heading\r\n~~~\nlower code\n~~~\n'
    assert validate_preview(result) is result
    assert '?' in result.text and result.text.count('\r\n\r\n') == 1


def test_soft_hyphens_are_explicit_and_real_hyphens_or_identifiers_are_preserved():
    source = 'co\u00ad\r\noperate\nre-\nenter\nhttps://example.org/\nnext\n'
    result = prepare_speech(source, spoken(join_soft_hyphens=True))
    assert result.text == 'cooperate\nre-\nenter\nhttps://example.org/\nnext\n'
    assert len(result.edits) == 1 and result.edits[0].before == '\u00ad\r\n'
    assert prepare_speech(source, spoken()).text == source


def test_capitalization_combines_with_expansion_without_overlapping_spans():
    source = 'e.g. 2 kg\n\nhttps://example.org\n'
    result = prepare_speech(source, spoken(numbers=True, units=True, abbreviations=True, capitalize_paragraphs=True))
    assert result.text == 'For example two kilograms\n\nhttps://example.org\n'
    assert validate_preview(result) is result
    assert all(a.end <= b.start for a, b in zip(result.edits, result.edits[1:]))


def test_format_choices_cannot_be_hidden_in_verbatim_or_bypass_boolean_validation():
    for field in ('capitalize_paragraphs', 'join_soft_hyphens', 'join_wrapped_lines'):
        with pytest.raises(ValueError):
            SpeechOptions(**{field: True})
        with pytest.raises(ValueError):
            spoken(**{field: 1})


def test_exact_pinned_slovak_source_closure_and_license_ship_with_the_module():
    import ast
    import json
    from pathlib import Path
    from hashlib import sha1
    from carr_library.reader import _vendor_num2words
    root = Path(_vendor_num2words.__file__).parent
    record = json.loads((root / 'UPSTREAM.json').read_bytes())
    assert record['commit'] == '6abe2c64714a38e41de23fe20fa319d0ad2fd845'
    assert record['license'] == 'LGPL-2.1-or-later'
    assert set(record['files']) == {'lang_SK.py', 'base.py', 'utils.py', 'compat.py', 'currency.py', 'COPYING'}
    for name, selected in record['files'].items():
        raw = (root / name).read_bytes()
        assert sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest() == selected['git_blob']
        if name.endswith('.py'):
            assert b'GNU Lesser General Public' in raw
            assert not any(isinstance(node, ast.Import) and any(alias.name == 'docopt' for alias in node.names)
                           for node in ast.walk(ast.parse(raw)))
    assert 'GNU LESSER GENERAL PUBLIC LICENSE' in (root / 'COPYING').read_text()
    assert not any(isinstance(node, (ast.Import, ast.ImportFrom))
                   for node in ast.walk(ast.parse((root / '__init__.py').read_bytes())))
