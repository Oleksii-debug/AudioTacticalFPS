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


def test_saved_reading_copy_requires_its_exact_save_digest_before_studio_mutation(controller):
    from carr_library.reader.narration import encode_project, new_project
    project = new_project('New original')
    controller.new('Existing studio text', title='Keep this project')
    before = controller.to_dict()
    with pytest.raises(ValueError, match='Save this exact'):
        controller.from_reading_project(project, saved_digest='0' * 64)
    assert controller.to_dict() == before
    digest = hashlib.sha256(encode_project(project)).hexdigest()
    controller.from_reading_project(project, saved_digest=digest)
    assert controller.script == controller.draft.source_text == 'New original'
    assert controller.source_reference['project_sha256'] == digest


def test_recovery_filename_cannot_replace_an_imported_original(tmp_path):
    from carr_library.reader.narration import NarrationStore, import_text
    from carr_library.ui.narration_editor import NarrationController
    private = tmp_path / 'private'
    private.mkdir()
    source = tmp_path / 'studio.json.recovery.json'
    source.write_text('Original source text', encoding='utf-8')
    reading = NarrationController(NarrationStore(private), import_text(source))
    studio = reading.prepare_audio_studio()
    studio.save(tmp_path / 'studio.json')
    studio.edit_script('Changed derivative')
    with pytest.raises(ValueError, match='original source file is immutable'):
        studio.save_recovery()
    assert source.read_text(encoding='utf-8') == 'Original source text'


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


@pytest.mark.parametrize('operation', ['save', 'save_as', 'recovery', 'reading_copy'])
def test_late_writer_before_export_preflight_is_preserved(controller, tmp_path, monkeypatch, operation):
    import carr_library.ui.audio_studio as module
    controller.new('Original source')
    project = tmp_path / 'project.json'
    controller.save(project)
    controller.edit_script('My pending edits')
    if operation == 'save':
        destination = project
        action = controller.save
    elif operation == 'save_as':
        destination = tmp_path / 'new-project.json'
        action = lambda: controller.save(destination)
    elif operation == 'recovery':
        destination = controller.save_recovery()
        controller.edit_script('More pending edits')
        action = controller.save_recovery
    else:
        destination = tmp_path / 'reading.txt'
        destination.write_text('Previous reading copy', encoding='utf-8')
        action = lambda: controller.export_reading_copy(destination, overwrite=True)
    previous_hash, previous_path = controller._file_hash, controller.project_path
    original = module.write_export
    def interleaved(path, text, **kwargs):
        destination.write_text('Another session saved these bytes', encoding='utf-8')
        return original(path, text, **kwargs)
    monkeypatch.setattr(module, 'write_export', interleaved)
    with pytest.raises((ValueError, FileExistsError)):
        action()
    assert destination.read_text(encoding='utf-8') == 'Another session saved these bytes'
    assert controller._file_hash == previous_hash and controller.project_path == previous_path
    assert controller.dirty


def test_invalid_new_project_does_not_replace_current_draft(controller):
    controller.new('Keep this original', title='Existing')
    controller.edit_script('Keep this reading copy')
    before = controller.to_dict()
    with pytest.raises(ValueError):
        controller.new('Replacement', title='\x00invalid')
    assert controller.to_dict() == before


def test_selecting_ffmpeg_refreshes_only_owned_runtime_registry(tmp_path, monkeypatch):
    import carr_library.audio.engines as engines
    binaries = [tmp_path / 'ffmpeg-a', tmp_path / 'ffmpeg-b']
    for binary in binaries:
        binary.write_bytes(b'Explicit fixture runtime; no subprocess requested')
    selected = []
    def registry(**settings):
        selected.append(settings['ffmpeg'])
        return Registry()
    monkeypatch.setattr(engines, 'default_registry', registry)
    studio = StudioController(private_root=tmp_path / 'private')
    studio.configure(ffmpeg=binaries[0])
    studio.voices()
    studio.configure(ffmpeg=binaries[1])
    studio.voices()
    assert selected == binaries
    injected = Registry()
    studio = StudioController(private_root=tmp_path / 'private', registry=injected)
    studio.configure(ffmpeg=binaries[0])
    assert studio._registry() is injected


def test_controller_voice_discovery_forwards_cancel_to_local_registry(tmp_path):
    class CancellableRegistry(Registry):
        def voices(self, *, cancelled=lambda: False):
            if cancelled():
                raise InterruptedError('Local voice discovery cancelled')
            return super().voices()
    studio = StudioController(private_root=tmp_path / 'private', registry=CancellableRegistry())
    with pytest.raises(InterruptedError, match='discovery cancelled'):
        studio.voices(cancelled=lambda: True)
    assert studio.voices(cancelled=lambda: False)[0]['id'] == 'test:local'


def test_output_folder_uses_shared_local_drive_guard_before_accepting(controller, tmp_path, monkeypatch):
    calls = []
    def remote_drive(path):
        calls.append(path)
        raise ValueError('Mapped network drives are not local audio destinations')
    monkeypatch.setattr('carr_library.ui.audio_studio.checked_directory', remote_drive)
    with pytest.raises(ValueError, match='Mapped network'):
        controller.configure(output_dir=tmp_path)
    assert calls == [tmp_path] and controller.output_dir is None


@pytest.mark.parametrize('operation', ['save_as', 'recovery', 'reading_copy'])
def test_final_new_destination_publication_never_overwrites_a_late_writer(
        controller, tmp_path, monkeypatch, operation):
    import carr_library.ui.audio_studio as module
    controller.new('Immutable original')
    controller.save(tmp_path / 'project.json')
    controller.edit_script('Pending reading edit')
    if operation == 'save_as':
        destination = tmp_path / 'new.json'
        action = lambda: controller.save(destination)
    elif operation == 'recovery':
        destination = controller.project_path.with_name(controller.project_path.name + '.recovery.json')
        action = controller.save_recovery
    else:
        destination = tmp_path / 'reading.txt'
        action = lambda: controller.export_reading_copy(destination)
    before = controller._file_hash, controller.project_path, controller._recovery_hash
    if operation == 'reading_copy':
        # Captured reading exports use the shared export publisher's actual
        # final no-replace primitive, rather than the project save stage.
        from carr_library.research import export_file
        owner = export_file.os
        primitive = 'rename' if owner.name == 'nt' else 'link'
    else:
        owner, primitive = module, 'publish_noreplace'
    original = getattr(owner, primitive)
    def writer_at_publication(stage, target, *args, **kwargs):
        assert Path(target) == destination and not destination.exists()
        destination.write_bytes(b'Another writer owns this destination')
        return original(stage, target, *args, **kwargs)
    monkeypatch.setattr(owner, primitive, writer_at_publication)
    with pytest.raises(FileExistsError):
        action()
    assert destination.read_bytes() == b'Another writer owns this destination'
    assert (controller._file_hash, controller.project_path, controller._recovery_hash) == before
    assert controller.dirty and not list(tmp_path.glob('.audio-save-*'))


@pytest.mark.parametrize('name', ['CON.json', 'project.json:stream', 'project.json.'])
@pytest.mark.parametrize('operation', ['save', 'export_reading_copy'])
def test_new_studio_file_keeps_export_filename_guards(controller, tmp_path, monkeypatch, name, operation):
    controller.new('Source')
    selected = tmp_path / name
    before = set(tmp_path.iterdir())
    original_exists = Path.exists
    def no_destination_probe(path):
        assert path != selected, 'Reject device aliases before probing or reading the destination.'
        return original_exists(path)
    monkeypatch.setattr(Path, 'exists', no_destination_probe)
    with pytest.raises(ValueError, match='filename'):
        getattr(controller, operation)(selected)
    assert controller.project_path is None
    # CON exists as a device alias on Windows even when no file was created.
    assert set(tmp_path.iterdir()) == before

def _destination_dialog(controller, destination):
    """Exercise the real handlers with only native picker/message adapters."""
    messages, errors = [], []
    dialog = SimpleNamespace(
        busy=False, dialog=object(), controller=controller,
        _capture=lambda: None,
        _file=lambda *args, **kwargs: destination,
        _refresh_text=lambda: None,
        _message=lambda text, **kwargs: messages.append(text),
        _error=errors.append)
    return dialog, messages, errors


@pytest.mark.parametrize('operation', ['save_as', 'export_copy'])
@pytest.mark.parametrize('accepted', [False, True], ids=['declined', 'confirmed'])
def test_studio_native_replacement_requires_separate_bound_consent(
        controller, tmp_path, monkeypatch, operation, accepted):
    import carr_library.ui.audio_studio as module
    from carr_library.ui.export_destination import approve_export_destination
    controller.new('Immutable source')
    controller.edit_script('Explicit reading derivative')
    destination = tmp_path / ('project.json' if operation == 'save_as' else 'reading.txt')
    destination.write_text('Existing destination', encoding='utf-8')
    dialog, messages, errors = _destination_dialog(controller, destination)
    questions = []
    def confirm(parent, target):
        questions.append((parent, target))
        return accepted
    monkeypatch.setattr(module, 'approve_export_destination',
        lambda parent, path, **kwargs: approve_export_destination(
            parent, path, confirm=confirm, **kwargs))
    result = getattr(module.AudioStudioDialog, operation)(dialog)
    assert questions == [(dialog.dialog, destination)]
    assert not errors and controller.draft.source_text == 'Immutable source'
    if not accepted:
        assert destination.read_text(encoding='utf-8') == 'Existing destination'
        assert not messages and controller.project_path is None and controller.dirty
        if operation == 'save_as':
            assert result is False
    elif operation == 'save_as':
        assert result is True and controller.project_path == destination
        reopened = StudioController(private_root=controller.private_root).open(destination)
        assert reopened.to_dict() == controller.to_dict()
        assert reopened.script == 'Explicit reading derivative'
        assert messages and not controller.dirty
    else:
        assert destination.read_text(encoding='utf-8') == 'Explicit reading derivative'
        assert messages and controller.project_path is None and controller.dirty


@pytest.mark.parametrize('operation', ['save_as', 'export_copy'])
def test_studio_native_confirmation_cannot_approve_another_writers_new_bytes(
        controller, tmp_path, monkeypatch, operation):
    import carr_library.ui.audio_studio as module
    from carr_library.ui.export_destination import approve_export_destination
    controller.new('Immutable source')
    controller.edit_script('Pending reading derivative')
    destination = tmp_path / ('project.json' if operation == 'save_as' else 'reading.txt')
    destination.write_text('Previously displayed destination', encoding='utf-8')
    dialog, messages, errors = _destination_dialog(controller, destination)
    before = controller.to_dict(), controller.project_path, controller._file_hash
    def confirm(parent, target):
        target.write_text('Another writer changed the destination during confirmation.', encoding='utf-8')
        return True
    monkeypatch.setattr(module, 'approve_export_destination',
        lambda parent, path, **kwargs: approve_export_destination(
            parent, path, confirm=confirm, **kwargs))
    result = getattr(module.AudioStudioDialog, operation)(dialog)
    assert destination.read_text(encoding='utf-8') == 'Another writer changed the destination during confirmation.'
    assert not messages and len(errors) == 1 and isinstance(errors[0], ValueError)
    assert (controller.to_dict(), controller.project_path, controller._file_hash) == before
    assert controller.dirty
    if operation == 'save_as':
        assert result is False


@pytest.mark.parametrize('operation', ['save_as', 'export_copy'])
def test_studio_new_destination_remains_absent_bound_after_picker(
        controller, tmp_path, monkeypatch, operation):
    import carr_library.ui.audio_studio as module
    from carr_library.ui.export_destination import approve_export_destination
    controller.new('Immutable source')
    controller.edit_script('Pending derivative')
    destination = tmp_path / ('project.json' if operation == 'save_as' else 'reading.txt')
    dialog, messages, errors = _destination_dialog(controller, destination)
    before = controller.to_dict(), controller.project_path, controller._file_hash
    def do_not_ask(parent, target):
        raise AssertionError('A new path does not need replacement confirmation.')
    def approve_then_external_write(parent, path, **kwargs):
        approval = approve_export_destination(parent, path, confirm=do_not_ask, **kwargs)
        assert approval.require_absent
        destination.write_text('Another writer appeared after selection.', encoding='utf-8')
        return approval
    monkeypatch.setattr(module, 'approve_export_destination', approve_then_external_write)
    getattr(module.AudioStudioDialog, operation)(dialog)
    assert destination.read_text(encoding='utf-8') == 'Another writer appeared after selection.'
    assert not messages and len(errors) == 1 and isinstance(errors[0], (ValueError, FileExistsError))
    assert (controller.to_dict(), controller.project_path, controller._file_hash) == before
    assert controller.dirty


def test_reading_copy_captures_before_hashing_an_existing_destination(
        controller, tmp_path, monkeypatch):
    import carr_library.ui.audio_studio as module
    controller.new('Immutable source')
    controller.edit_script('Pending derivative')
    destination = tmp_path / 'reading.txt'
    destination.write_text('Earlier destination', encoding='utf-8')
    original = module._path_digest
    def changed_before_hash(path, **kwargs):
        assert path == destination
        destination.write_text('Changed before the controller hashes the selected file.', encoding='utf-8')
        return original(path, **kwargs)
    monkeypatch.setattr(module, '_path_digest', changed_before_hash)
    with pytest.raises(ValueError, match='destination changed'):
        controller.export_reading_copy(destination, overwrite=True)
    assert destination.read_text(encoding='utf-8') == 'Changed before the controller hashes the selected file.'
    assert controller.draft.source_text == 'Immutable source' and controller.dirty


@pytest.mark.parametrize('operation', ['save_as', 'reading_copy'])
def test_studio_destination_approval_keeps_selected_parent_identity(
        controller, tmp_path, operation):
    from carr_library.research.export_file import capture_export_destination
    controller.new('Immutable source')
    controller.edit_script('Pending derivative')
    parent = tmp_path / 'chosen'
    parent.mkdir()
    destination = parent / ('project.json' if operation == 'save_as' else 'reading.txt')
    approval = capture_export_destination(destination, private_root=controller.private_root)
    parent.rename(tmp_path / 'original-chosen')
    parent.mkdir()
    action = controller.save if operation == 'save_as' else controller.export_reading_copy
    with pytest.raises(ValueError, match='destination changed'):
        action(destination, overwrite=True, approval=approval)
    assert not destination.exists()
    assert not (tmp_path / 'original-chosen' / destination.name).exists()
    assert controller.project_path is None and controller.dirty


@pytest.mark.parametrize(('save', 'text', 'expected_style'), [
    (True, True, 1), (True, False, 1), (False, True, 12),
], ids=['reading-copy', 'project-save-as', 'text-open'])
def test_studio_picker_only_selects_path_before_separate_replacement_question(
        tmp_path, save, text, expected_style):
    from carr_library.ui.audio_studio import AudioStudioDialog
    selected = tmp_path / ('reading.txt' if text else 'project.json')
    seen, destroyed = [], []
    class Picker:
        def __init__(self, *args, **kwargs):
            seen.append(kwargs)
        def ShowModal(self):
            return 100
        def GetPath(self):
            return str(selected)
        def Destroy(self):
            destroyed.append(True)
    wx = SimpleNamespace(
        FileDialog=Picker, FD_SAVE=1, FD_OVERWRITE_PROMPT=2,
        FD_OPEN=4, FD_FILE_MUST_EXIST=8, ID_OK=100)
    dialog = SimpleNamespace(wx=wx, dialog=object())
    assert AudioStudioDialog._file(dialog, 'Choose destination', save=save, text=text) == selected
    assert seen[0]['style'] == expected_style and destroyed == [True]


def _studio_executor_probe():
    """Use the real Studio job method and executor with explicit window boundaries."""
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext
    from queue import Queue
    import threading
    from unittest.mock import Mock
    from carr_library.ui.audio_studio import AudioStudioDialog

    class Control:
        def __init__(self, enabled=True):
            self.enabled = enabled
        def Disable(self):
            self.enabled = False
        def Enable(self, enabled=True):
            self.enabled = enabled

    dialog = object.__new__(AudioStudioDialog)
    dialog.busy = dialog.closed = dialog.close_pending = False
    dialog.cancelled = threading.Event()
    dialog.controls = [Control(), Control(False)]
    dialog.windows_playback_controls = [dialog.controls[1]]
    dialog.cancel_button = Control(False)
    dialog.playback = SimpleNamespace(native=None, stop=lambda: None)
    dialog.executor = ThreadPoolExecutor(max_workers=1)
    callbacks, errors, messages = Queue(), [], []
    dialog.wx = SimpleNamespace(CallAfter=callbacks.put)
    dialog._error, dialog._message = errors.append, messages.append
    dialog.operation_progress = SimpleNamespace(
        start=Mock(return_value=SimpleNamespace(token='synthetic-operation')),
        bind=Mock(side_effect=lambda token: nullcontext()),
        finish_result=Mock(), tick=Mock(), fail=Mock(), cancel=Mock())
    return dialog, callbacks, errors


def _assert_studio_controls_released(dialog):
    assert not dialog.busy
    assert dialog.controls[0].enabled
    assert not dialog.controls[1].enabled  # Windows-only controls remain unavailable here.
    assert not dialog.cancel_button.enabled


@pytest.mark.parametrize('boundary', ['start', 'finish_result', 'tick'])
def test_studio_observer_failure_does_not_lose_or_retry_a_real_committed_result(tmp_path, boundary):
    import os
    import threading
    from unittest.mock import Mock

    dialog, callbacks, errors = _studio_executor_probe()
    observer = dialog.operation_progress
    getattr(observer, boundary).side_effect = RuntimeError('Synthetic detached progress observer')
    target = tmp_path / 'committed-result.txt'
    result = {'state': 'complete', 'output': str(target)}
    calls, main_thread = [], threading.get_ident()

    def publish():
        calls.append(threading.get_ident())
        with target.open('x', encoding='utf-8') as stream:
            stream.write('Durable synthetic Studio result\n')
            stream.flush()
            os.fsync(stream.fileno())
        return result

    done = Mock()
    try:
        dialog._run('Save actual result', publish, done)
        assert dialog.busy
        callback = callbacks.get(timeout=10)
        assert target.read_text(encoding='utf-8') == 'Durable synthetic Studio result\n'
        callback()
        assert len(calls) == 1 and calls[0] != main_thread
        done.assert_called_once_with(result)
        assert errors == [] and callbacks.empty()
        observer.fail.assert_not_called()
        observer.cancel.assert_not_called()
        _assert_studio_controls_released(dialog)
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)


def test_studio_real_executor_rejection_restores_controls_and_preserves_admission_error(tmp_path):
    from unittest.mock import Mock

    dialog, callbacks, errors = _studio_executor_probe()
    dialog.executor.shutdown(wait=True)
    dialog.operation_progress.fail.side_effect = ValueError('Secondary observer failure')
    operation, done = Mock(), Mock()
    dialog._run('Rejected submission', operation, done)
    assert len(errors) == 1 and isinstance(errors[0], RuntimeError)
    assert 'cannot schedule new futures' in str(errors[0])
    assert not callbacks.qsize()
    operation.assert_not_called()
    done.assert_not_called()
    _assert_studio_controls_released(dialog)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('failure', [RuntimeError, InterruptedError], ids=['service-failure', 'cancelled'])
def test_studio_failure_observer_cannot_replace_the_real_worker_error(tmp_path, failure):
    import os
    from unittest.mock import Mock

    dialog, callbacks, errors = _studio_executor_probe()
    original = failure('Original synthetic worker outcome')
    observer = dialog.operation_progress
    observer.fail.side_effect = ValueError('Secondary failure observer')
    observer.cancel.side_effect = ValueError('Secondary cancellation observer')
    target = tmp_path / 'retained-checkpoint.txt'
    calls = []
    def stopped():
        calls.append(True)
        with target.open('x', encoding='utf-8') as stream:
            stream.write('Retained synthetic checkpoint\n')
            stream.flush()
            os.fsync(stream.fileno())
        raise original

    done = Mock()
    try:
        dialog._run('Checkpoint before stopping', stopped, done)
        callbacks.get(timeout=10)()
        assert calls == [True] and errors == [original]
        assert target.read_text(encoding='utf-8') == 'Retained synthetic checkpoint\n'
        done.assert_not_called()
        _assert_studio_controls_released(dialog)
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)


def _real_studio_progress():
    """Use the actual shared reporter/presenter without constructing native UI."""
    import time
    from unittest.mock import Mock
    from carr_library.progress import ProgressHub
    from carr_library.ui.operation_progress import OperationProgress

    presenter = object.__new__(OperationProgress)
    presenter.library = object()
    presenter.frame = SimpleNamespace(library=presenter.library)
    presenter.closed = False
    presenter.reporter = None
    presenter.clock = time.monotonic
    presenter.hub = ProgressHub()
    presenter.notifications = SimpleNamespace(clear=lambda: None)
    presenter.tick = Mock()
    return presenter


def _refuse_dead_studio_controls(dialog, monkeypatch):
    def unavailable(*args, **kwargs):
        pytest.fail('A detached Studio completion touched dead or newer controls.')
    for control in (*dialog.controls, dialog.cancel_button):
        monkeypatch.setattr(control, 'Enable', unavailable)
        monkeypatch.setattr(control, 'Disable', unavailable)
    dialog._error = dialog._message = unavailable
    dialog.close = unavailable


@pytest.mark.parametrize('outcome', ['complete', 'failed', 'speech-cancelled', 'dsp-cancelled', 'paused'])
def test_closed_studio_settles_exact_shared_task_without_touching_dead_controls(
        tmp_path, monkeypatch, outcome):
    import os
    from unittest.mock import Mock
    from carr_library.audio.jobs import RenderCancelled

    dialog, callbacks, errors = _studio_executor_probe()
    presenter = dialog.operation_progress = _real_studio_progress()
    artifact = tmp_path / 'actual-worker-checkpoint.txt'
    exception = {'failed': OSError('Synthetic receipt publication failure'),
                 'speech-cancelled': InterruptedError('Speech acknowledged stop'),
                 'dsp-cancelled': RenderCancelled('DSP acknowledged stop')}.get(outcome)
    result = {'state': 'paused' if outcome == 'paused' else 'complete',
              'output': str(artifact)}
    done, calls = Mock(), []
    def work():
        calls.append(True)
        with artifact.open('x', encoding='utf-8') as stream:
            stream.write('Actually retained before the real future returns.\n')
            stream.flush()
            os.fsync(stream.fileno())
        if exception is not None:
            raise exception
        return result
    try:
        dialog._run('Closed Studio task', work, done)
        callback = callbacks.get(timeout=10)
        reporter = presenter.reporter
        assert reporter.event.state == 'running' and artifact.is_file()
        dialog.closed = presenter.closed = True
        dialog.cancelled.set()  # A late stop cannot hide a real failure or commit.
        _refuse_dead_studio_controls(dialog, monkeypatch)
        callback()
        expected = ('cancelled' if outcome.endswith('cancelled') else outcome)
        assert reporter.event.state == expected
        assert reporter.event.verified is (outcome == 'complete')
        assert (reporter.event.percent == 100) is (outcome == 'complete')
        if outcome == 'failed':
            assert 'receipt publication failure' in reporter.event.message
        assert calls == [True] and artifact.read_text(encoding='utf-8').startswith('Actually retained')
        assert errors == []
        done.assert_not_called()
        presenter.tick.assert_not_called()
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)


def test_stale_studio_completion_settles_old_task_without_relabeling_new_work(tmp_path, monkeypatch):
    from unittest.mock import Mock

    dialog, callbacks, errors = _studio_executor_probe()
    presenter = dialog.operation_progress = _real_studio_progress()
    done = Mock()
    try:
        dialog._run('Previous task', lambda: {'state': 'complete'}, done)
        callback = callbacks.get(timeout=10)
        previous = presenter.reporter
        presenter.start('New independent task', total=3)
        current = presenter.reporter
        before = current.event
        dialog._job_token = object()
        dialog.cancelled.set()
        _refuse_dead_studio_controls(dialog, monkeypatch)
        callback()
        assert previous.event.state == 'complete' and previous.event.verified
        assert current.event == before and current.event.state == 'running'
        assert current.event.percent == 0 and not current.event.verified
        assert dialog.busy and errors == []
        done.assert_not_called()
        presenter.tick.assert_not_called()
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('kind', ['speech', 'dsp'])
def test_live_studio_uses_explicit_speech_and_dsp_cancellation_types(kind):
    from unittest.mock import Mock
    from carr_library.audio.jobs import RenderCancelled

    dialog, callbacks, errors = _studio_executor_probe()
    presenter = dialog.operation_progress = _real_studio_progress()
    failure = (InterruptedError if kind == 'speech' else RenderCancelled)('Actual typed stop')
    done = Mock()
    def stop():
        raise failure
    try:
        dialog._run('Typed cancellation', stop, done)
        callbacks.get(timeout=10)()
        assert presenter.reporter.event.state == 'cancelled'
        assert not presenter.reporter.event.verified
        assert presenter.reporter.event.percent is None
        assert errors == [failure]
        done.assert_not_called()
        _assert_studio_controls_released(dialog)
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)


def _completed_inspection_fixture(tmp_path):
    """Current receipt schema with real synthetic PCM; no model or runtime."""
    from carr_library.audio.models import digest
    controller = StudioController(private_root=tmp_path / 'private', registry=Registry())
    controller.private_root.mkdir()
    output = tmp_path / 'production'
    output.mkdir()
    script = ('[output sample_rate=8000 stems=false]\n# Inspection\n'
              'Narrator: Prepared synthetic source.\n[pause 3]\nNarrator: End.\n')
    controller.new(script, input_kind='script')
    controller.configure(voice='fixture:local', language='en', output_dir=output)
    controller.save(tmp_path / 'studio.json')
    plan, key = controller.plan(), 'a' * 64
    directory = output / 'renders' / key
    directory.mkdir(parents=True)
    with wave.open(str(directory / 'master.wav'), 'wb') as stream:
        stream.setparams((2, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b'\0\0\0\0' * 32000)
    (directory / 'subtitles.srt').write_text(
        '1\n00:00:00,000 --> 00:00:00,500\nSynthetic source.\n', encoding='utf-8')
    chapters = [{'id': 'synthetic-scene', 'sample_rate': 8000,
                 'start_frame': 0, 'end_frame': 32000}]
    timeline = {'schema_version': 1, 'sample_rate': 8000,
        'source_sha256': plan.source_sha256, 'chapters': chapters,
        'lines': [], 'scenes': [{'scene_id': 'synthetic-scene',
            'events': [{'kind': 'line', 'start_frame': 0, 'frames': 4000},
                       {'kind': 'pause', 'start_frame': 4000, 'frames': 24000},
                       {'kind': 'line', 'start_frame': 28000, 'frames': 4000}],
            'clips': []}]}
    (directory / 'timeline.json').write_text(json.dumps(timeline), encoding='utf-8')
    completion = {'schema_version': 1, 'status': 'complete', 'run_key': key,
        'source_sha256': plan.source_sha256, 'plan_sha256': digest(plan.to_dict()),
        'output_name': 'master.wav', 'stem_names': [], 'sample_rate': 8000,
        'frames': 32000, 'duration_seconds': 4.0, 'chapters': chapters,
        'files': [{'name': item.name, 'size': item.stat().st_size,
                   'sha256': hashlib.sha256(item.read_bytes()).hexdigest()}
                  for item in sorted(directory.iterdir())]}
    manifest = directory / 'complete.json'
    manifest.write_text(json.dumps(completion, sort_keys=True) + '\n', encoding='utf-8')
    controller.result = {**completion, 'manifest': str(manifest),
                         'output': str(directory / 'master.wav')}
    (output / 'render-journal.json').write_text(json.dumps({
        'schema_version': 1, 'state': 'complete', 'run_key': key,
        'source_sha256': plan.source_sha256, 'output': 'Z:/old-machine/master.wav'}),
        encoding='utf-8')
    return controller, manifest


def _inspection_bytes(root):
    return {item.relative_to(root).as_posix(): item.read_bytes()
            for item in root.rglob('*') if item.is_file()}


def test_completed_audio_inspection_is_real_read_only_and_keeps_render_result(tmp_path, monkeypatch):
    from carr_library.audio import production
    controller, manifest = _completed_inspection_fixture(tmp_path)
    before, rendered = _inspection_bytes(tmp_path), controller.result
    def no_new_work(*args, **kwargs):
        pytest.fail('Read-only inspection attempted rendering, voices or resources.')
    monkeypatch.setattr(production, 'render_project', no_new_work)
    monkeypatch.setattr(controller, '_registry', no_new_work)
    monkeypatch.setattr(controller, '_resource_broker', no_new_work)
    report = controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    assert report['status'] == 'complete' and report['review_status'] == 'warnings'
    assert report['interval_count'] == 1 and report['qualifying_frames'] == 32000
    assert report['intervals'][0]['annotations'] == {
        'authored_pause_overlap_frames': 24000, 'spoken_line_overlap_frames': 8000,
        'declared_music_clip_overlap_frames': 0}
    assert report['context']['background_music'] == 'not-recorded-in-timeline'
    text = production.completed_silence_report_text(report)
    assert 'Review warnings only' in text and 'Background scene music is unknown' in text
    assert report['completion_sha256'] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert controller.result is rendered and _inspection_bytes(tmp_path) == before


def test_reopened_studio_inspects_current_run_without_saved_absolute_output_authority(tmp_path, monkeypatch):
    from carr_library.audio import production
    controller, manifest = _completed_inspection_fixture(tmp_path)
    reopened = StudioController(private_root=controller.private_root, registry=Registry())
    reopened.open(controller.project_path)
    assert reopened.result is None
    before = _inspection_bytes(tmp_path)
    monkeypatch.setattr(production, 'render_project',
                        lambda *a, **k: pytest.fail('Reopening an inspection rendered audio.'))
    report = reopened.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    assert report['run_key'] == controller.result['run_key']
    assert report['completion_sha256'] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert reopened.result is None and _inspection_bytes(tmp_path) == before


@pytest.mark.parametrize('changed', ['source', 'voice', 'title'])
def test_completed_inspection_refuses_a_newer_source_or_plan_without_replacing_audio(tmp_path, changed):
    controller, _ = _completed_inspection_fixture(tmp_path)
    if changed == 'source':
        controller.edit_script(controller.script + 'Narrator: Later edit.\n')
    elif changed == 'voice':
        controller.configure(voice='fixture:other')
    else:
        controller.configure(title='Newer title')
    before, rendered, draft = _inspection_bytes(tmp_path), controller.result, controller.script
    with pytest.raises(ValueError, match='saved production plan and source'):
        controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    assert controller.result is rendered and controller.script == draft
    assert _inspection_bytes(tmp_path) == before


@pytest.mark.parametrize('changed', ['run-key', 'manifest', 'state', 'journal'])
def test_completed_inspection_refuses_an_unbound_selection_before_backend_work(tmp_path, monkeypatch, changed):
    from carr_library.audio import production
    controller, _ = _completed_inspection_fixture(tmp_path)
    if changed == 'run-key':
        controller.result['run_key'] = '../foreign'
    elif changed == 'manifest':
        controller.result['manifest'] = str(tmp_path / 'complete.json')
    elif changed == 'state':
        controller.result['status'] = 'running'
    else:
        controller.result = None
        (controller.output_dir / 'render-journal.json').write_text('[]', encoding='utf-8')
    before, rendered = _inspection_bytes(tmp_path), controller.result
    monkeypatch.setattr(production, 'inspect_completed_silence',
                        lambda *a, **k: pytest.fail('Unbound inspection reached the backend.'))
    with pytest.raises(ValueError):
        controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    assert controller.result is rendered and _inspection_bytes(tmp_path) == before


def test_completed_inspection_cancels_the_real_scan_and_preserves_the_old_result(tmp_path):
    import threading
    from carr_library.audio.jobs import RenderCancelled
    controller, _ = _completed_inspection_fixture(tmp_path)
    before, rendered, stop, phases = _inspection_bytes(tmp_path), controller.result, threading.Event(), []
    def status(message):
        phases.append(message)
        stop.set()
    with pytest.raises(RenderCancelled):
        controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3,
                                           cancelled=stop.is_set, on_status=status)
    assert phases and controller.result is rendered and _inspection_bytes(tmp_path) == before
    assert not controller._render_lock.locked()


def test_completed_inspection_never_steals_the_existing_render_lock(tmp_path):
    controller, _ = _completed_inspection_fixture(tmp_path)
    before, rendered = _inspection_bytes(tmp_path), controller.result
    controller._render_lock.acquire()
    try:
        with pytest.raises(ValueError, match='current Studio operation'):
            controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
        assert controller._render_lock.locked()
    finally:
        controller._render_lock.release()
    assert controller.result is rendered and _inspection_bytes(tmp_path) == before


@pytest.mark.parametrize('changed', ['draft', 'result', 'project'])
def test_real_inspection_stops_when_its_controller_binding_changes(tmp_path, changed):
    from carr_library.audio.jobs import RenderCancelled
    from carr_library.reader.narration_text import NarrationDraft
    controller, _ = _completed_inspection_fixture(tmp_path)
    before, changes = _inspection_bytes(tmp_path), []
    def status(message):
        if changes:
            return
        changes.append(message)
        if changed == 'draft':
            controller.draft = NarrationDraft('Later independent source.', source_id='other')
        elif changed == 'result':
            controller.result = {'status': 'complete', 'run_key': 'b' * 64}
        else:
            controller.project_id = 'c' * 32
    with pytest.raises(RenderCancelled):
        controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3, on_status=status)
    assert len(changes) == 1 and _inspection_bytes(tmp_path) == before
    assert not controller._render_lock.locked()


def test_inspection_report_export_retains_create_only_and_bound_approval(tmp_path):
    from carr_library.audio.production import completed_silence_report_text
    from carr_library.research.export_file import capture_export_destination
    controller, _ = _completed_inspection_fixture(tmp_path)
    report = controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    target = tmp_path / 'report.txt'
    before, rendered = _inspection_bytes(controller.output_dir), controller.result
    receipt = controller.export_inspection_report(report, target)
    assert receipt.path == target and target.read_text('utf-8') == completed_silence_report_text(report)
    target.write_text('Foreign later contents.', encoding='utf-8')
    with pytest.raises(FileExistsError):
        controller.export_inspection_report(report, target)
    approval = capture_export_destination(target, private_root=controller.private_root)
    target.write_text('Another writer after confirmation.', encoding='utf-8')
    with pytest.raises(ValueError, match='destination changed'):
        controller.export_inspection_report(report, target, approval=approval)
    assert target.read_text('utf-8') == 'Another writer after confirmation.'
    assert controller.result is rendered and _inspection_bytes(controller.output_dir) == before


@pytest.mark.parametrize('destination', ['master', 'manifest', 'output-plan', 'project', 'original', 'private'])
def test_inspection_report_export_cannot_modify_production_project_or_original(tmp_path, destination):
    controller, manifest = _completed_inspection_fixture(tmp_path)
    report = controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    original = tmp_path / 'original.txt'
    original.write_text('Immutable original.', encoding='utf-8')
    controller.source_reference = {'kind': 'local-text', 'original_path': str(original),
        'filename': original.name, 'bytes_sha256': hashlib.sha256(original.read_bytes()).hexdigest()}
    # The source reference is provenance, not a production-plan change.
    choices = {'master': manifest.parent / 'master.wav', 'manifest': manifest,
               'output-plan': controller.output_dir / 'inspection.txt',
               'project': controller.project_path, 'original': original,
               'private': controller.private_root / 'inspection.txt'}
    before, rendered = _inspection_bytes(tmp_path), controller.result
    with pytest.raises(ValueError):
        controller.export_inspection_report(report, choices[destination])
    assert controller.result is rendered and _inspection_bytes(tmp_path) == before


@pytest.mark.parametrize('changed', ['manifest', 'cancel'])
def test_inspection_export_keeps_previous_destination_after_change_or_cancel(tmp_path, changed):
    from carr_library.audio.jobs import RenderCancelled
    from carr_library.research.export_file import capture_export_destination
    controller, manifest = _completed_inspection_fixture(tmp_path)
    report = controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    target = tmp_path / 'review.txt'
    target.write_text('Retain previous report.', encoding='utf-8')
    approval = capture_export_destination(target, private_root=controller.private_root)
    if changed == 'manifest':
        manifest.write_bytes(manifest.read_bytes() + b'\n')
    before = _inspection_bytes(tmp_path)
    with pytest.raises(ValueError if changed == 'manifest' else RenderCancelled):
        controller.export_inspection_report(report, target, approval=approval,
                                            cancelled=lambda: changed == 'cancel')
    assert target.read_text('utf-8') == 'Retain previous report.'
    assert _inspection_bytes(tmp_path) == before


@pytest.mark.parametrize('threshold,minimum', [('nan', '3'), ('-121', '3'), ('-50', '0'), (True, '3'), (10 ** 400, '3')])
def test_native_inspection_criteria_reject_invalid_or_implicit_numeric_values(threshold, minimum):
    from carr_library.ui.audio_studio import _inspection_criteria_values
    with pytest.raises(ValueError):
        _inspection_criteria_values(threshold, minimum)


def test_native_inspection_criteria_use_explicit_units_and_accept_finite_boundaries():
    from carr_library.ui.audio_studio import _inspection_criteria_values
    assert _inspection_criteria_values('-120', '0.001') == (-120.0, 0.001)
    assert _inspection_criteria_values('0', '86400') == (0.0, 86400.0)


@pytest.mark.parametrize('member', ['master.wav', 'timeline.json'])
def test_inspection_export_refuses_changed_media_even_with_unchanged_completion(tmp_path, member):
    controller, manifest = _completed_inspection_fixture(tmp_path)
    report = controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    completion_bytes = manifest.read_bytes()
    source = manifest.parent / member
    original = source.read_bytes()
    source.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    before = _inspection_bytes(tmp_path)
    target = tmp_path / 'stale-report.txt'
    with pytest.raises(ValueError, match='changed after inspection'):
        controller.export_inspection_report(report, target)
    assert manifest.read_bytes() == completion_bytes
    assert not target.exists() and _inspection_bytes(tmp_path) == before
    assert not controller._render_lock.locked()


def test_inspection_export_holds_all_real_readers_through_commit_and_keeps_late_cleanup_visible(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import carr_library.audio.jobs as jobs
    import carr_library.ui.audio_studio as studio
    controller, manifest = _completed_inspection_fixture(tmp_path)
    report = controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    real_reader, real_write = jobs.verified_reader, studio.write_export
    active, publication = {}, []
    @contextmanager
    def observed_reader(path, **kwargs):
        with real_reader(path, **kwargs) as stream:
            name = Path(path).name
            active[name] = active.get(name, 0) + 1
            try:
                yield stream
            finally:
                active[name] -= 1
        if Path(path) == manifest and publication:
            raise OSError('Synthetic final reader cleanup failure after actual commit')
    def publish(path, text, **kwargs):
        assert {name for name, count in active.items() if count} == {
            'complete.json', 'master.wav', 'timeline.json'}
        assert controller._render_lock.locked()
        receipt = real_write(path, text, **kwargs)
        publication.append(receipt)
        return receipt
    monkeypatch.setattr(jobs, 'verified_reader', observed_reader)
    monkeypatch.setattr(studio, 'write_export', publish)
    target = tmp_path / 'committed-inspection.txt'
    before, rendered = _inspection_bytes(controller.output_dir), controller.result
    receipt = controller.export_inspection_report(report, target)
    assert len(publication) == 1 and receipt.path == target and target.is_file()
    assert 'Report saved' in receipt.cleanup_warning
    assert 'Synthetic final reader cleanup failure' in receipt.cleanup_warning
    assert not any(active.values()) and not controller._render_lock.locked()
    assert controller.result is rendered and _inspection_bytes(controller.output_dir) == before


def _inspection_dialog_probe(controller):
    dialog, callbacks, errors = _studio_executor_probe()
    class Value:
        def __init__(self, value):
            self.value = value
        def GetValue(self):
            return self.value
    class Choice:
        def __init__(self, value):
            self.value = value
        def GetSelection(self):
            return self.value
    dialog.controller = controller
    dialog.parent = SimpleNamespace(library=object())
    dialog.operation_progress.frame = dialog.parent
    dialog._script_dirty = False
    dialog.title, dialog.language = Value(controller.title), Value(controller.language)
    dialog.script, dialog.output = Value(controller.script), Value(str(controller.output_dir))
    dialog.input_kind, dialog.voice = Choice(1), Choice(0)
    dialog.voice_rows = [{'id': controller.voice}]
    dialog._inspection_criteria = (-50, 3)
    dialog._choose_inspection_criteria = lambda current: (-50, 3) if current() else None
    messages, reports = [], []
    dialog._message = lambda message, **kwargs: messages.append(str(message))
    dialog._show_inspection_report = lambda report, current, owned: reports.append(
        (report, current(), owned()))
    return dialog, callbacks, errors, messages, reports


def _settle_inspection_dialog(dialog, callbacks):
    import time
    from queue import Empty
    deadline = time.monotonic() + 10
    while dialog.busy:
        if time.monotonic() >= deadline:
            pytest.fail('The actual Studio inspection worker did not settle.')
        try:
            callbacks.get(timeout=0.05)()
        except Empty:
            pass
    while not callbacks.empty():
        callbacks.get_nowait()()


def test_studio_inspection_dispatches_real_worker_and_displays_only_its_retained_report(tmp_path, monkeypatch):
    import threading
    import carr_library.audio.production as production
    controller, _ = _completed_inspection_fixture(tmp_path)
    dialog, callbacks, errors, messages, reports = _inspection_dialog_probe(controller)
    real, threads = production.inspect_completed_silence, []
    def measured(*args, **kwargs):
        threads.append(threading.get_ident())
        return real(*args, **kwargs)
    monkeypatch.setattr(production, 'inspect_completed_silence', measured)
    before, rendered, owner = _inspection_bytes(tmp_path), controller.result, threading.get_ident()
    try:
        dialog.inspect_completed_audio()
        _settle_inspection_dialog(dialog, callbacks)
        assert threads and len(threads) == 1 and threads[0] != owner
        assert len(reports) == 1 and reports[0][1:] == (True, True)
        assert reports[0][0]['interval_count'] == 1
        assert errors == [] and any('unchanged' in message for message in messages)
        assert controller.result is rendered and _inspection_bytes(tmp_path) == before
        _assert_studio_controls_released(dialog)
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('pending', ['script', 'title'])
def test_studio_inspection_refuses_unapplied_native_edits_without_capturing_or_rendering(tmp_path, pending):
    controller, _ = _completed_inspection_fixture(tmp_path)
    dialog, callbacks, errors, messages, reports = _inspection_dialog_probe(controller)
    before, rendered = _inspection_bytes(tmp_path), controller.result
    if pending == 'script':
        dialog._script_dirty = True
    else:
        dialog.title.value = 'Unapplied native edit'
    dialog._choose_inspection_criteria = lambda current: pytest.fail('Pending edits reached the criteria dialog')
    try:
        dialog.inspect_completed_audio()
        assert len(errors) == 1 and 'Save or revert pending' in str(errors[0])
        assert not dialog.busy and callbacks.empty() and not reports
        assert controller.result is rendered and _inspection_bytes(tmp_path) == before
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)


def test_studio_inspection_cancelled_setup_starts_no_worker_and_keeps_previous_result(tmp_path):
    controller, _ = _completed_inspection_fixture(tmp_path)
    dialog, callbacks, errors, messages, reports = _inspection_dialog_probe(controller)
    before, rendered = _inspection_bytes(tmp_path), controller.result
    dialog._choose_inspection_criteria = lambda current: None
    dialog.executor.shutdown(wait=True)
    dialog.inspect_completed_audio()
    assert errors == [] and callbacks.empty() and not dialog.busy and not reports
    assert controller.result is rendered and _inspection_bytes(tmp_path) == before


@pytest.mark.parametrize('boundary', ['cancel', 'library'])
def test_running_native_inspection_reaches_real_cancellation_when_current_owner_changes(tmp_path, monkeypatch, boundary):
    import threading
    from carr_library.audio.jobs import RenderCancelled
    import carr_library.audio.production as production
    controller, _ = _completed_inspection_fixture(tmp_path)
    dialog, callbacks, errors, messages, reports = _inspection_dialog_probe(controller)
    real, entered, release = production.inspect_completed_silence, threading.Event(), threading.Event()
    def paused(*args, **kwargs):
        status = kwargs['on_status']
        def barrier(message):
            if not entered.is_set():
                entered.set()
                assert release.wait(10)
            status(message)
        return real(*args, **{**kwargs, 'on_status': barrier})
    monkeypatch.setattr(production, 'inspect_completed_silence', paused)
    before, rendered = _inspection_bytes(tmp_path), controller.result
    try:
        dialog.inspect_completed_audio()
        assert entered.wait(10) and dialog.busy
        if boundary == 'cancel':
            dialog.cancelled.set()
        else:
            dialog.parent.library = object()
        release.set()
        _settle_inspection_dialog(dialog, callbacks)
        assert len(errors) == 1 and isinstance(errors[0], RenderCancelled)
        assert not reports and controller.result is rendered
        assert _inspection_bytes(tmp_path) == before and not controller._render_lock.locked()
        _assert_studio_controls_released(dialog)
    finally:
        release.set()
        dialog.executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('boundary', ['widgets', 'result', 'library'])
def test_studio_late_inspection_completion_cannot_replace_a_newer_owner(tmp_path, boundary):
    controller, _ = _completed_inspection_fixture(tmp_path)
    dialog, callbacks, errors, messages, reports = _inspection_dialog_probe(controller)
    before, rendered = _inspection_bytes(tmp_path), controller.result
    try:
        dialog.inspect_completed_audio()
        dialog.executor.submit(lambda: None).result(timeout=10)
        assert dialog.busy  # The real worker finished; its UI callback is still pending.
        if boundary == 'widgets':
            dialog.title.value = 'Later visible edit'
        elif boundary == 'result':
            controller.result = {**rendered, 'newer_owner': True}
        else:
            dialog.parent.library = object()
        _settle_inspection_dialog(dialog, callbacks)
        assert not reports and len(errors) == 1 and 'late inspection' in str(errors[0])
        assert _inspection_bytes(tmp_path) == before
        if boundary != 'result':
            assert controller.result is rendered
        else:
            assert controller.result['newer_owner'] is True
        _assert_studio_controls_released(dialog)
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)


def test_studio_inspection_export_runs_verification_and_publication_on_existing_worker(tmp_path, monkeypatch):
    import threading
    import carr_library.ui.audio_studio as studio
    from carr_library.research.export_file import capture_export_destination
    controller, _ = _completed_inspection_fixture(tmp_path)
    report = controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    dialog, callbacks, errors, messages, reports = _inspection_dialog_probe(controller)
    target, owner, workers = tmp_path / 'native-review.txt', threading.get_ident(), []
    dialog._inspection_report_path = lambda parent: target
    approval = capture_export_destination(target, private_root=controller.private_root)
    monkeypatch.setattr(studio, 'approve_export_destination', lambda *args, **kwargs: approval)
    real = controller.export_inspection_report
    def publish(*args, **kwargs):
        workers.append(threading.get_ident())
        return real(*args, **kwargs)
    monkeypatch.setattr(controller, 'export_inspection_report', publish)
    before, rendered = _inspection_bytes(controller.output_dir), controller.result
    try:
        request = dialog._export_inspection_report(report, object(), lambda: True)
        assert request == (target, approval) and workers == [] and not target.exists()
        dialog._publish_inspection_report(report, request, lambda: True, lambda: True)
        _settle_inspection_dialog(dialog, callbacks)
        assert workers and len(workers) == 1 and workers[0] != owner and target.is_file()
        assert errors == [] and any('Inspection report saved:' in message for message in messages)
        assert controller.result is rendered and _inspection_bytes(controller.output_dir) == before
        _assert_studio_controls_released(dialog)
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('boundary', ['picker', 'approval'])
def test_studio_inspection_export_rechecks_current_owner_after_each_native_boundary(tmp_path, monkeypatch, boundary):
    import carr_library.ui.audio_studio as studio
    from carr_library.research.export_file import capture_export_destination
    controller, _ = _completed_inspection_fixture(tmp_path)
    report = controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    dialog, callbacks, errors, messages, reports = _inspection_dialog_probe(controller)
    target, alive, approvals = tmp_path / 'refused-report.txt', [True], []
    def picker(parent):
        if boundary == 'picker':
            alive[0] = False
        return target
    def approve(*args, **kwargs):
        approvals.append(True)
        alive[0] = False
        return capture_export_destination(target, private_root=controller.private_root)
    dialog._inspection_report_path = picker
    monkeypatch.setattr(studio, 'approve_export_destination', approve)
    try:
        with pytest.raises(ValueError, match='changed'):
            dialog._export_inspection_report(report, object(), lambda: alive[0])
        assert approvals == ([] if boundary == 'picker' else [True])
        assert not target.exists() and callbacks.empty() and not dialog.busy
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)


def test_studio_inspection_export_refuses_owned_audio_before_native_replacement_approval(tmp_path, monkeypatch):
    import carr_library.ui.audio_studio as studio
    controller, manifest = _completed_inspection_fixture(tmp_path)
    report = controller.inspect_completed_audio(threshold_dbfs=-50, minimum_seconds=3)
    dialog, callbacks, errors, messages, reports = _inspection_dialog_probe(controller)
    dialog._inspection_report_path = lambda parent: manifest
    monkeypatch.setattr(studio, 'approve_export_destination',
        lambda *args, **kwargs: pytest.fail('An owned production file reached replacement approval'))
    before = _inspection_bytes(tmp_path)
    try:
        with pytest.raises(ValueError, match='outside the production'):
            dialog._export_inspection_report(report, object(), lambda: True)
        assert _inspection_bytes(tmp_path) == before and callbacks.empty()
    finally:
        dialog.executor.shutdown(wait=True, cancel_futures=True)
