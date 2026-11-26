"""Narration remains a reversible derivative, never silently corrected evidence."""
from copy import deepcopy
import json

import pytest

from carr_library.reader.narration_text import NarrationDraft, text_digest
from carr_library.reader import narration_text


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


def test_large_literal_batch_is_linear_one_undo_and_roundtrips_exact_unchanged_spans():
    source = 'ab ' * 20000 + 'End.\r\n'
    draft = NarrationDraft(source, source_id='synthetic-repeated-text')
    assert draft.replace_literal('ab', 'ab!') == 20000
    assert draft.text == source.replace('ab', 'ab!')
    assert draft.source_text == source and draft.source_sha256 == text_digest(source)
    assert len(draft.edits) == 1 and len(draft._states) == 2
    assert sum(len(state) for state in draft._states) <= 40002
    # Replacement text containing the needle is not searched again, and the
    # spaces between replacements remain individually exact source spans.
    assert draft.source_range(3, 4) == (2, 3, True)
    assert draft.source_range(40003, 40004) == (30002, 30003, True)
    payload = draft.to_dict()
    assert payload['version'] == 2 and len(payload['edits']) == 1
    assert len(json.dumps(payload)) < 2 * len(source)
    restored = NarrationDraft.from_dict(payload, source_text=source)
    assert restored.text == draft.text and restored.spans() == draft.spans()
    assert restored.undo() and restored.text == source and not restored.can_undo
    assert restored.redo() and restored.text == draft.text
    assert restored.spans() == draft.spans()


def test_batch_after_prior_derived_spans_preserves_honest_mapping_and_deletion():
    draft = NarrationDraft('one middle one tail')
    draft.replace_literal('middle', 'one derived')
    assert draft.replace_literal('one', '1') == 3
    assert draft.text == '1 1 derived 1 tail'
    assert draft.source_range(2, 3) == (4, 10, False)
    tail = draft.text.index('tail')
    assert draft.source_range(tail, tail + 4) == (15, 19, True)
    assert draft.undo() and draft.text == 'one one derived one tail'
    assert draft.replace_literal('one', '') == 3
    assert draft.text == '  derived  tail'
    roundtrip = NarrationDraft.from_dict(draft.to_dict())
    assert roundtrip.spans() == draft.spans()


def test_cancelled_or_oversized_batch_preserves_text_history_and_redo(monkeypatch):
    draft = NarrationDraft('ab ' * 3000)
    draft.edit_text(draft.text + 'Extra')
    draft.undo()
    original = draft.to_dict()
    calls = 0
    def cancel_during_work():
        nonlocal calls
        calls += 1
        return calls >= 10
    with pytest.raises(InterruptedError, match='cancelled'):
        draft.replace_literal('ab', 'cd', cancelled=cancel_during_work)
    assert calls >= 10 and draft.to_dict() == original and draft.can_redo
    monkeypatch.setattr(narration_text, 'MAX_REPLACEMENTS', 10)
    with pytest.raises(ValueError, match='match budget'):
        draft.replace_literal('ab', 'cd')
    assert draft.to_dict() == original and draft.can_redo


def test_expansion_and_replay_budgets_reject_atomically(monkeypatch):
    draft = NarrationDraft('a a a a')
    original = draft.to_dict()
    monkeypatch.setattr(narration_text, 'MAX_TEXT_BYTES', 20)
    with pytest.raises(ValueError, match='text budget'):
        draft.replace_literal('a', 'x' * 10)
    assert draft.to_dict() == original
    monkeypatch.setattr(narration_text, 'MAX_REPLAY_BYTES', 5)
    with pytest.raises(ValueError, match='history'):
        draft.replace_literal('a', 'x')
    assert draft.to_dict() == original


def test_legacy_range_history_remains_readable_and_batch_history_is_checked():
    draft = NarrationDraft('One sentence.')
    draft.replace_literal('One', 'First')
    legacy = draft.to_dict()
    legacy['version'] = 1
    assert NarrationDraft.from_dict(legacy).text == 'First sentence.'
    batch = NarrationDraft('one one')
    batch.replace_literal('one', 'two')
    payload = batch.to_dict()
    for field, value in [('matches', True), ('matches', 3), ('needle', 'absent'),
                         ('before_sha256', '0' * 64), ('after_sha256', '0' * 64)]:
        forged = deepcopy(payload)
        forged['edits'][0][field] = value
        with pytest.raises(ValueError):
            NarrationDraft.from_dict(forged)
