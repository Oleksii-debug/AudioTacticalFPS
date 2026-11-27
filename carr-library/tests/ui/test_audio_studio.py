"""Project/controller lifecycle; audio-quality acceptance belongs to real renders."""
from dataclasses import dataclass
from concurrent.futures import Future
import hashlib
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest

from carr_library.ui.audio_studio import (StudioController, AudioStudioActions,
                                         AudioStudioDialog, plan_description)


@dataclass
class Plan:
    text: str
    title: str
    voice: str
    language: str

    def to_dict(self):
        return vars(self)


def parser(text, **kwargs):
    return Plan(text, **kwargs)


class Registry:
    def voices(self):
        return (SimpleNamespace(id='test:local', name='Local fixture', languages=('uk', 'sk'), engine='test', available=True),)

    def capabilities(self):
        return ({'engine': 'test', 'available': True},)


@pytest.fixture
def controller(tmp_path):
    ffmpeg = tmp_path / 'ffmpeg-test-placeholder'
    ffmpeg.write_text('No subprocess is launched in controller tests.', encoding='utf-8')
    return StudioController(private_root=tmp_path / 'private', registry=Registry(), parser=parser, ffmpeg=ffmpeg)


def test_save_open_preserves_immutable_source_and_derivative_and_dirty_state(controller, tmp_path):
    source = 'Привіт.\r\nDobrý deň!'
    controller.new(source, title='Дві мови')
    controller.configure(voice='test:local', language='uk', output_dir=tmp_path)
    controller.draft.replace_literal('Привіт', 'Приві́т')
    path = tmp_path / 'проєкт.json'
    assert controller.dirty
    controller.save(path)
    assert not controller.dirty
    controller.edit_script(controller.script + '\nNew line.')
    assert controller.dirty
    controller.open(path)
    assert controller.draft.source_text == source
    assert controller.script == 'Приві́т.\r\nDobrý deň!'
    assert not controller.dirty
    assert controller.voice == 'test:local' and controller.output_dir == tmp_path
    assert controller.draft.can_undo


def test_project_never_imports_an_executable_or_remote_mode(controller, tmp_path):
    path = tmp_path / 'project.json'
    controller.new('Text.')
    controller.save(path)
    original = controller.to_dict()
    for key, value in [('ffmpeg', '/tmp/untrusted-helper'), ('mode', 'APPROVED REMOTE'), ('credential', 'secret')]:
        forged = dict(original)
        forged[key] = value
        path.write_text(json.dumps(forged), encoding='utf-8')
        with pytest.raises(ValueError):
            controller.open(path)
        assert controller.to_dict() == original


def test_save_conflict_disk_failure_cancel_preserve_old_project(controller, tmp_path, monkeypatch):
    path = tmp_path / 'project.json'
    controller.new('Original')
    controller.save(path)
    original = path.read_bytes()
    controller.edit_script('Edited')
    with pytest.raises(TimeoutError):
        controller.save(cancelled=lambda: True)
    assert path.read_bytes() == original and controller.dirty
    path.write_text('External modification', encoding='utf-8')
    with pytest.raises(ValueError, match='changed outside'):
        controller.save()
    assert path.read_text() == 'External modification'
    before = controller.project_path
    def disk_full(*args, **kwargs):
        raise OSError('Disk full')
    monkeypatch.setattr('carr_library.ui.audio_studio.write_export', disk_full)
    with pytest.raises(OSError):
        controller.save(tmp_path / 'another.json')
    assert controller.project_path == before and controller.dirty


def test_recovery_is_bound_to_exact_saved_base_and_does_not_claim_commit(controller, tmp_path):
    path = tmp_path / 'project.json'
    controller.new('First version')
    controller.save(path)
    original = path.read_bytes()
    controller.edit_script('Recovered version')
    recovery = controller.save_recovery()
    assert recovery.is_file() and path.read_bytes() == original and controller.dirty
    controller.open(path)
    controller.recover()
    assert controller.script == 'Recovered version' and controller.dirty
    controller.save()
    assert not controller.dirty
    with pytest.raises(ValueError, match='different saved project revision'):
        controller.recover()


def test_recovery_refuses_unrelated_file_and_external_changes(controller, tmp_path):
    path = tmp_path / 'project.json'
    controller.new('Source')
    controller.save(path)
    recovery = path.with_name(path.name + '.recovery.json')
    recovery.write_text('{"kind":"other-document"}', encoding='utf-8')
    with pytest.raises(ValueError, match='another document'):
        controller.save_recovery()
    recovery.unlink()
    controller.save_recovery()
    recovery.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='changed outside'):
        controller.save_recovery()


def test_private_outputs_and_network_destinations_fail_before_renderer(controller, tmp_path):
    controller.private_root.mkdir()
    controller.new('Read this.')
    for target in (controller.private_root, '//server/share', 'https://example.test/output'):
        with pytest.raises(ValueError):
            controller.configure(output_dir=target)
    calls = []
    controller.renderer = lambda *args, **kwargs: calls.append(True)
    controller.configure(output_dir=tmp_path)
    with pytest.raises(InterruptedError):
        controller.render(cancelled=lambda: True)
    assert calls == []


def test_controller_passes_cancel_and_progress_and_checks_real_output_path(controller, tmp_path):
    controller.new('First role: Text.')
    controller.configure(voice='test:local', output_dir=tmp_path)
    output = tmp_path / 'render.wav'
    statuses, seen = [], []
    def render(plan, folder, *, engine, ffmpeg, cancelled, on_status):
        seen.append((plan.text, folder, engine, ffmpeg, cancelled()))
        on_status('Rendering first chunk')
        with wave.open(str(output), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(8000)
            wav.writeframes(b'\0\0' * 800)
        return {'status': 'complete', 'output': str(output), 'duration_seconds': 0.1}
    controller.renderer = render
    result = controller.render(on_status=statuses.append)
    assert result == controller.result and result['output'] == str(output)
    assert statuses == ['Rendering first chunk']
    assert seen[0][:2] == ('First role: Text.', tmp_path)
    assert seen[0][-1] is False
    assert controller.draft.source_text == 'First role: Text.'
    controller.renderer = lambda *args, **kwargs: {'status': 'complete', 'output': str(tmp_path / 'missing.wav')}
    with pytest.raises(ValueError, match='expected file'):
        controller.render()


def test_controller_rejects_incomplete_service_and_concurrent_render(controller, tmp_path):
    controller.new('Script.')
    controller.configure(output_dir=tmp_path)
    controller.renderer = lambda *args, **kwargs: {'status': 'cancelled'}
    with pytest.raises(ValueError, match='complete audio render'):
        controller.render()
    controller._render_lock.acquire()
    try:
        with pytest.raises(ValueError, match='already running'):
            controller.render()
    finally:
        controller._render_lock.release()


def test_readable_preview_and_voice_choices_no_wx_dependency(controller):
    controller.new('Narrator: Exact spoken line.', title='Book')
    summary = controller.preview()
    assert 'Exact spoken line.' in summary and 'Book' in summary and 'Local production plan' in summary
    assert controller.voices()[0]['id'] == 'test:local'
    assert controller.voice_status() == 'test: available'


def test_literal_mode_preserves_source_that_looks_like_script(tmp_path):
    from carr_library.audio.models import Line
    controller = StudioController(private_root=tmp_path / 'private')
    source = 'Date: 2026-11-26\n[pause 2]\n# A heading\fSecond source segment.'
    controller.new(source)
    plan = controller.plan()
    assert controller.input_kind == 'narration'
    assert plan.original_script == source
    assert len(plan.roles) == 1 and len(plan.scenes) == 2
    assert all(isinstance(event, Line) for scene in plan.scenes for event in scene.events)
    assert '\f'.join(''.join(event.text for event in scene.events) for scene in plan.scenes) == source
    assert controller.draft.source_text == source


def test_saved_interpretation_and_legacy_projects_are_unambiguous(controller, tmp_path):
    path = tmp_path / 'project.json'
    controller.new('Label: unchanged source')
    controller.save(path)
    controller.configure(input_kind='script')
    assert controller.dirty
    controller.open(path)
    assert controller.input_kind == 'narration' and not controller.dirty
    saved = controller.to_dict()
    legacy = {key: value for key, value in saved.items() if key != 'input_kind'}
    legacy['version'] = 1
    path.write_text(json.dumps(legacy), encoding='utf-8')
    controller.open(path)
    assert controller.input_kind == 'script'
    legacy['source_reference'] = {'kind': 'library-source-reading-copy'}
    path.write_text(json.dumps(legacy), encoding='utf-8')
    controller.open(path)
    assert controller.input_kind == 'narration'
    invalid = {**saved, 'input_kind': 'execute'}
    path.write_text(json.dumps(invalid), encoding='utf-8')
    before = controller.to_dict()
    with pytest.raises(ValueError, match='mode'):
        controller.open(path)
    assert controller.to_dict() == before


def test_original_imported_file_cannot_be_replaced_by_project_or_reading_export(controller, tmp_path):
    original = tmp_path / 'source.txt'
    original.write_text('Original source', encoding='utf-8')
    controller.new('Original source', source_reference={'kind': 'local-text', 'original_path': str(original),
                   'filename': original.name, 'bytes_sha256': hashlib.sha256(original.read_bytes()).hexdigest()})
    controller.edit_script('Reading derivative')
    with pytest.raises(ValueError, match='immutable'):
        controller.save(original, overwrite=True)
    with pytest.raises(ValueError, match='immutable'):
        controller.export_reading_copy(original, overwrite=True)
    assert original.read_text(encoding='utf-8') == 'Original source'


def test_selected_book_import_uses_exact_pages_and_retains_revision(tmp_path):
    from carr_library.application import LibraryApp
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('My research')
    raw = 'Перший текст.\fSecond exact source page.'
    book = app.add_text(workspace, 'Book', raw, authors=('Author',))
    panel = SimpleNamespace(workspace=workspace, ready=True, _selection=lambda: (None, None, book['edition']))
    captured, reports = [], []
    frame = SimpleNamespace(library=app, active=(workspace, None), working=False, busy=False,
        _workspace=lambda: workspace, book=SimpleNamespace(GetPage=lambda index: panel),
        cancel_event=SimpleNamespace(is_set=lambda: False), report=reports.append)
    frame.run_job = lambda label, action, done: done(action())
    class Studio:
        def __init__(self, parent, controller):
            captured.append(controller)
        def show(self):
            pass
    AudioStudioActions(frame, studio_factory=Studio).from_selected_book()
    assert not reports
    controller = captured[0]
    assert controller.script == raw
    assert controller.input_kind == 'narration'
    assert controller.plan().original_script == raw
    assert controller.source_reference['edition'] == book['edition']
    assert len(controller.source_reference['pages']) == 2
    assert controller.source_reference['pages'][0]['location']['text_sha256'] == hashlib.sha256(raw.split('\f')[0].encode()).hexdigest()
    controller.draft.replace_literal('Перший', 'Змінений')
    assert controller.draft.source_text == raw
    assert app._selections == {}


def _studio_dialog(controller):
    """Exercise native handlers and their real worker routing without claiming wx execution."""
    dialog = AudioStudioDialog.__new__(AudioStudioDialog)
    dialog.controller = controller
    dialog.dialog = object()
    dialog.busy = dialog.closed = dialog.close_pending = False
    dialog.cancelled = Event()
    dialog.controls, dialog.cancel_button, dialog.playback = [Mock()], Mock(), Mock()
    dialog.jobs, dialog.posted = [], []

    def submit(action):
        future = Future()
        dialog.jobs.append((action, future))
        return future

    chooser = Mock()
    chooser.ShowModal.return_value = 1
    chooser.GetSelection.return_value = 0
    dialog.wx = SimpleNamespace(ID_OK=1, SingleChoiceDialog=Mock(return_value=chooser),
                                CallAfter=dialog.posted.append)
    dialog.executor = SimpleNamespace(submit=submit)
    dialog._capture, dialog._refresh_text, dialog._message, dialog._error = (Mock() for _ in range(4))
    dialog._unsaved = Mock(return_value=True)
    dialog._file = Mock(side_effect=AssertionError('Unexpected standalone file chooser'))
    dialog.plan_text = Mock()
    return dialog


def _finish_studio_job(dialog, *, after_read=None):
    action, future = dialog.jobs.pop(0)
    try:
        future.set_result(action())
    except Exception as exc:
        future.set_exception(exc)
    if after_read is not None:
        after_read()
    while dialog.posted:
        dialog.posted.pop(0)()


def test_native_save_library_backup_restore_and_reopen_keeps_original_and_draft(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.library_backup import restore_backup

    app = LibraryApp(tmp_path / 'original-library')
    app.create_workspace('Research')
    controller = StudioController(private_root=app.root)
    original = 'Точне джерело.\r\nSecond original line.'
    controller.new(original, title='Дослідницький аудіопроєкт')
    dialog = _studio_dialog(controller)
    dialog._capture.side_effect = lambda: controller.edit_script('Explicit saved reading copy.')
    assert dialog.save() is True
    assert controller.project_path.is_relative_to(app.root / 'audio-projects')
    assert controller.output_dir == controller.project_path.parent.parent / 'audio'
    assert controller.output_dir.is_dir()
    dialog._file.assert_not_called()
    controller.edit_script('Uncommitted recovery copy.')
    controller.save_recovery()
    project_bytes = controller.project_path.read_bytes()
    relative = controller.project_path.relative_to(app.root)

    snapshot, destination = tmp_path / 'backup', tmp_path / 'restored-library'
    checksum = app.backup(snapshot)
    restored_root = Path(restore_backup(snapshot, destination, checksum))
    reopened = StudioController(private_root=restored_root)
    restored_dialog = _studio_dialog(reopened)
    restored_dialog.open_library()
    assert restored_dialog.busy and len(restored_dialog.jobs) == 1
    restored_dialog.wx.SingleChoiceDialog.assert_not_called()
    restored_dialog.controls[0].Disable.assert_called_once()
    _finish_studio_job(restored_dialog)
    restored_dialog._error.assert_not_called()
    assert not restored_dialog.busy
    restored_dialog.controls[0].Enable.assert_called_once()
    assert reopened.project_path == restored_root / relative
    assert reopened.project_path.read_bytes() == project_bytes
    assert reopened.draft.source_text == original
    assert reopened.script == 'Explicit saved reading copy.'
    assert reopened.output_dir == reopened.project_path.parent.parent / 'audio'
    assert reopened.output_dir.is_relative_to(restored_root)
    reopened.recover()
    assert reopened.script == 'Uncommitted recovery copy.'
    assert reopened.draft.source_text == original
    reopened.save()
    assert controller.project_path.read_bytes() == project_bytes
    assert json.loads(reopened.project_path.read_text(encoding='utf-8'))['output_dir'] == str(reopened.output_dir)


@pytest.mark.parametrize('accepted', [True, False], ids=['standalone-save', 'standalone-cancel'])
def test_native_first_save_preserves_explicit_external_output_choice(tmp_path, accepted):
    controller = StudioController(private_root=tmp_path / 'private')
    controller.new('Exact original')
    destination = tmp_path / 'standalone.json'
    dialog = _studio_dialog(controller)
    # The output choice is captured from the UI before deciding where to save.
    dialog._capture.side_effect = lambda: controller.configure(output_dir=tmp_path)
    dialog._file = Mock(return_value=destination if accepted else None)
    assert dialog.save() is accepted
    assert controller.project_path == (destination if accepted else None)
    assert destination.exists() is accepted
    assert controller.output_dir == tmp_path
    assert not (controller.private_root / 'audio-projects').exists()
    assert controller.draft.source_text == 'Exact original'


def test_native_save_keeps_an_existing_external_project_external(tmp_path):
    controller = StudioController(private_root=tmp_path / 'private')
    controller.new('Source')
    path = controller.save(tmp_path / 'external.json')
    dialog = _studio_dialog(controller)
    dialog._capture.side_effect = lambda: controller.edit_script('Saved external edit')
    assert dialog.save() is True
    assert controller.project_path == path
    assert not (controller.private_root / 'audio-projects').exists()
    reopened = StudioController(private_root=controller.private_root).open(path)
    assert reopened.script == 'Saved external edit' and reopened.draft.source_text == 'Source'
    dialog._file.assert_not_called()


@pytest.mark.parametrize('guard', ['busy', 'closed', 'unsaved'], ids=['busy', 'closed', 'unsaved-cancel'])
def test_native_library_chooser_respects_existing_operation_and_unsaved_guards(tmp_path, guard):
    dialog = _studio_dialog(StudioController(private_root=tmp_path / 'private'))
    if guard == 'unsaved':
        dialog._unsaved.return_value = False
    else:
        setattr(dialog, guard, True)
    dialog.open_library()
    assert not dialog.jobs
    dialog.wx.SingleChoiceDialog.assert_not_called()


@pytest.mark.parametrize('phase', ['before', 'after'], ids=['cancel-before-read', 'cancel-before-chooser'])
def test_native_library_discovery_cancel_does_not_open_a_project(tmp_path, phase):
    from carr_library.application import LibraryApp
    app = LibraryApp(tmp_path / 'library')
    saved = StudioController(private_root=app.root)
    saved.new('Saved source')
    saved.save_in_library()
    current = StudioController(private_root=app.root)
    current.new('Keep current editing session')
    dialog = _studio_dialog(current)
    dialog.open_library()
    if phase == 'before':
        dialog.cancelled.set()
    _finish_studio_job(dialog, after_read=dialog.cancelled.set if phase == 'after' else None)
    assert current.project_path is None and current.script == 'Keep current editing session'
    dialog.wx.SingleChoiceDialog.assert_not_called()
    dialog._refresh_text.assert_not_called()
    assert not dialog.busy


@pytest.mark.parametrize('selection', [None, -1, 1], ids=['native-cancel', 'no-selection', 'out-of-range'])
def test_native_library_chooser_cancel_or_invalid_row_preserves_current_project(tmp_path, selection):
    from carr_library.application import LibraryApp
    app = LibraryApp(tmp_path / 'library')
    saved = StudioController(private_root=app.root)
    saved.new('Saved source')
    saved.save_in_library()
    current = StudioController(private_root=app.root)
    current.new('Keep this source')
    dialog = _studio_dialog(current)
    chooser = dialog.wx.SingleChoiceDialog.return_value
    chooser.ShowModal.return_value = 0 if selection is None else dialog.wx.ID_OK
    chooser.GetSelection.return_value = selection
    dialog.open_library()
    _finish_studio_job(dialog)
    chooser.Destroy.assert_called_once()
    assert current.project_path is None and current.script == 'Keep this source'
    dialog._refresh_text.assert_not_called()
    if selection is not None:
        assert isinstance(dialog._error.call_args.args[0], ValueError)


def test_native_library_chooser_empty_library_reports_without_file_dialog(tmp_path):
    dialog = _studio_dialog(StudioController(private_root=tmp_path / 'private'))
    dialog.open_library()
    _finish_studio_job(dialog)
    dialog.wx.SingleChoiceDialog.assert_not_called()
    dialog._file.assert_not_called()
    assert 'no saved audio projects' in dialog._message.call_args.args[0]
