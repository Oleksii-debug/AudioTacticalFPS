"""Real isolated find/replace worker, exact ranges and source-preserving history."""
from dataclasses import replace
from pathlib import Path
from threading import Event, Timer
import time

import pytest

from carr_library.speech.reading import ReadingDocument, PronunciationRule
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui.pronunciation_dialog import ReadingSnapshot
from carr_library.ui import find_replace_dialog as ui


def snapshot(document, selection=(0, 0)):
    return ReadingSnapshot.capture(document, selection)


def test_literal_preview_and_apply_all_are_exact_and_one_undoable_derivative(tmp_path):
    original = '😀 Dr. First.\r\nDr. Second remains.'
    document = ReadingDocument(original)
    before = document.to_dict()
    proposal = ui.preview_find(snapshot(document), PronunciationRule('Dr.', 'Doctor'))
    assert [item.before for item in proposal.matches] == ['Dr.', 'Dr.']
    assert [(item.start, item.end) for item in proposal.matches] == [
        (original.index('Dr.'), original.index('Dr.') + 3),
        (original.rindex('Dr.'), original.rindex('Dr.') + 3)]
    assert proposal.after == original.replace('Dr.', 'Doctor') and document.to_dict() == before
    assert ui.apply_find_preview(document, proposal, snapshot(document))
    assert document.revision == 1 and document.text == proposal.after
    assert document.source_text == original and document.source_span(0, 2).exact
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(document))
    recovered = store.load().document
    assert recovered.source_text == original and recovered.text == proposal.after
    assert recovered.undo() and recovered.text == original
    assert document.undo() and document.text == original


@pytest.mark.parametrize('replacement', [r'\1 $literal', r'c:\new\file', '\\', '\n\n', ''])
def test_literal_replacement_does_not_expand_regex_groups(replacement):
    document = ReadingDocument('one [x] two [x]')
    proposal = ui.preview_find(snapshot(document), PronunciationRule('[x]', replacement))
    assert proposal.after == 'one ' + replacement + ' two ' + replacement
    assert all(item.replacement == replacement for item in proposal.matches)


def test_regex_groups_and_case_flag_use_the_existing_worker():
    document = ReadingDocument('😀 12 kg. Then 3 KG.')
    proposal = ui.preview_find(snapshot(document),
        PronunciationRule(r'(?P<amount>\d+) (?P<unit>kg)', r'\g<amount> kilograms',
                          regex=True, ignore_case=True))
    assert proposal.after == '😀 12 kilograms. Then 3 kilograms.'
    assert [item.before for item in proposal.matches] == ['12 kg', '3 KG']
    assert document.text == document.source_text


def test_selected_scope_cannot_replace_outside_its_reviewed_interval():
    document = ReadingDocument('cat cat cat')
    captured = snapshot(document, (4, 7))
    proposal = ui.preview_find(captured, PronunciationRule('cat', 'dog'), selection_only=True)
    assert (proposal.start, proposal.end) == (4, 7) and proposal.after == 'dog'
    assert len(proposal.matches) == 1
    ui.apply_find_preview(document, proposal, captured)
    assert document.text == 'cat dog cat' and document.source_text == 'cat cat cat'
    assert document.source_span(0, 4).exact and document.source_span(7, 11).exact


def test_replacing_selected_match_leaves_other_equal_text_untouched():
    document = ReadingDocument('same same same')
    proposal = ui.preview_find(snapshot(document), PronunciationRule('same', 'only'))
    item = proposal.matches[1]
    current = snapshot(document, (item.start, item.end))
    proposal = replace(proposal, snapshot=current)
    ui.apply_find_preview(document, proposal, current, index=1)
    assert document.text == 'same only same' and document.revision == 1
    assert document.undo() and document.text == 'same same same'


@pytest.mark.parametrize('change', ['text', 'selection', 'document'])
def test_stale_or_foreign_preview_cannot_change_the_current_document(change):
    document = ReadingDocument('old value old')
    proposal = ui.preview_find(snapshot(document), PronunciationRule('old', 'new'))
    selection = (0, 0)
    if change == 'text':
        document.set_text('edited value old')
    elif change == 'selection':
        selection = (4, 9)
    else:
        document = ReadingDocument(document.text)
    before = document.to_dict()
    with pytest.raises(ValueError, match='changed'):
        ui.apply_find_preview(document, proposal, snapshot(document, selection))
    assert document.to_dict() == before


@pytest.mark.parametrize('index', [True, -1, 99])
def test_invalid_selected_match_is_never_treated_as_replace_all(index):
    document = ReadingDocument('a a')
    proposal = ui.preview_find(snapshot(document), PronunciationRule('a', 'b'))
    with pytest.raises(ValueError, match='Choose a match'):
        ui.apply_find_preview(document, proposal, snapshot(document), index=index)
    assert document.text == 'a a'


def test_single_replace_requires_its_exact_reviewed_selection():
    document = ReadingDocument('one one')
    proposal = ui.preview_find(snapshot(document), PronunciationRule('one', 'two'))
    with pytest.raises(ValueError, match='Select this exact'):
        ui.apply_find_preview(document, proposal, snapshot(document), index=0)
    assert document.text == 'one one'


def test_zero_width_regex_keeps_exact_insertion_points_and_original():
    document = ReadingDocument('x x')
    proposal = ui.preview_find(snapshot(document), PronunciationRule(r'(?=x)', '^', regex=True))
    assert [(row.start, row.end) for row in proposal.matches] == [(0, 0), (2, 2)]
    ui.apply_find_preview(document, proposal, snapshot(document))
    assert document.text == '^x ^x' and document.source_text == 'x x'
    assert document.undo() and document.text == 'x x'


def test_no_match_and_unchanged_replacement_cannot_create_fake_edits():
    document = ReadingDocument('unchanged')
    for rule in (PronunciationRule('missing', 'new'), PronunciationRule('unchanged', 'unchanged')):
        proposal = ui.preview_find(snapshot(document), rule)
        with pytest.raises(ValueError, match='no text changes'):
            ui.apply_find_preview(document, proposal, snapshot(document))
    assert document.revision == 0


@pytest.mark.parametrize('rule', [
    PronunciationRule('[', 'x', regex=True),
    PronunciationRule('x', r'\9', regex=True),
])
def test_invalid_regex_or_group_fails_before_any_edit(rule):
    document = ReadingDocument('x x')
    with pytest.raises(RuntimeError):
        ui.preview_find(snapshot(document), rule)
    assert document.revision == 0 and document.text == document.source_text


def test_actual_match_budget_is_enforced_by_the_shared_worker():
    document = ReadingDocument('x' * 10001)
    with pytest.raises(RuntimeError):
        ui.preview_find(snapshot(document), PronunciationRule('x', 'y'))
    assert document.revision == 0


def test_actual_catastrophic_regex_is_timed_out_and_child_is_reaped(monkeypatch):
    import carr_library.speech._io as worker_io
    document = ReadingDocument('a' * 20000 + '!')
    children, original = [], worker_io.subprocess.Popen
    def started(*args, **kwargs):
        child = original(*args, **kwargs); children.append(child); return child
    monkeypatch.setattr(worker_io.subprocess, 'Popen', started)
    beginning = time.monotonic()
    with pytest.raises(TimeoutError):
        ui.preview_find(snapshot(document), PronunciationRule('(a+)+$', 'x', regex=True), timeout=.08)
    assert time.monotonic() - beginning < 4
    assert len(children) == 1 and children[0].poll() is not None
    assert document.revision == 0


def test_cancellation_of_actual_regex_child_reaps_it_without_publishing(monkeypatch):
    import carr_library.speech._io as worker_io
    document = ReadingDocument('a' * 20000 + '!')
    children, original, stop, timers = [], worker_io.subprocess.Popen, Event(), []
    def started(*args, **kwargs):
        child = original(*args, **kwargs); children.append(child)
        timer = Timer(.15, stop.set); timers.append(timer); timer.start()
        return child
    monkeypatch.setattr(worker_io.subprocess, 'Popen', started)
    try:
        with pytest.raises(InterruptedError):
            ui.preview_find(snapshot(document), PronunciationRule('(a+)+$', 'x', regex=True),
                            cancelled=stop.is_set, timeout=5)
    finally:
        stop.set()
        for timer in timers:
            timer.cancel(); timer.join(1)
    assert len(children) == 1 and children[0].poll() is not None
    assert document.text == document.source_text and document.revision == 0


def test_pre_cancelled_search_never_launches_a_worker(monkeypatch):
    document = ReadingDocument('text')
    monkeypatch.setattr(ui, 'run_process', lambda *a, **k: (_ for _ in ()).throw(AssertionError('Launched')))
    with pytest.raises(InterruptedError):
        ui.preview_find(snapshot(document), PronunciationRule('text', 'new'), cancelled=lambda: True)


@pytest.mark.parametrize('timeout', [True, 0, -.1, float('nan'), float('inf'), 11, 10 ** 400])
def test_invalid_execution_allowance_is_rejected(timeout):
    document = ReadingDocument('text')
    with pytest.raises(ValueError):
        ui.preview_find(snapshot(document), PronunciationRule('text', 'new'), timeout=timeout)
