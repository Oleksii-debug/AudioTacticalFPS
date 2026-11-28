"""Actual current reader/Studio host closures; no replacement controller."""
from pathlib import Path
from types import SimpleNamespace
import runpy

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.reader.speech_normalization import SpeechOptions
from carr_library.ui import speech_preparation as ui

_base = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'speech' / 'test_pronunciation_reader_hook.py'))


@pytest.fixture
def reader(tmp_path, monkeypatch):
    state, _, _, _ = _base['reader'].__wrapped__(tmp_path, monkeypatch)
    source = '😀 retained\r\n12 exact middle 5 kg.'
    state.document = ReadingDocument(source)
    state.editor.value = source
    start = source.index('12')
    first = len(source[:start].encode('utf-16-le')) // 2
    last = len(source.encode('utf-16-le')) // 2
    state.editor.selection = (first, last)
    class Review:
        def __init__(self, callbacks):
            self.callbacks, self.alive, self.destroyed = callbacks, True, False
        def ShowModal(self):
            state.driver(**self.callbacks)
        def finish_close(self):
            self.alive = False
        def Destroy(self):
            self.destroyed = True
    def create(parent, **callbacks):
        review = Review(callbacks)
        state.reviews.append(review)
        return review
    monkeypatch.setattr(ui, 'create_speech_preparation', create)
    return state, source, start


def test_actual_reader_preparation_hook_keeps_unicode_ranges_source_interior_and_one_undo(reader):
    state, source, start = reader
    def exercise(*, capture, apply, undo, **kwargs):
        snapshot = capture()
        assert snapshot.selection == (start, len(source))
        proposal = ui.prepare_selection(snapshot, snapshot.selection,
            SpeechOptions(mode='speech', locale='en-US', numbers=True, units=True))
        accepted, warning = apply(proposal)
        assert not warning and state.document.text == source[:start] + 'twelve exact middle five kilograms.'
        offset = state.document.text.index(' exact middle ')
        assert state.document.source_span(offset, offset + len(' exact middle ')).exact
        assert state.saved[-1]['schema'] == 'carr.reading-copy.v3'
        assert undo(accepted) == '' and state.document.text == source
    state.driver = exercise
    command, playback = _base['reader_hook'](state)
    command(preparation=True)
    assert not state.messages and state.reviews[0].destroyed and state.editor.focused
    assert playback() == (2, False) and state.document.source_text == source


@pytest.mark.parametrize('changed', ['library', 'root', 'closed', 'source', 'voice', 'selection'])
def test_actual_reader_hook_refuses_stale_scope_before_any_apply(reader, tmp_path, changed):
    state, source, _ = reader
    def exercise(*, capture, apply, **kwargs):
        proposal = ui.prepare_selection(capture(), capture().selection,
            SpeechOptions(mode='speech', locale='en-US', numbers=True))
        if changed == 'library':
            state.parent.library = SimpleNamespace(root=state.parent.library.root)
        elif changed == 'root':
            path = tmp_path / 'other'; path.mkdir()
            state.parent.library.root = path
        elif changed == 'closed':
            state.parent._closing = True
        elif changed == 'source':
            state.authorized = False
        elif changed == 'voice':
            state.voice_choice.index = 1
        else:
            state.editor.selection = (0, 1)
        with pytest.raises(ValueError):
            apply(proposal)
    state.driver = exercise
    command, _ = _base['reader_hook'](state)
    command(preparation=True)
    assert state.document.text == source and not state.saved
    assert state.reviews[0].destroyed


def test_actual_studio_service_saves_prepared_line_with_unchanged_other_lines_and_script(tmp_path):
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'studio' / 'test_pronunciation.py'))
    app, service, project, draft = module['configured'](tmp_path, clips=False)
    try:
        original = draft.document.source_text
        snapshot = ui.capture_speech(draft.document, (0, len(original)),
                                    language=draft.role.language, voice_id=draft.role.voice_id)
        proposal = ui.prepare_selection(snapshot, snapshot.selection,
            SpeechOptions(mode='speech', locale='en-US', numbers=True, units=True))
        ui.apply_speech(draft.document, proposal, snapshot)
        assert draft.document.text == 'Dr. One carries twelve kilograms.'
        assert service.projects.load(project.id) == project
        saved = service.save_line(draft)
        assert saved.after.original_script == project.original_script
        assert saved.after.roles == project.roles and saved.after.scenes[1:] == project.scenes[1:]
        assert saved.after.scenes[0].lines[1:] == project.scenes[0].lines[1:]
        assert saved.after.scenes[0].lines[0].text == draft.document.text
        assert saved.after.scenes[0].lines[0].source_ref == draft.line.source_ref
        assert service.undo_line(saved).after.scenes == project.scenes
    finally:
        assert app.close_search()



def test_actual_studio_preparation_host_uses_existing_worker_and_native_range(tmp_path, monkeypatch):
    from dataclasses import replace
    studio = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'studio' / 'test_pronunciation_dialog.py'))
    fixture = studio['view'].__wrapped__(tmp_path)
    view = next(fixture)
    try:
        dialog = view.dialog
        original = '😀 12 middle 5 kg.\r\n'
        line = replace(view.project.scenes[0].lines[0], text=original)
        project = replace(view.project, revision=2,
            scenes=(replace(view.project.scenes[0], lines=(line, *view.project.scenes[0].lines[1:])),))
        dialog.service.projects.save(project, expected_revision=1)
        dialog.reload(); view.frame.drain()
        dialog.open_line(); view.frame.drain()
        first = len('😀 '.encode('utf-16-le')) // 2
        last = len(original.encode('utf-16-le')) // 2
        dialog.text.SetSelection(first, last)
        observed = []
        class Child:
            alive = True
            def __bool__(self): return True
            def ShowModal(self):
                snapshot = callbacks['capture']()
                assert snapshot.selection == (2, len(original))
                assert snapshot.voice_id == 'fixture:one'
                def complete(proposal):
                    assert view.frame.working is False
                    accepted, warning = callbacks['apply'](proposal)
                    assert 'local Studio line draft' in warning
                    assert dialog.draft.document.text == '😀 twelve middle five kilograms.\r\n'
                    assert dialog.draft.document.source_span(8, 16).exact
                    callbacks['undo'](accepted)
                    assert dialog.draft.document.text == original
                    observed.append(True)
                assert callbacks['run'](lambda: ui.prepare_selection(snapshot, snapshot.selection,
                    SpeechOptions(mode='speech', locale='en-US', numbers=True, units=True)),
                    complete, on_failed=lambda error: pytest.fail(str(error)))
                view.frame.drain()
            def finish_close(self): self.alive = False
            def Destroy(self): self.destroyed = True
        callbacks = {}
        def factory(parent, **values):
            assert parent is dialog and values['cancel'] is view.frame.cancel_event
            callbacks.update(values)
            return Child()
        monkeypatch.setattr(ui, 'create_speech_preparation', factory)
        assert dialog.buttons['prepare'].IsEnabled()
        dialog.review(preparation=True)
        assert observed == [True] and not dialog.draft.dirty
        assert dialog.service.projects.load(project.id) == project
        assert all(thread != view.frame.owner for thread in view.frame.worker_threads)
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass
        assert view.frame.library.close_search()
