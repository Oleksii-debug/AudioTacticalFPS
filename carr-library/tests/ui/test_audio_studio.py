"""Project/controller lifecycle; audio-quality acceptance belongs to real renders."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest

from carr_library.ui.audio_studio import StudioController, AudioStudioActions, plan_description


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


@pytest.mark.parametrize('operation', ['save', 'export_reading_copy'])
@pytest.mark.parametrize('existing', [False, True], ids=['new-file', 'replacement'])
def test_audio_export_approval_rejects_late_destination_change(controller, tmp_path, operation, existing):
    from carr_library.research.export_file import capture_export_destination
    controller.new('Immutable original')
    controller.edit_script('Separate reading derivative')
    target = tmp_path / 'chosen-export.txt'
    if existing:
        target.write_bytes(b'Approved old file')
    approval = capture_export_destination(target, private_root=controller.private_root)
    newer = b'Created or changed after the destination was approved'
    target.write_bytes(newer)
    before = controller.project_path, controller._file_hash, controller._saved_value
    with pytest.raises(ValueError, match='destination changed'):
        getattr(controller, operation)(target, overwrite=True, approval=approval)
    assert target.read_bytes() == newer
    assert (controller.project_path, controller._file_hash, controller._saved_value) == before
    assert controller.draft.source_text == 'Immutable original'
    assert controller.script == 'Separate reading derivative' and controller.dirty
    assert list(tmp_path.glob('.research-export-*')) == []


@pytest.mark.parametrize('operation', ['save', 'export_reading_copy'])
@pytest.mark.parametrize('existing', [False, True], ids=['new-file', 'replacement'])
def test_audio_export_captures_before_preparation(controller, tmp_path, monkeypatch, operation, existing):
    controller.new('Immutable original')
    controller.edit_script('Separate reading derivative')
    target = tmp_path / 'during-preparation.txt'
    if existing:
        target.write_bytes(b'Original destination')
    newer = b'Another application wrote this during export preparation'
    calls = []
    if operation == 'save':
        actual = controller.to_dict

        def prepare():
            result = actual()
            calls.append('serialized')
            target.write_bytes(newer)
            return result

        monkeypatch.setattr(controller, 'to_dict', prepare)
    else:
        actual = controller.draft.assert_source

        def prepare():
            actual()
            calls.append('source checked')
            target.write_bytes(newer)

        monkeypatch.setattr(controller.draft, 'assert_source', prepare)
    with pytest.raises(ValueError, match='destination changed'):
        getattr(controller, operation)(target, overwrite=True)
    assert len(calls) == 1
    assert target.read_bytes() == newer
    assert controller.project_path is None
    assert controller._file_hash is None and controller._saved_value is None
    assert controller.draft.source_text == 'Immutable original'
    assert list(tmp_path.glob('.research-export-*')) == []


def test_project_save_detects_change_after_external_hash_check(controller, tmp_path, monkeypatch):
    controller.new('Original project')
    target = tmp_path / 'project.json'
    controller.save(target)
    original_hash, original_saved = controller._file_hash, controller._saved_value
    controller.edit_script('Unsaved edits')
    newer = b'External project change after the initial saved-file check'
    actual = controller.to_dict

    def prepare():
        result = actual()
        target.write_bytes(newer)
        return result

    with monkeypatch.context() as scope:
        scope.setattr(controller, 'to_dict', prepare)
        with pytest.raises(ValueError, match='destination changed'):
            controller.save()
    assert target.read_bytes() == newer
    assert controller.project_path == target
    assert controller._file_hash == original_hash and controller._saved_value == original_saved
    assert controller.script == 'Unsaved edits' and controller.dirty
    assert list(tmp_path.glob('.research-export-*')) == []


@pytest.mark.parametrize('action', ['save_as', 'export_copy'])
@pytest.mark.parametrize('answer', ['approve', 'decline', 'changed-during-prompt'])
def test_audio_save_dialog_binds_replace_answer(controller, tmp_path, monkeypatch, action, answer):
    import sys
    from carr_library.ui.audio_studio import AudioStudioDialog
    controller.new('Immutable source')
    controller.edit_script('Reading derivative')
    target = tmp_path / 'chosen.txt'
    original = b'Existing destination selected in the file picker'
    newer = b'Changed by another application while Replace was displayed'
    target.write_bytes(original)
    prompts, errors, messages = [], [], []

    class ReplaceDialog:
        def __init__(self, parent, message, title, style):
            assert parent is dialog.dialog
            assert target.name in message
            assert style == wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION

        def ShowModal(self):
            prompts.append('shown')
            if answer == 'changed-during-prompt':
                target.write_bytes(newer)
            return wx.ID_NO if answer == 'decline' else wx.ID_YES

        def Destroy(self):
            prompts.append('destroyed')

    wx = SimpleNamespace(MessageDialog=ReplaceDialog, YES_NO=1, NO_DEFAULT=2,
                         ICON_QUESTION=4, ID_YES=10, ID_NO=11)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    dialog = AudioStudioDialog.__new__(AudioStudioDialog)
    dialog.dialog = object()
    dialog.controller = controller
    dialog.busy = False
    dialog._capture = lambda: None
    dialog._file = lambda *args, **kwargs: target
    dialog._refresh_text = lambda: None
    dialog._message = lambda message, **kwargs: messages.append(message)
    dialog._error = errors.append

    result = getattr(dialog, action)()
    assert prompts == ['shown', 'destroyed']
    assert controller.draft.source_text == 'Immutable source'
    if answer == 'changed-during-prompt':
        assert target.read_bytes() == newer
        assert len(errors) == 1 and isinstance(errors[0], ValueError)
        assert 'destination changed' in str(errors[0])
        assert messages == [] and controller.project_path is None and controller.dirty
        if action == 'save_as':
            assert result is False
    elif answer == 'decline':
        assert target.read_bytes() == original
        assert errors == [] and messages == []
        assert controller.project_path is None and controller.dirty
        if action == 'save_as':
            assert result is False
    else:
        assert errors == [] and len(messages) == 1
        if action == 'save_as':
            assert result is True and controller.project_path == target and not controller.dirty
            reopened = StudioController(private_root=controller.private_root).open(target)
            assert reopened.script == 'Reading derivative'
            assert reopened.draft.source_text == 'Immutable source'
        else:
            assert target.read_text(encoding='utf-8') == 'Reading derivative'
            assert controller.project_path is None and controller.dirty
    assert list(tmp_path.glob('.research-export-*')) == []
