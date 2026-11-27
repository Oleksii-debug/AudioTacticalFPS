"""Current modal/session contracts; native control acceptance remains separate."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.ui import legacy_transcription as ui
from carr_library.transcription_receipt import DEFAULT_SETTINGS
from tests.ui.test_transcription_dialog import view_fixture


def row():
    return dict(reference=dict(kind='archive', archive='a' * 64, job='b' * 32),
        title='Earlier project', recorded_status='complete', source_count=2, result_count=1,
        sources=[dict(id='1' * 32, name='Original.wav', bytes=40, has_result=True, has_edit=True),
                 dict(id='2' * 32, name='Pending.wav', bytes=30, has_result=False, has_edit=False)],
        historical=True)


def choices(monkeypatch, *answers):
    remaining = iter(answers)
    calls = []
    def choose(view, title, explanation, labels, *, multiple=False):
        view.session.guard()
        calls.append((title, tuple(labels), multiple))
        return next(remaining)
    monkeypatch.setattr(ui, '_choice', choose)
    return calls


def prepared():
    return dict(id='c' * 32, definition=dict(id='c' * 32, title='Recovered',
        settings=dict(DEFAULT_SETTINGS), runtime_manifest_sha256='d' * 64,
        inputs=[dict(relative='original.wav', path='inputs/root-0/original.wav', bytes=40)]),
        warning=None, historical_source=True)


def test_historical_listing_does_not_capture_or_activate_old_runtime(tmp_path, monkeypatch):
    view, finish = view_fixture(tmp_path)
    choices(monkeypatch, 0)
    listed = Mock(return_value=())
    retained = Mock(side_effect=AssertionError('No automatic live capture.'))
    monkeypatch.setattr(ui.recovery, 'jobs', listed)
    monkeypatch.setattr(ui.recovery, 'retain_live', retained)
    view.historical()
    assert not listed.called and view.session.busy
    finish()
    listed.assert_called_once()
    retained.assert_not_called()
    assert 'No retained' in view.status.value and view.service.calls == []


def test_explicit_capture_and_selected_import_use_current_worker_boundary(tmp_path, monkeypatch):
    view, finish = view_fixture(tmp_path)
    view.manifest_path = str(tmp_path / 'reviewed-manifest.json')
    chosen = choices(monkeypatch, 1, 0, 3, (1,))
    retained = Mock()
    monkeypatch.setattr(ui.recovery, 'retain_live', retained)
    monkeypatch.setattr(ui.recovery, 'jobs', Mock(return_value=(row(),)))
    imported = Mock(return_value=prepared())
    monkeypatch.setattr(ui.recovery, 'import_as_new', imported)
    view.historical()
    retained.assert_not_called()
    finish()
    retained.assert_called_once()
    imported.assert_not_called()
    assert view.session.busy and view.session.kind == 'historical-prepare'
    finish()
    args, kwargs = imported.call_args
    assert args[0] is view.service and args[1] == row()['reference']
    assert args[2] == ('2' * 32,)
    assert str(args[3]) == view.manifest_path and args[4] == dict(DEFAULT_SETTINGS)
    assert kwargs['title'] == 'Synthetic job'
    assert view.current_job == 'c' * 32 and view.definition is not None
    assert view.buttons['start'].enabled and view.service.calls == []
    assert chosen[-1][2] is True


def test_old_completion_never_substitutes_for_current_runtime_selection(tmp_path, monkeypatch):
    view, finish = view_fixture(tmp_path)
    choices(monkeypatch, 0, 0, 3)
    monkeypatch.setattr(ui.recovery, 'jobs', Mock(return_value=(row(),)))
    imported = Mock()
    monkeypatch.setattr(ui.recovery, 'import_as_new', imported)
    view.historical()
    finish()
    imported.assert_not_called()
    assert view.current_job is None and 'reviewed runtime' in view.status.value
    assert not view.buttons['start'].enabled


def test_committed_import_with_unreadable_definition_requires_explicit_reopen(tmp_path, monkeypatch):
    view, finish = view_fixture(tmp_path)
    view.manifest_path = str(tmp_path / 'reviewed.json')
    choices(monkeypatch, 0, 0, 3, (0,))
    monkeypatch.setattr(ui.recovery, 'jobs', Mock(return_value=(row(),)))
    result = dict(id='c' * 32, definition=None, historical_source=True,
                  warning='The new job was prepared; reopen its definition.')
    monkeypatch.setattr(ui.recovery, 'import_as_new', Mock(return_value=result))
    view.historical()
    finish()
    finish()
    assert 'c' * 32 in view.status.value and 'Refresh saved jobs' in view.status.value
    assert view.current_job is None and view.definition is None
    assert not view.buttons['start'].enabled and view.service.calls == []


def test_newer_library_blocks_historical_picker_callback(tmp_path, monkeypatch):
    view, finish = view_fixture(tmp_path)
    selected = choices(monkeypatch, 0)
    def changed(*args, **kwargs):
        view.frame.library = SimpleNamespace(root=tmp_path / 'other')
        return (row(),)
    monkeypatch.setattr(ui.recovery, 'jobs', changed)
    view.historical()
    finish()
    assert len(selected) == 1 and view.session.closed
    assert view.current_job is None and view.service.calls == []


def test_cancelled_historical_choice_keeps_current_draft_controls(tmp_path, monkeypatch):
    view, _ = view_fixture(tmp_path)
    view.sources = ('unsaved.wav',)
    view.title.ChangeValue('Current unfinished draft')
    choices(monkeypatch, None)
    jobs = Mock()
    monkeypatch.setattr(ui.recovery, 'jobs', jobs)
    view.historical()
    assert view.sources == ('unsaved.wav',) and view.title.GetValue() == 'Current unfinished draft'
    jobs.assert_not_called()


def test_close_waits_for_historical_copy_and_ignores_late_result(tmp_path, monkeypatch):
    view, finish = view_fixture(tmp_path)
    selected = choices(monkeypatch, 1)
    def cancelled(*args, **kwargs):
        assert kwargs['cancelled']()
        raise InterruptedError('Historical copy stopped at its byte boundary.')
    monkeypatch.setattr(ui.recovery, 'retain_live', cancelled)
    view.historical()
    assert view.session.request_close() is False
    finish()
    assert view.session.closed and len(selected) == 1
    assert view.current_job is None and view.service.calls == []


def test_only_accepted_sources_are_offered_for_historical_reading(tmp_path, monkeypatch):
    view, finish = view_fixture(tmp_path)
    view.dialog = object()
    selected = choices(monkeypatch, 0, 0, 0, 0)
    monkeypatch.setattr(ui.recovery, 'jobs', Mock(return_value=(row(),)))
    project = object()
    reader = Mock(return_value=project)
    monkeypatch.setattr(ui.recovery, 'reading_copy', reader)
    opened = Mock()
    monkeypatch.setattr('carr_library.ui.narration_editor.open_narration_editor', opened)
    view.historical()
    finish()
    assert selected[-1][1] == ('Original.wav',)
    finish()
    assert reader.call_args.args[2] == '1' * 32
    assert reader.call_args.kwargs['edited'] is False
    assert opened.call_args.args[2] is project
    assert view.service.calls == []
