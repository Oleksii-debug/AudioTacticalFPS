"""Authored foreign syntax on the actual existing pronunciation/backup pipeline."""
from hashlib import sha256
import json
from pathlib import Path
import shutil

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.speech.reading import ReadingDocument, PronunciationRule, import_dictionary
from carr_library.speech.pronunciation_formats import parse_dictionary
from carr_library.ui.pronunciation_dialog import (
    DictionaryStore, ReadingSnapshot, apply_pronunciation_preview,
    preview_pronunciations, read_dictionary,
)


def bxd(rows):
    value = '\ufeffVersion=1\r\nDelimiter=\x03\r\nAuthored fixture\x03Tests\x03No borrowed dictionary contents\r\n'
    for index, row in enumerate(rows, 1):
        fields = [str(index).rjust(32, 'A'), str(index), '1', '0', '0', '0', '0',
                  row['pattern'], row['replacement'], '']
        for column, item in row.get('fields', {}).items():
            fields[column] = item
        value += '\x03'.join(fields) + '\r\n'
    return value.encode('utf-8')


def rendered(raw, kind, source, **kwargs):
    before = source
    parsed = parse_dictionary(raw, format=kind, **kwargs)
    document = ReadingDocument(before)
    snapshot = ReadingSnapshot.capture(document, (0, 0))
    preview = preview_pronunciations(snapshot, parsed.rules)
    assert document.text == document.source_text == before and document.revision == 0
    return parsed, document, snapshot, preview


def test_dic_order_and_case_use_the_existing_actual_substitution_worker():
    raw = b'cat=short\ncatalog=long\n$CAT=capital\n'
    parsed, document, snapshot, preview = rendered(raw, 'dic', 'CAT catalog cat cats scat.')
    assert [rule.ignore_case for rule in parsed.rules] == [False, True, True]
    assert 'longest pattern' in parsed.notice
    assert preview.after == 'capital long short cats scat.'
    assert apply_pronunciation_preview(document, preview, snapshot)
    assert document.source_text == 'CAT catalog cat cats scat.'
    assert document.undo() and document.text == document.source_text


@pytest.mark.parametrize('pattern,expected', [
    ('cat', 'X cats scat xcatx'),
    ('cat*', 'X Xs scat xcatx'),
    ('*cat', 'X cats sX xcatx'),
    ('*cat*', 'X Xs sX xXx'),
])
def test_dic_edge_templates_change_only_the_matched_literal(pattern, expected):
    _parsed, _document, _snapshot, preview = rendered(
        (pattern + '=X\n').encode(), 'dic', 'cat cats scat xcatx')
    assert preview.after == expected


def test_rex_captures_and_case_are_translated_without_a_new_regex_engine():
    raw = b'(\\d+) kg=$1 kilograms\n@kg=KILOGRAMS\n'
    parsed, document, snapshot, preview = rendered(raw, 'rex', 'Carry 12 KG, and kg.')
    assert parsed.rules[0].replacement == r'\g<1> kilograms'
    assert [rule.ignore_case for rule in parsed.rules] == [True, False]
    assert preview.after == 'Carry 12 kilograms, and KILOGRAMS.'
    assert apply_pronunciation_preview(document, preview, snapshot)
    assert document.source_sha256 == sha256(b'Carry 12 KG, and kg.').hexdigest()


def test_rex_escaped_separator_and_literal_replacement_are_unambiguous():
    _parsed, _doc, _snapshot, preview = rendered(
        br'a\=b=equals $0; $$' + b'\n', 'rex', 'a=b')
    assert preview.after == 'equals a=b; $'


def test_bxd_ordinal_is_not_pattern_type_and_file_order_stays_authoritative():
    raw = bxd([
        {'pattern': 'A', 'replacement': 'B', 'fields': {1: '18', 3: '1'}},
        {'pattern': '(B)', 'replacement': '$1C', 'fields': {1: '2', 4: '1'}},
        {'pattern': 'never', 'replacement': 'activated', 'fields': {2: '0'}},
    ])
    parsed, document, _snapshot, preview = rendered(raw, 'bxd', 'A never')
    assert len(parsed.rules) == 2 and parsed.disabled == 1
    assert parsed.rules[0].regex is True and parsed.rules[1].regex is True
    assert parsed.rules[0].pattern == r'(?<![^\W_])A(?![^\W_])'
    assert parsed.rules[1].pattern == '(B)'
    assert preview.after == 'BC never'
    assert document.text == 'A never'
    assert 'disabled BXD rules omitted' in parsed.notice and 'file order retained' in parsed.notice


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig', 'utf-16', 'utf-16-be'])
def test_bom_declared_unicode_is_preserved_without_encoding_guessing(encoding):
    text = 'Київ=Кийів\n'
    raw = text.encode(encoding)
    if encoding == 'utf-16-be':
        raw = b'\xfe\xff' + raw
    parsed = parse_dictionary(raw, format='dic')
    assert 'Київ' in parsed.rules[0].pattern and parsed.rules[0].replacement == 'Кийів'


def test_legacy_codepage_requires_an_explicit_choice():
    raw = 'Київ=Кийів\n'.encode('cp1251')
    with pytest.raises(ValueError, match='encoding'):
        parse_dictionary(raw, format='dic')
    parsed = parse_dictionary(raw, format='dic', encoding='cp1251')
    assert parsed.rules[0].replacement == 'Кийів'
    with pytest.raises(ValueError, match='conflicts'):
        parse_dictionary(b'\xef\xbb\xbfcat=X\n', format='dic', encoding='cp1251')


@pytest.mark.parametrize('kind,raw,message', [
    ('dic', b'word-list-without-replacements\n', 'unescaped ='),
    ('dic', b'1000\nword\n', 'unescaped ='),
    ('dic', b'ca*t=X\n', 'edge'),
    ('rex', br'(?P<code>cat)=$1' + b'\n', 'manual review'),
    ('rex', br'(cat)=$2' + b'\n', 'missing'),
    ('rex', br'cat=\U$0' + b'\n', 'manual review'),
    ('rex', br'\1=octal-or-backreference' + b'\n', 'hexadecimal'),
    ('rex', br'\x{D800}=partial-surrogate' + b'\n', 'complete Unicode'),
    ('dic', b'cat=X\0\n', 'NUL'),
    ('dic', b'\xff\xfe\x00\x00c\x00\x00\x00', 'UTF-32'),
    ('txt', b'cat=X\n', 'supported'),
])
def test_unestablished_or_malformed_syntax_is_an_actionable_atomic_refusal(kind, raw, message):
    with pytest.raises(ValueError, match=message):
        parse_dictionary(raw, format=kind)


@pytest.mark.parametrize('mutation', ['version', 'delimiter', 'boundary', 'flag', 'duplicate', 'extra'])
def test_bxd_unknown_fields_are_never_silently_reinterpreted(mutation):
    raw = bxd([{'pattern': 'cat', 'replacement': 'X'}])
    if mutation == 'version':
        raw = raw.replace(b'Version=1', b'Version=2')
    elif mutation == 'delimiter':
        raw = raw.replace(b'Delimiter=\x03', b'Delimiter=|')
    elif mutation == 'boundary':
        raw = bxd([{'pattern': 'cat', 'replacement': 'X', 'fields': {5: '4'}}])
    elif mutation == 'flag':
        raw = bxd([{'pattern': 'cat', 'replacement': 'X', 'fields': {4: '3'}}])
    elif mutation == 'duplicate':
        raw = bxd([{'pattern': 'cat', 'replacement': 'X'},
                   {'pattern': 'dog', 'replacement': 'Y', 'fields': {1: '1'}}])
    else:
        raw = raw.rstrip(b'\r\n') + b'\x03extra\n'
    with pytest.raises(ValueError, match='BXD dictionary line'):
        parse_dictionary(raw, format='bxd')


def test_all_dictionary_limits_and_cancel_remain_prepublication():
    for value, message in [
        (b'cat=X\n' * 1001, '1000'),
        (b'# comment\n' * 4097, '4096'),
        (b'c' * 16385 + b'=X\n', '16384'),
        (b'x' * 8_000_001, '8,000,000'),
    ]:
        with pytest.raises(ValueError, match=message):
            parse_dictionary(value, format='dic')
    visited = []
    def cancelled():
        visited.append(True)
        return len(visited) >= 5
    with pytest.raises(InterruptedError):
        parse_dictionary(b'cat=X\n' * 10, format='dic', cancelled=cancelled)
    assert len(visited) == 5


def test_foreign_import_remains_preview_only_then_uses_existing_dictionary_backup(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Foreign dictionary preview')
    source = app.add_text(workspace, 'Original', 'Keep the cat.')
    source_path = app.assets.resolve(source['asset'])
    original = source_path.read_bytes()
    selected = tmp_path / 'reviewed.dic'
    dictionary_bytes = b'cat=kitten\n'
    selected.write_bytes(dictionary_bytes)
    notes = []
    rules, captured = read_dictionary(selected, report=notes.append)
    assert captured == sha256(dictionary_bytes).hexdigest() and notes
    store = DictionaryStore(app.root)
    assert store.load() == ((), None)
    assert selected.read_bytes() == dictionary_bytes and source_path.read_bytes() == original
    token = store.save(rules, expected=None)
    own = store.path.read_bytes()
    assert import_dictionary(store.path) == rules
    assert json.loads(own)['schema'] == 'carr.pronunciations.v1'
    import subprocess
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs:
        pytest.fail('Passive dictionary backup or restore activated a worker.'))
    archive = tmp_path / 'backup'
    manifest = app.backup(archive)
    shutil.rmtree(app.root)
    restored = tmp_path / 'restored'
    restore_backup(archive, restored, expected_manifest_sha256=manifest)
    assert DictionaryStore(restored).load() == (rules, token)
    assert DictionaryStore(restored).path.read_bytes() == own
    assert LibraryApp(restored).assets.resolve(source['asset']).read_bytes() == original
    assert selected.read_bytes() == dictionary_bytes


def test_unsupported_foreign_import_preserves_current_saved_rules_and_original(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    store = DictionaryStore(app.root)
    old = (PronunciationRule('old', 'retained'),)
    store.save(old, expected=None)
    previous = store.path.read_bytes()
    source = tmp_path / 'bad.bxd'
    raw = bxd([{'pattern': 'cat', 'replacement': 'X', 'fields': {5: '4'}}])
    source.write_bytes(raw)
    with pytest.raises(ValueError, match='boundary'):
        read_dictionary(source)
    assert store.path.read_bytes() == previous and source.read_bytes() == raw


@pytest.mark.parametrize('rule,text,expected', [
    ('**=STAR', '*', 'STAR'),
    ('$$=DOLLAR', '$', 'DOLLAR'),
    ('===EQUAL', '=', 'EQUAL'),
    ('##=HASH', '#', 'HASH'),
])
def test_documented_dic_doubled_special_symbols_are_literal_patterns(rule, text, expected):
    _parsed, _document, _snapshot, preview = rendered(
        ('# ordinary comment\n' + rule + '\n').encode(), 'dic', text)
    assert preview.after == expected


def test_existing_canonical_json_import_keeps_its_caller_selected_filename(tmp_path):
    from carr_library.speech.reading import export_dictionary
    selected = tmp_path / 'reviewed.rules'
    rules = (PronunciationRule('retained', 'existing JSON'),)
    export_dictionary(rules, selected)
    before = selected.read_bytes()
    imported, token = read_dictionary(selected)
    assert imported == rules and token == sha256(before).hexdigest()
    assert selected.read_bytes() == before


# Independent author-tool observations from our own synthetic inputs. The
# executable/help is not redistributed and is never launched by these tests.
_AUTHOR = json.loads((Path(__file__).with_name('fixtures')
    / 'pronunciation_author_observations.json').read_text(encoding='utf-8'))
_AUTHOR_REFUSALS = {
    'bxd-field-2-2': 'unrecognized enable, case or pattern flags',
    'bxd-field-3-2': 'unrecognized enable, case or pattern flags',
    'bxd-field-6-1': 'phoneme output',
    'bxd-field-6-2': 'phoneme output',
    'bxd-field-6-3': 'unrecognized BXD output mode',
    'bxd-boundaries-0-1': 'phoneme output',
    'bxd-boundaries-1-1': 'phoneme output',
    'rex-39': 'manual review',
}


def test_author_observation_identity_and_file_ending_are_explicit():
    assert _AUTHOR['source']['commit'] == 'ea8eff0f415d4352f6b66956ed9a118239e419c5'
    assert _AUTHOR['source']['run_id'] == 46020971597
    assert _AUTHOR['selection']['archive_sha256'] == (
        '1e4954fb8a934a53d8228219fd457336e18fed1fb463b4f04c12e71346359e04')
    assert _AUTHOR['selection']['executable_sha256'] == (
        'e9985d73f18d6d6a3c3a4a47dc833aa5149e274e6cab3437db0c2490dceac0e1')
    assert _AUTHOR['selection']['archive_member'] == 'blb2txt.exe'
    assert len(_AUTHOR['cases']) == 42
    baseline = _AUTHOR['cases'][0]
    assert baseline['case'] == 'baseline' and baseline['kind'] is None
    assert baseline['output'] == baseline['input'] + '\r\n'
    assert all(row['exit_code'] == 0 and row['stderr'] == '' for row in _AUTHOR['cases'])
    assert len({row['case'] for row in _AUTHOR['cases']}) == len(_AUTHOR['cases'])


@pytest.mark.parametrize('row', [row for row in _AUTHOR['cases'] if row['kind'] is not None],
                         ids=lambda row: 'author-' + row['case'])
def test_observed_author_dictionary_cases_match_or_are_explicitly_refused(row):
    raw = row['dictionary'].encode('utf-8')
    if row['case'] in _AUTHOR_REFUSALS:
        with pytest.raises(ValueError, match=_AUTHOR_REFUSALS[row['case']]):
            parse_dictionary(raw, format=row['kind'])
        return
    if row['case'] == 'bxd-field-2-0':
        parsed = parse_dictionary(raw, format='bxd')
        assert parsed.rules == () and parsed.disabled == 1
        assert row['output'] == row['input'] + '\r\n'
        return
    parsed, document, snapshot, preview = rendered(raw, row['kind'], row['input'])
    # The text utility adds one final file CRLF, proven by the baseline above.
    # Preserve every authored in-document CRLF and all other exact characters.
    assert row['output'].endswith('\r\n')
    assert preview.after == row['output'][:-2]
    assert document.text == document.source_text == row['input']
    assert document.revision == 0 and document._signature() == snapshot.signature
    assert parsed.rules and parsed.format == row['kind']
