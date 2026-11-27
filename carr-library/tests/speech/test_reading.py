"""Original text, source mapping and durable narration-copy regression tests."""
from hashlib import sha256
import json
from pathlib import Path
import time

import pytest

from carr_library.speech import (ReadingDocument, PronunciationRule, SourceSpan,
                                 export_dictionary, import_dictionary)


def _bound_project():
    from carr_library.speech.source import NarrationSource
    text = 'ABC\n\nDEF'
    source = NarrationSource(text, 'Source', 'library', 'edition', 'revision', (
        {'ordinal': 1, 'kind': 'physical_page', 'index': 3, 'start': 0, 'end': 3,
         'text_sha256': sha256(b'ABC').hexdigest(), 'text_verified_at_read': False},
        {'ordinal': 2, 'kind': 'physical_page', 'index': 5, 'start': 5, 'end': 8,
         'text_sha256': sha256(b'DEF').hexdigest(), 'text_verified_at_read': True}), (), 'a' * 64)
    return ReadingDocument.from_narration_source(source)


def test_legacy_v1_copy_remains_readable_and_explicitly_unbound():
    value = {'schema': 'carr.reading-copy.v1', 'title': 'Legacy reading copy',
             'source_text': 'ABC', 'source_sha256': sha256(b'ABC').hexdigest(),
             'edits': [{'start': 1, 'end': 2, 'replacement': 'X', 'label': 'Legacy edit'}],
             'revision': 1}
    legacy = ReadingDocument.from_dict(value)
    assert legacy.text == 'AXC' and legacy.source_anchor is None
    assert legacy.to_dict() == value
    assert legacy.undo() and legacy.text == 'ABC'


def test_source_anchor_is_detached_and_does_not_invent_quotation_authority():
    bound = _bound_project()
    anchor = bound.source_anchor
    anchor['workspace'] = 'other'
    anchor['positions'][0]['text_verified_at_read'] = True
    anchor['derived_separators'][0]['start'] = 0
    exported = bound.to_dict()
    exported['source_anchor']['positions'][0]['text_sha256'] = '0' * 64
    assert bound.source_anchor['workspace'] == 'library'
    assert bound.source_anchor['positions'][0]['text_verified_at_read'] is False
    assert bound.source_anchor['derived_separators'] == [{'start': 3, 'end': 5}]
    assert bound.source_anchor['quotation_authority'] is False
    assert ReadingDocument.from_dict(bound.to_dict()).source_anchor == bound.source_anchor
    assert bound.source_span(0, 3) == SourceSpan(0, 3, True)
    assert bound.source_span(3, 5) == SourceSpan(None, None, False)


@pytest.mark.parametrize('path,value', [
    (('workspace',), ''), (('workspace',), 'other\nworkspace'),
    (('asset_sha256',), 'not-a-hash'), (('quotation_authority',), True),
    (('quotation_authority',), 0), (('positions',), []),
    (('positions', 0, 'start'), True), (('positions', 0, 'end'), 4),
    (('positions', 0, 'ordinal'), False), (('positions', 1, 'ordinal'), 4),
    (('positions', 1, 'kind'), 'logical_segment'), (('positions', 1, 'index'), 3),
    (('positions', 0, 'text_sha256'), '0' * 64),
    (('positions', 0, 'text_verified_at_read'), 'true'),
    (('derived_separators',), []), (('derived_separators', 0, 'start'), False),
])
def test_damaged_or_authoritative_source_anchor_is_rejected(path, value):
    data = _bound_project().to_dict()
    target = data['source_anchor']
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        ReadingDocument.from_dict(data)


@pytest.mark.parametrize('change', ['v1-with-anchor', 'v2-without-anchor', 'v2-null-anchor'])
def test_reading_schema_never_silently_discards_source_binding(change):
    data = _bound_project().to_dict()
    if change == 'v1-with-anchor':
        data['schema'] = 'carr.reading-copy.v1'
    elif change == 'v2-without-anchor':
        del data['source_anchor']
    else:
        data['source_anchor'] = None
    with pytest.raises(ValueError):
        ReadingDocument.from_dict(data)


def test_edit_never_changes_raw_source_and_mapping_is_conservative():
    raw = 'A raw line.\r\nІще інший рядок.\nمرحبا'
    copy = ReadingDocument(raw)
    assert copy.source_span(0, 10) == SourceSpan(0, 10, True)
    copy.replace(2, 5, 'corrected')
    assert copy.source_text == raw
    assert copy.source_sha256 == sha256(raw.encode()).hexdigest()
    assert copy.source_span(2, 11) == SourceSpan(2, 5, False)
    assert copy.source_span(11, 16) == SourceSpan(5, 10, True)
    assert copy.source_span(0, 16) == SourceSpan(0, 10, False)
    assert copy.undo() and copy.text == raw
    assert copy.source_span(0, 10).exact
    assert copy.redo() and 'corrected' in copy.text


def test_insert_has_no_invented_source_offset():
    copy = ReadingDocument('ABCD')
    copy.replace(2, 2, 'xyz')
    assert copy.text == 'ABxyzCD'
    assert copy.source_span(2, 5) == SourceSpan(None, None, False)
    assert copy.source_span(0, 7) == SourceSpan(0, 4, False)
    copy.replace(3, 4, '123')
    assert copy.source_span(3, 6) == SourceSpan(None, None, False)


def test_delete_does_not_rejoin_noncontiguous_source_as_exact():
    copy = ReadingDocument('ABCDE')
    copy.replace(2, 3, '')
    assert copy.text == 'ABDE'
    assert copy.source_span(0, 4) == SourceSpan(0, 5, False)
    assert copy.source_span(2, 4) == SourceSpan(3, 5, True)


def test_redo_is_invalidated_by_new_edit():
    copy = ReadingDocument('ABC')
    copy.replace(0, 1, 'X')
    copy.undo()
    copy.replace(1, 2, 'Y')
    assert not copy.can_redo
    assert copy.text == 'AYC'


def test_set_text_preserves_prefix_suffix_without_guessing_changed_offsets():
    copy = ReadingDocument('prefix middle suffix')
    copy.set_text('prefix replacement suffix')
    assert copy.source_span(0, 7) == SourceSpan(0, 7, True)
    assert not copy.source_span(7, 18).exact
    assert copy.source_span(18, 25) == SourceSpan(13, 20, True)


def test_reading_project_roundtrip_keeps_undo_and_redo(tmp_path):
    copy = ReadingDocument('Original\r\nукраїнський текст.', title='Книжка')
    copy.replace(0, 8, 'Narrated')
    copy.replace(8, 8, '!')
    copy.undo()
    path = tmp_path / 'Книжка.reading.json'
    checksum = copy.save(path)
    assert checksum == sha256(path.read_bytes()).hexdigest()
    reopened = ReadingDocument.load(path)
    assert reopened.source_text == copy.source_text
    assert reopened.text == copy.text
    assert reopened.can_redo and reopened.redo()
    assert reopened.text == 'Narrated!\r\nукраїнський текст.'
    assert not copy.dirty
    reopened.save(path)
    assert not reopened.dirty


def test_project_tampered_source_or_edit_rejected(tmp_path):
    copy = ReadingDocument('ABC')
    copy.replace(1, 2, 'X')
    path = tmp_path / 'project.json'
    copy.save(path)
    raw = json.loads(path.read_text())
    raw['source_text'] = 'other'
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='checksum'):
        ReadingDocument.load(path)
    raw['source_text'] = 'ABC'
    raw['edits'][0]['start'] = 200
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='range'):
        ReadingDocument.load(path)


def test_to_dict_cannot_mutate_live_edit_history():
    copy = ReadingDocument('ABC')
    copy.replace(1, 2, 'D')
    value = copy.to_dict()
    value['edits'][0]['replacement'] = 'BAD'
    assert copy.to_dict()['edits'][0]['replacement'] == 'D'


def test_save_refuses_immutable_original_and_existing_unrelated_file(tmp_path):
    original = tmp_path / 'source.txt'
    original.write_bytes(b'ABC\r\n')
    copy = ReadingDocument('ABC\r\n', source_path=original)
    copy.replace(0, 1, 'Z')
    with pytest.raises(ValueError, match='original'):
        copy.save(original)
    existing = tmp_path / 'unrelated.json'
    existing.write_bytes(b'preserve me')
    with pytest.raises(FileExistsError):
        copy.save(existing)
    assert existing.read_bytes() == b'preserve me'
    assert original.read_bytes() == b'ABC\r\n'


def test_save_detects_external_modification(tmp_path):
    copy = ReadingDocument('ABC')
    target = tmp_path / 'draft.json'
    copy.save(target)
    other = ReadingDocument.load(target)
    other.replace(0, 1, 'Z')
    other.save(target)
    copy.replace(0, 1, 'Q')
    with pytest.raises(FileExistsError, match='changed'):
        copy.save(target)
    assert ReadingDocument.load(target).text == 'ZBC'


def test_cancelled_save_and_export_do_not_publish(tmp_path):
    copy = ReadingDocument('ABC')
    with pytest.raises(InterruptedError):
        copy.save(tmp_path / 'draft.json', cancelled=lambda: True)
    with pytest.raises(InterruptedError):
        copy.export(tmp_path / 'copy.txt', cancelled=lambda: True)
    assert not list(tmp_path.iterdir())


def test_export_raw_and_derivative_preserves_line_breaks(tmp_path):
    raw = 'One\r\nДва\nThree\rFour'
    copy = ReadingDocument(raw)
    copy.replace(0, 3, 'Changed')
    copy.export(tmp_path / 'original.txt', original=True)
    copy.export(tmp_path / 'narration.txt')
    assert (tmp_path / 'original.txt').read_bytes() == raw.encode('utf-8')
    assert (tmp_path / 'narration.txt').read_bytes() == copy.text.encode('utf-8')


def test_cleanup_is_reversible_narration_only():
    original = 'A  hyphen-\nated\tword.\n\nParagraph.'
    copy = ReadingDocument(original)
    copy.cleanup(join_hyphenated=True)
    assert copy.text == 'A hyphenated word.\n\nParagraph.'
    assert copy.source_text == original
    assert 'Immutable source' in copy.diff()
    copy.reject_changes()
    assert copy.text == original
    assert copy.can_redo


def test_literal_and_bounded_regex_pronunciation_scope():
    copy = ReadingDocument('Dr. Smith, 12 kg. Dr. Jones.')
    copy.apply_pronunciations([
        PronunciationRule('Dr.', 'Doctor', language='en'),
        PronunciationRule(r'(\d+) kg', r'\1 kilograms', regex=True),
        PronunciationRule('Doctor', 'Doktor', language='de'),
    ], language='en')
    assert copy.text == 'Doctor Smith, 12 kilograms. Doctor Jones.'
    assert copy.source_text == 'Dr. Smith, 12 kg. Dr. Jones.'
    copy.undo()
    assert copy.text == copy.source_text


def test_catastrophic_regex_is_killed_and_changes_are_atomic():
    copy = ReadingDocument('a' * 2000 + '!')
    before = copy.text
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        copy.apply_pronunciations([PronunciationRule('!', '?'),
            PronunciationRule('(a+)+$', 'X', regex=True)], timeout=.15)
    assert time.monotonic() - started < 5
    assert copy.text == before and copy.revision == 0


def test_dictionary_roundtrip_no_automatic_execution(tmp_path):
    rules = (PronunciationRule('Kyiv', 'Київ', language='uk'),
             PronunciationRule(r'(\d+) kg', r'\1 kilograms', regex=True, voice_id='voice'))
    path = tmp_path / 'pronunciations.json'
    export_dictionary(rules, path)
    assert import_dictionary(path) == rules
    with pytest.raises(FileExistsError):
        export_dictionary(rules, path)


@pytest.mark.parametrize('raw', ['bad\0text', '\ud800'])
def test_invalid_source_unicode_rejected(raw):
    with pytest.raises(ValueError):
        ReadingDocument(raw)
