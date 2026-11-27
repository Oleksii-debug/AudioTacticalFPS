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
    assert parsed.rules[0].regex is False and parsed.rules[1].regex is True
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
        raw = bxd([{'pattern': 'cat', 'replacement': 'X', 'fields': {5: '3'}}])
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
    raw = bxd([{'pattern': 'cat', 'replacement': 'X', 'fields': {6: '3'}}])
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
