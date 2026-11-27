"""Actual selected optional libraries/native Hunspell; this qualification never skips.

Run only in the explicit reading-tools gate, which supplies both pinned packs.
No mock or host-site-package substitution is accepted for these cases.
"""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil

import pytest

from carr_library.reader.narration import apply_preparation, encode_project, new_project
from carr_library.reader.preparation import PreparationContext, prepare_selection, spellcheck_selection, spelling_proposal


@pytest.fixture
def normalizer():
    value = os.environ.get('CARR_READING_PREPARATION_MANIFEST')
    assert value and Path(value).is_absolute() and Path(value).is_file(), 'The gate must supply its reviewed optional library manifest.'
    return Path(value)


@pytest.fixture
def hunspell():
    value = os.environ.get('CARR_HUNSPELL_MANIFEST')
    assert value and Path(value).is_absolute() and Path(value).is_file(), 'The gate must supply its built/pinned native Hunspell manifest.'
    return Path(value)


@pytest.mark.parametrize('text,language,mode,expected', [
    ('42', 'en', 'cardinal', 'forty-two'),
    ('42', 'uk', 'cardinal', 'сорок два'),
    ('100', 'sk', 'cardinal', 'sto'),
    ('0012', 'en', 'digits', 'zero zero one two'),
    ('1.5', 'en', 'cardinal', 'one point five'),
    ('21', 'en', 'ordinal', 'twenty-first'),
])
def test_actual_selected_number_libraries_preserve_source_and_generate_reviewable_words(
        normalizer, text, language, mode, expected):
    project = new_project('Before ' + text + ' after.')
    result = prepare_selection(project, 7, 7 + len(text),
                               PreparationContext(language, 'number', number_mode=mode), normalizer)
    assert result.record.after == expected
    assert result.text == 'Before ' + expected + ' after.'
    assert project.current_text == project.original_text
    assert result.record.engine == 'reading-preparation'
    assert 'Babel 2.18.0' in result.record.engine_version


@pytest.mark.parametrize('written,expected', [('1 kg', 'one kilogram'), ('2 kg', 'two kilograms')])
def test_actual_cldr_units_keep_numeric_plural_context(normalizer, written, expected):
    project = new_project(written)
    result = prepare_selection(project, 0, len(written),
        PreparationContext('en', 'unit', unit_id='mass-kilogram', unit_symbol='kg'), normalizer)
    assert result.text == expected and project.current_text == written


@pytest.mark.parametrize('written,order,expected', [
    ('2000-01-02', 'ymd', 'January two, two thousand'),
    ('03/04/2000', 'dmy', 'April three, two thousand'),
    ('03/04/2000', 'mdy', 'March four, two thousand'),
])
def test_actual_cldr_dates_use_explicit_order_without_guessing(normalizer, written, order, expected):
    project = new_project(written)
    result = prepare_selection(project, 0, len(written),
                               PreparationContext('en', 'date', date_order=order), normalizer)
    assert result.text == expected and project.current_text == written


def test_actual_unicode_symbol_name_is_explicit_english_and_keeps_whitespace(normalizer):
    written = '\r\n & \r\n'
    project = new_project(written)
    result = prepare_selection(project, 0, len(written), PreparationContext('en', 'symbol'), normalizer)
    assert result.text == '\r\n ampersand \r\n'
    assert 'Unicode ' in result.record.engine_version


@pytest.mark.parametrize('written,kind', [
    ('0012', 'number'), ('1e6', 'number'), ('1.2.3', 'number'),
    ('03/04/26', 'date'), ('2000-02-30', 'date'),
])
def test_actual_worker_preserves_ambiguous_or_invalid_selection(normalizer, written, kind):
    project = new_project(written)
    before = encode_project(project)
    with pytest.raises(ValueError, match='ambiguous|unsupported'):
        prepare_selection(project, 0, len(written), PreparationContext('en', kind), normalizer)
    assert encode_project(project) == before


def test_actual_hunspell_c_api_unicode_paths_affixes_and_suggestions(hunspell):
    text = 'hello helo Привіт books wrld world'
    project = new_project(text)
    result = spellcheck_selection(project, 0, len(text), hunspell, language='en')
    assert [row.word for row in result.issues] == ['helo', 'wrld']
    assert 'hello' in result.issues[0].suggestions
    assert 'world' in result.issues[1].suggestions
    changed = apply_preparation(project, spelling_proposal(project, result, 0, 'hello'))
    assert changed.current_text == 'hello hello Привіт books wrld world'
    assert changed.original_text == text
    assert result.engine_version == '1.7.6'


def test_actual_hunspell_ignores_path_tools_and_has_no_native_executable_fallback(hunspell, tmp_path, monkeypatch):
    fake = tmp_path / 'untrusted-path'
    fake.mkdir()
    (fake / 'hunspell.exe').write_bytes(b'not a native runtime')
    monkeypatch.setenv('PATH', str(fake))
    project = new_project('helo')
    report = spellcheck_selection(project, 0, 4, hunspell, language='en')
    assert 'hello' in report.issues[0].suggestions
    assert (fake / 'hunspell.exe').read_bytes() == b'not a native runtime'


def test_actual_native_pack_tampering_is_refused_before_load(hunspell, tmp_path):
    root = tmp_path / 'copied-pack'
    shutil.copytree(hunspell.parent, root)
    path = root / hunspell.name
    value = json.loads(path.read_bytes())
    binary = root / value['settings']['dll']
    binary.write_bytes(binary.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='Checksum'):
        spellcheck_selection(new_project('helo'), 0, 4, path, language='en')
