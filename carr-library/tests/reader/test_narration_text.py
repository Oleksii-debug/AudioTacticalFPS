"""Narration remains a reversible derivative, never silently corrected evidence."""
from copy import deepcopy
import json

import pytest

from carr_library.reader.narration_text import NarrationDraft, text_digest


def test_explicit_unicode_replacement_preserves_original_and_exact_unchanged_offsets():
    source = 'Словацька мова.\r\nZámok stojí. E=mc². e\u0301.'
    draft = NarrationDraft(source, source_id='edition:1:revision:2')
    before = draft.source_sha256
    assert draft.replace_literal('Zámok', 'За́мок') == 1
    assert draft.source_text == source
    assert draft.source_sha256 == before
    start = source.index('Zámok')
    assert draft.source_range(start, start + 6) == (start, start + 5, False)
    tail = draft.text.index('E=mc²')
    assert draft.source_range(tail, tail + 5) == (source.index('E=mc²'), source.index('E=mc²') + 5, True)
    assert '\r\n' in draft.text and 'e\u0301' in draft.text
    assert all(source[item.source_start:item.source_end] == draft.text[item.reading_start:item.reading_end]
               for item in draft.spans() if item.exact)


def test_literal_metacharacters_are_not_regular_expressions_and_all_is_explicit():
    draft = NarrationDraft('a.b aXb a.b [voice]')
    assert draft.replace_literal('a.b', 'X', all_occurrences=False) == 1
    assert draft.text == 'X aXb a.b [voice]'
    assert draft.replace_literal('[voice]', r'\1; $(not-a-command)') == 1
    assert '$(not-a-command)' in draft.text
    with pytest.raises(ValueError):
        draft.replace_literal('', 'x')


def test_edits_undo_redo_and_new_branch_retain_correct_mapping():
    draft = NarrationDraft('One two three.')
    draft.replace_literal('two', 'second')
    draft.edit_text('First second three.')
    assert draft.undo() and draft.text == 'One second three.'
    assert draft.undo() and draft.text == draft.source_text
    assert draft.redo() and draft.text == 'One second three.'
    assert draft.redo() and draft.text == 'First second three.'
    assert not draft.redo()
    draft.undo()
    draft.replace_literal('three', 'third')
    assert not draft.can_redo
    assert draft.source_range(0, 3) == (0, 3, True)


def test_deletion_and_insertion_do_not_manufacture_contiguous_exact_source():
    draft = NarrationDraft('left middle right')
    draft.replace_literal(' middle', '')
    assert draft.text == 'left right'
    assert draft.source_range(0, len(draft.text)) == (0, len(draft.source_text), False)
    draft.replace_range(4, 4, ' new')
    assert draft.text == 'left new right'
    assert draft.source_range(5, 8) == (4, 4, False)


def test_save_roundtrip_anchors_source_hash_and_rejects_stale_or_forged_history(tmp_path):
    draft = NarrationDraft('Original українська.', source_id='book-42')
    draft.replace_literal('українська', 'украї́нська')
    path = tmp_path / 'читання.json'
    private = tmp_path / 'private'
    draft.save(path, private_root=private, source_text=draft.source_text)
    loaded = NarrationDraft.load(path, source_text=draft.source_text, source_id='book-42')
    assert loaded.text == draft.text and loaded.spans() == draft.spans()
    with pytest.raises(ValueError, match='changed'):
        NarrationDraft.load(path, source_text='New revision.', source_id='book-42')
    with pytest.raises(ValueError, match='different source'):
        NarrationDraft.load(path, source_text=draft.source_text, source_id='other-book')
    payload = draft.to_dict()
    for field, value in [('source_sha256', '0' * 64), ('reading_sha256', '0' * 64)]:
        forged = deepcopy(payload)
        forged[field] = value
        with pytest.raises(ValueError):
            NarrationDraft.from_dict(forged)
    payload['edits'][0]['before'] = 'forged'
    with pytest.raises(ValueError, match='history'):
        NarrationDraft.from_dict(payload)


def test_failed_or_cancelled_save_never_replaces_existing_file(tmp_path):
    path = tmp_path / 'draft.json'
    draft = NarrationDraft('Original')
    draft.save(path, private_root=tmp_path / 'private')
    original = path.read_bytes()
    draft.edit_text('Edited')
    with pytest.raises(FileExistsError):
        draft.save(path, private_root=tmp_path / 'private')
    with pytest.raises(TimeoutError):
        draft.save(path, private_root=tmp_path / 'private', overwrite=True, cancelled=lambda: True)
    with pytest.raises(ValueError, match='changed'):
        draft.save(path, private_root=tmp_path / 'private', overwrite=True, source_text='A new original')
    assert path.read_bytes() == original
    assert not list(tmp_path.glob('.research-export-*'))


def test_saved_draft_rejects_duplicate_fields_and_invalid_mapping_ranges(tmp_path):
    draft = NarrationDraft('abc')
    draft.edit_text('abcd')
    payload = draft.to_dict()
    for start, end in [(-1, 1), (True, 3), (0, 100), (2, 1)]:
        bad = deepcopy(payload)
        bad['edits'][0]['start'], bad['edits'][0]['end'] = start, end
        with pytest.raises(ValueError):
            NarrationDraft.from_dict(bad)
    path = tmp_path / 'duplicate.json'
    path.write_text('{"version":1,"version":2}', encoding='utf-8')
    with pytest.raises(ValueError, match='JSON'):
        NarrationDraft.load(path, source_text='abc')


def test_draft_serialization_never_mutates_source_even_after_nested_rewrites():
    draft = NarrationDraft('Short word.')
    draft.replace_literal('Short', 'Rather long')
    draft.replace_literal('long', 'lengthy')
    saved = json.loads(json.dumps(draft.to_dict(), ensure_ascii=False))
    reopened = NarrationDraft.from_dict(saved, source_text='Short word.')
    assert reopened.source_sha256 == text_digest('Short word.')
    assert reopened.source_range(7, 13) == (0, 5, False)
    assert reopened.text == 'Rather lengthy word.'
