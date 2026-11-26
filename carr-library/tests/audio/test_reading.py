"""Narration edits must never gain original-quotation authority."""
import copy
import hashlib

import pytest

from carr_library.audio.reading import ReadingDerivative


def test_unicode_edit_preserves_original_and_maps_only_retained_spans():
    original = 'Вітаю, Олексію. Dobrý deň!'
    document = ReadingDerivative(original)
    start = original.index('Dobrý')
    assert document.replace(start, start + 5, 'DO-brý')
    assert document.source == original
    assert document.source_sha256 == hashlib.sha256(original.encode()).hexdigest()
    assert document.render('original') == original
    assert document.render('enhanced') == original[:start] + 'DO-brý deň!'
    assert document.source_span(0, start) == (0, start)
    assert document.source_span(start, start + 6) is None
    assert document.source_span(start + 6, len(document.text)) == (start + 5, len(original))
    change, = document.diff()
    assert (change.original, change.replacement) == ('Dobrý', 'DO-brý')
    with pytest.raises(AttributeError):
        document.source = 'silent source rewrite'


def test_literal_dictionary_is_one_undoable_change_and_never_regex():
    document = ReadingDerivative('A .* B .* C [x]')
    assert document.substitute('.*', 'dot star') == 2
    assert document.text == 'A dot star B dot star C [x]'
    assert document.undo()
    assert document.text == document.source
    assert document.redo()
    assert document.text == 'A dot star B dot star C [x]'
    assert document.revision == 3


def test_insert_delete_and_disconnected_spans_do_not_fabricate_origin():
    document = ReadingDerivative('one two three')
    document.replace(4, 8, '')
    assert document.text == 'one three'
    assert document.source_span(0, len(document.text)) is None
    assert document.source_span(4, 9) == (8, 13)
    document.replace(0, 0, 'Read: ')
    assert document.source_span(0, 6) is None
    assert document.source_span(6, 9) == (0, 3)
    changes = document.diff()
    assert [(change.original, change.replacement) for change in changes] == [('', 'Read: '), ('two ', '')]


def test_history_reopens_with_undo_redo_and_new_edit_replaces_redo_branch():
    document = ReadingDerivative('Alpha beta gamma')
    document.substitute('beta', 'BETA')
    document.substitute('gamma', 'GAMMA')
    document.undo()
    reopened = ReadingDerivative.from_dict(document.to_dict())
    assert reopened.text == 'Alpha BETA gamma'
    assert reopened.can_redo and reopened.can_undo
    assert reopened.redo()
    assert reopened.text == 'Alpha BETA GAMMA'
    reopened.undo()
    reopened.substitute('Alpha', 'ALPHA')
    assert not reopened.can_redo
    assert reopened.source == 'Alpha beta gamma'
    assert reopened.revision > document.revision
    assert reopened.reset()
    assert reopened.text == reopened.source
    assert reopened.undo()
    assert reopened.text == 'ALPHA BETA gamma'


def test_replacement_result_can_never_be_exported_as_original_offsets():
    document = ReadingDerivative('X ab X')
    document.replace(0, 1, 'ab')
    assert document.source_span(0, 2) is None
    assert document.source_span(3, 5) == (2, 4)
    assert document.source_span(0, 5) is None


@pytest.mark.parametrize('change', [
    lambda d: d.update(source='tampered'),
    lambda d: d.update(source_sha256='0' * 64),
    lambda d: d.update(schema=True),
    lambda d: d.update(cursor=True),
    lambda d: d.update(revision=-1),
    lambda d: d.update(unexpected='hidden action'),
    lambda d: d['history'].append([{'start': 4, 'end': 5}, {'start': 0, 'end': 2}]),
    lambda d: d['history'].append([{'start': 0, 'end': 4}, {'start': 2, 'end': 5}]),
    lambda d: d['history'].__setitem__(0, [{'inserted': 'abcde'}]),
    lambda d: d['history'].append([{'start': False, 'end': 2}]),
])
def test_tampered_or_ambiguous_history_is_rejected(change):
    value = ReadingDerivative('abcde').to_dict()
    change(value)
    with pytest.raises(ValueError):
        ReadingDerivative.from_dict(value)


@pytest.mark.parametrize('start,end,replacement', [
    (-1, 2, 'x'), (0, 10, 'x'), (3, 1, 'x'), (True, 2, 'x'),
    (0, 1, '\ud800'), (0, 1, None),
])
def test_invalid_edits_leave_the_previous_draft_unchanged(start, end, replacement):
    document = ReadingDerivative('abcde')
    before = copy.deepcopy(document.to_dict())
    with pytest.raises(ValueError):
        document.replace(start, end, replacement)
    assert document.to_dict() == before


def test_resource_failure_preserves_valid_draft_and_undo_history():
    document = ReadingDerivative('aaa', max_text_chars=10, max_history=2)
    with pytest.raises(ValueError, match='text budget'):
        document.substitute('a', 'longer')
    assert document.text == 'aaa' and not document.can_undo
    document.replace(0, 1, 'A')
    before = document.to_dict()
    with pytest.raises(ValueError, match='history budget'):
        document.replace(1, 2, 'A')
    assert document.to_dict() == before
    assert document.undo()
    assert document.text == 'aaa'


def test_sparse_mapping_for_a_large_source_and_many_literal_matches():
    source = 'alpha beta\n' * 20000
    document = ReadingDerivative(source)
    assert len(document.source_offsets) == 1
    assert document.substitute('alpha', 'A') == 20000
    assert document.text == source.replace('alpha', 'A')
    assert document.source == source
    assert document.source_span(1, 6) == (5, 10)
    assert ReadingDerivative.from_dict(document.to_dict()).text == document.text


def test_empty_document_insertion_and_reset_roundtrip():
    document = ReadingDerivative('')
    assert document.source_offsets == () and document.diff() == ()
    assert document.replace(0, 0, 'A newly authored narration.')
    assert document.source_span(0, len(document.text)) is None
    assert document.diff()[0].original == ''
    assert ReadingDerivative.from_dict(document.to_dict()).text == document.text
    assert document.reset()
    assert document.text == ''


def test_noop_and_count_controls_preserve_revision():
    document = ReadingDerivative('one one one')
    assert not document.replace(0, 3, 'one')
    assert document.substitute('one', 'one') == 0
    assert document.substitute('one', '1', 0) == 0
    assert document.revision == 0
    assert document.substitute('one', '1', 2) == 2
    assert document.text == '1 1 one'
    with pytest.raises(ValueError):
        document.substitute('', 'x')
    with pytest.raises(ValueError):
        document.substitute('one', '1', True)
