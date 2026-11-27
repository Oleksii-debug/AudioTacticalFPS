"""Actual bounded search worker and source-preserving reading edits."""
import json
from pathlib import Path
from threading import Event, Timer
import subprocess
import sys

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.ui.find_replace_dialog import (FindQuery, ReadingSnapshot, apply_replacement,
    find_matches, next_match, preview_replacement)


def snapshot(document, selection=(0, 0)):
    return ReadingSnapshot.capture(document, selection)


def test_actual_literal_unicode_crlf_selection_undo_and_saved_recovery(tmp_path):
    source = '😀 Source.\r\nDr. Один; Dr. Два.\r\nDr. outside.'
    doc = ReadingDocument(source)
    start, end = source.index('Dr.'), source.rindex('\r\n')
    captured = snapshot(doc, (start, end))
    found = find_matches(captured, FindQuery('Dr.', 'Доктор'), bounds=(start, end))
    assert len(found.matches) == 2
    preview = preview_replacement(found)
    assert preview.before == source[start:end]
    assert preview.after == 'Доктор Один; Доктор Два.'
    assert apply_replacement(doc, preview, captured)
    assert doc.revision == 1 and doc.source_text == source
    assert doc.text == '😀 Source.\r\nДоктор Один; Доктор Два.\r\nDr. outside.'
    assert doc.source_span(0, start).exact
    assert not doc.source_span(start, start + len('Доктор')).exact
    target = tmp_path / 'reviewed.json'
    doc.save(target)
    recovered = ReadingDocument.load(target)
    assert recovered.text == doc.text and recovered.source_text == source
    assert recovered.undo() and recovered.text == source
    assert recovered.redo() and recovered.text == doc.text


def test_literal_replacement_keeps_backslashes_and_whitespace_patterns():
    doc = ReadingDocument('A  B  C')
    captured = snapshot(doc)
    found = find_matches(captured, FindQuery('  ', r'\1\path'))
    preview = preview_replacement(found)
    apply_replacement(doc, preview, captured)
    assert doc.text == r'A\1\pathB\1\pathC'


def test_actual_regex_groups_case_and_single_match():
    doc = ReadingDocument('Mass 12 KG, mass 7 kg.')
    captured = snapshot(doc, (5, 10))
    result = find_matches(captured, FindQuery(r'(\d+) kg', r'\1 kilograms', True, True))
    assert result.matches == ((5, 10, '12 kilograms'), (17, 21, '7 kilograms'))
    preview = preview_replacement(result, index=0)
    apply_replacement(doc, preview, captured)
    assert doc.text == 'Mass 12 kilograms, mass 7 kg.'
    assert doc.undo() and doc.text == doc.source_text


def test_forward_reverse_wrap_and_zero_width_matches_advance():
    doc = ReadingDocument('😀A\r\nB')
    result = find_matches(snapshot(doc), FindQuery(r'(?=A)|(?=B)|$', regex=True))
    assert [row[:2] for row in result.matches] == [(1, 1), (4, 4), (5, 5)]
    assert next_match(result, (0, 0)) == (0, False)
    assert next_match(result, (1, 1), previous=0) == (1, False)
    assert next_match(result, (5, 5), previous=2) == (0, True)
    assert next_match(result, (1, 1), reverse=True, previous=0) == (2, True)
    assert next_match(result, (4, 4), reverse=True) == (1, False)


@pytest.mark.parametrize('changed', ['selection', 'content', 'identity', 'undo_branch', 'voice'])
def test_stale_preview_cannot_change_source_or_new_reading_state(changed):
    doc = ReadingDocument('one two one')
    captured = snapshot(doc)
    preview = preview_replacement(find_matches(captured, FindQuery('one', 'ONE')))
    if changed == 'selection':
        current = snapshot(doc, (0, 3))
    elif changed == 'content':
        doc.replace(0, 3, 'changed'); current = snapshot(doc)
    elif changed == 'identity':
        doc = ReadingDocument(doc.text); current = snapshot(doc)
    elif changed == 'undo_branch':
        doc.replace(0, 3, 'changed'); doc.undo(); current = snapshot(doc)
    else:
        current = ReadingSnapshot.capture(doc, (0, 0), voice_id='changed:voice')
    before = doc.to_dict()
    with pytest.raises(ValueError, match='changed'):
        apply_replacement(doc, preview, current)
    assert doc.to_dict() == before
    assert doc.source_text == 'one two one'


@pytest.mark.parametrize('query', [FindQuery('[', regex=True), FindQuery('(one)', r'\2', regex=True),
                                  FindQuery('x', 'y')])
def test_invalid_expression_group_and_no_matches_leave_copy_unchanged(query):
    doc = ReadingDocument('one two')
    before = doc.to_dict()
    with pytest.raises(ValueError):
        preview_replacement(find_matches(snapshot(doc), query))
    assert doc.to_dict() == before


def test_match_and_expansion_limits_are_enforced_by_actual_worker():
    doc = ReadingDocument('a' * 10001)
    with pytest.raises(ValueError, match='smaller'):
        find_matches(snapshot(doc), FindQuery('a', 'b'))
    with pytest.raises(ValueError, match='smaller'):
        find_matches(snapshot(ReadingDocument('a' * 1000)), FindQuery('a', 'b' * 4096))
    assert doc.text == doc.source_text


@pytest.mark.parametrize('cancel', [False, True])
def test_actual_catastrophic_regex_is_stopped_and_owned_child_reaped(monkeypatch, cancel):
    from carr_library.speech import _io
    children, stopped = [], Event()
    actual = subprocess.Popen
    def launch(*args, **kwargs):
        process = actual(*args, **kwargs); children.append(process); return process
    class ProcessFacade:
        Popen = staticmethod(launch)
        def __getattr__(self, name):
            return getattr(subprocess, name)
    monkeypatch.setattr(_io, 'subprocess', ProcessFacade())
    doc = ReadingDocument('a' * 1500 + '!')
    timer = Timer(.15, stopped.set)
    if cancel:
        timer.start()
    try:
        with pytest.raises(InterruptedError if cancel else TimeoutError):
            find_matches(snapshot(doc), FindQuery('(a+)+$', regex=True),
                         cancelled=stopped.is_set, timeout=1 if cancel else .2)
    finally:
        if cancel:
            timer.cancel(); timer.join()
    assert children and all(process.poll() is not None for process in children)
    assert doc.text == doc.source_text and not doc.can_undo


def test_cancel_before_start_does_not_spawn_worker(monkeypatch):
    from carr_library.speech import _io
    class ProcessFacade:
        def Popen(self, *args, **kwargs):
            pytest.fail('Unexpected worker')
        def __getattr__(self, name):
            return getattr(subprocess, name)
    monkeypatch.setattr(_io, 'subprocess', ProcessFacade())
    with pytest.raises(InterruptedError):
        find_matches(snapshot(ReadingDocument('abc')), FindQuery('a'), cancelled=lambda: True)


@pytest.mark.parametrize('bounds', [(0, 4), (-1, 2), (2, 1), (True, 2), [0, 1]])
def test_invalid_scope_rejected_before_matching(bounds):
    with pytest.raises(ValueError, match='scope'):
        find_matches(snapshot(ReadingDocument('abc')), FindQuery('a'), bounds=bounds)


def test_no_change_preview_does_not_create_edit():
    doc = ReadingDocument('abc')
    captured = snapshot(doc)
    preview = preview_replacement(find_matches(captured, FindQuery('a', 'a')))
    with pytest.raises(ValueError, match='no reading-text changes'):
        apply_replacement(doc, preview, captured)
    assert not doc.can_undo


@pytest.mark.parametrize('encoding', ['cp1252', 'ascii'])
def test_actual_regex_worker_uses_utf8_with_legacy_redirected_stdout(encoding):
    from carr_library.speech import _io, regex_worker
    worker = Path(regex_worker.__file__).resolve()
    script = ('import io,runpy,sys; '
              f'sys.stdout=io.TextIOWrapper(sys.stdout.buffer,encoding={encoding!r},errors="strict"); '
              'runpy.run_path(sys.argv[1],run_name="__main__")')
    payload = json.dumps({'text': 'Dr. Один', 'pattern': r'Dr\.', 'replacement': 'Доктор 😀'},
                         ensure_ascii=False).encode('utf-8')
    reply, _, code = _io.run_process([sys.executable, '-I', '-B', '-S', '-c', script, str(worker)],
                                    input_bytes=payload, timeout=3)
    assert code == 0
    assert json.loads(reply.decode('utf-8')) == {'matches': [[0, 3, 'Доктор 😀']]}
