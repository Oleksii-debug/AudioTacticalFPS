"""Exercise the reader's actual shared edit closure with native Unicode offsets."""
from hashlib import sha256
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.speech.spelling import HunspellProfile, SpellingFile, SpellingIssue, SpellingResult
from carr_library.ui import spelling_dialog as ui

_spec = importlib.util.spec_from_file_location('carr_spelling_reader_fixtures',
    Path(__file__).with_name('test_find_replace_reader_hook.py'))
_fixtures = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixtures)


@pytest.fixture
def reader(tmp_path, monkeypatch):
    state, original, start, _native_start = _fixtures.reader.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(ui, 'create_spelling_dialog', _fixtures.review_ui.create_find_dialog)
    return state, original, start


def correction(snapshot):
    first = snapshot.text.index('Dr.')
    item = SpellingFile('/synthetic-only', '0' * 64, 1)
    profile = HunspellProfile(item, item, item, 'synthetic', 'No runtime execution in this closure test')
    result = SpellingResult(profile, sha256(snapshot.text.encode()).hexdigest(), (0, len(snapshot.text)),
                           2, 'UTF-8', (SpellingIssue(first, first + 3, 'Dr.', ('Doctor',)),))
    return ui.SpellingReview(snapshot, result)


def test_real_reader_spelling_closure_select_apply_undo_and_recovery(reader):
    state, original, _ = reader
    def exercise(*, capture, select, apply, undo, **kwargs):
        initial = capture(); issue = correction(initial).result.issues[0]
        select(initial, (issue.start, issue.end))
        assert state.editor.GetRange(*state.editor.GetSelection()) == 'Dr.'
        current = capture()
        preview = ui.preview_correction(correction(current), 0, 'Доктор 😀', current)
        applied, warning = apply(preview)
        assert warning == '' and state.document.text == initial.text.replace('Dr.', 'Доктор 😀', 1)
        assert state.document.source_text == original
        assert undo(applied) == '' and state.document.text == initial.text
    state.driver = exercise
    command, playback = _fixtures.reader_hook(state); command(spelling=True)
    assert not state.messages and playback() == (None, 12)
    assert len(state.saved) == len(state.stops) == len(state.refreshed) == 2
    assert state.reviews[0].destroyed and state.editor.focused


@pytest.mark.parametrize('changed', ['library', 'root', 'closing', 'source', 'selection'])
def test_spelling_closure_rechecks_authority_before_apply(reader, tmp_path, changed):
    state, original, _ = reader
    def exercise(*, capture, select, apply, **kwargs):
        initial = capture(); issue = correction(initial).result.issues[0]
        select(initial, (issue.start, issue.end))
        current = capture(); preview = ui.preview_correction(correction(current), 0, 'Doctor', current)
        if changed == 'library': state.parent.library = SimpleNamespace(root=state.parent.library.root)
        elif changed == 'root':
            other = tmp_path / 'other'; other.mkdir(); state.parent.library.root = other
        elif changed == 'closing': state.parent._closing = True
        elif changed == 'source': state.authorized = False
        else: state.editor.selection = (0, 0)
        before = state.document.to_dict()
        with pytest.raises(ValueError): apply(preview)
        assert state.document.to_dict() == before
    state.driver = exercise
    _fixtures.reader_hook(state)[0](spelling=True)
    assert state.document.source_text == original and not state.saved and not state.messages


def test_spelling_closure_reports_committed_edit_when_recovery_is_unavailable(reader):
    state, original, _ = reader
    state.save_failure = True
    def exercise(*, capture, select, apply, undo, **kwargs):
        initial = capture(); issue = correction(initial).result.issues[0]
        select(initial, (issue.start, issue.end))
        current = capture(); preview = ui.preview_correction(correction(current), 0, 'Doctor', current)
        applied, warning = apply(preview)
        assert 'in memory' in warning and 'Save As' in warning
        assert state.document.text != initial.text
        assert state.document.source_text == original
        assert 'recovery save failed' in undo(applied) and state.document.text == initial.text
    state.driver = exercise
    _fixtures.reader_hook(state)[0](spelling=True)
    assert not state.saved and not state.messages
