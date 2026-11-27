"""Real host process budgets and typed restore boundaries; synthetic media only."""
from hashlib import sha256
import importlib.util
from pathlib import Path
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.durable_state import atomic_json, read_json
from carr_library.library_backup import restore_backup
from carr_library.transcription_host import AudioBudget, run_process

_spec = importlib.util.spec_from_file_location('supplied_worker_protocol_for_host',
    Path(__file__).with_name('test_supplied_worker_bridge.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
prepared = _fixture.prepared


def test_stopped_receipt_deferral_respects_existing_monotonic_progress():
    from carr_library.progress import ProgressHub
    from carr_library.transcription_host import report_progress
    task = ProgressHub().start('Transcription', total=2)
    report_progress(task, 1, 2, 'running')
    report_progress(task, 0, 2, 'stopped')
    assert task.event.completed == 1
    assert task.event.phase == 'stopped'
    assert not task.event.terminal


@pytest.mark.parametrize('code,budget,message', [
    ("from pathlib import Path; Path('large.bin').write_bytes(b'x'*200)",
     AudioBudget(max_output_bytes=64), 'disk budget'),
    ("from pathlib import Path; Path('one').touch(); Path('two').touch()",
     AudioBudget(max_staging_files=1), 'file budget'),
])
def test_actual_fast_child_cannot_evade_final_staging_budget(tmp_path, code, budget, message):
    with pytest.raises(RuntimeError, match=message):
        run_process([str(Path(sys.executable).resolve()), '-I', '-B', '-c', code],
                    cwd=tmp_path, watch=(tmp_path,), budget=budget)


def test_ready_job_backup_restores_retained_media_without_engine_execution(prepared, tmp_path):
    app, workspace, service, original, _ = prepared
    identity = service.submit([original])
    expected = original.read_bytes()
    worker = Path(service.configuration()['worker'])
    worker.unlink()
    checksum = app.backup(tmp_path / 'backup')
    original.unlink()
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', expected_manifest_sha256=checksum))
    recovered = restored.transcription(workspace)
    job = recovered.get(identity)
    assert job['status'] == 'ready'
    assert (recovered._path(identity) / 'input' / job['sources'][0]['filename']).read_bytes() == expected
    assert not worker.exists()
    assert recovered.configuration()['inference_qualified'] is False


@pytest.mark.parametrize('field', ['source_sha256', 'recognition_identity', 'original_text_sha256'])
def test_foreign_edit_binding_blocks_backup_without_publication(prepared, tmp_path, field):
    app, _, service, _, _ = prepared
    identity, job = _fixture.completed(prepared)
    revision = service.save_edit(identity, job['sources'][0]['filename'], 'User edit.')
    path = service._path(identity) / 'edits' / (revision + '.json')
    value = read_json(path)
    value[field] = '0' * 64
    atomic_json(path, value, replace=True)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='source binding'):
        app.backup(destination)
    assert not destination.exists()


def test_unknown_transcription_state_cannot_disappear_from_backup(prepared, tmp_path):
    app, _, service, _, _ = prepared
    path = service.root / 'unrecognized.bin'
    path.write_bytes(b'Required unknown data must not be omitted.')
    with pytest.raises(ValueError, match='Unknown transcription'):
        app.backup(tmp_path / 'rejected')
    assert path.read_bytes() == b'Required unknown data must not be omitted.'
    assert not (tmp_path / 'rejected').exists()


def test_completed_backup_keeps_engine_receipts_and_omits_only_declared_cache(prepared, tmp_path):
    app, workspace, service, _, _ = prepared
    identity, job = _fixture.completed(prepared)
    cache = service._path(identity) / 'logs' / 'cache'
    cache.mkdir(parents=True)
    (cache / 'rebuildable.json').write_text('{}', encoding='utf-8')
    receipt = service._path(identity) / job['receipts'][0]
    expected = sha256(receipt.read_bytes()).hexdigest()
    digest = app.backup(tmp_path / 'backup')
    recovered = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', expected_manifest_sha256=digest)).transcription(workspace)
    assert recovered.verify(identity)['status'] == 'completed'
    assert sha256((recovered._path(identity) / job['receipts'][0]).read_bytes()).hexdigest() == expected
    assert not (recovered._path(identity) / 'logs').exists()
    assert (cache / 'rebuildable.json').read_text(encoding='utf-8') == '{}'


def test_running_job_backup_requires_stop_without_losing_originals(prepared, tmp_path):
    app, _, service, original, _ = prepared
    identity = service.submit([original])
    job = service.get(identity)
    job['status'] = 'running'
    atomic_json(service._path(identity) / 'job.json', job, replace=True)
    with pytest.raises(ValueError, match='Stop transcription'):
        app.backup(tmp_path / 'rejected')
    assert original.exists() and not (tmp_path / 'rejected').exists()


def test_escaped_edit_roundtrip_uses_serialized_json_limit(prepared, tmp_path):
    app, workspace, service, _, _ = prepared
    identity, job = _fixture.completed(prepared)
    text = '\\' * (9 * 1024**2)
    revision = service.save_edit(identity, job['sources'][0]['filename'], text)
    path = service._path(identity) / 'edits' / (revision + '.json')
    assert len(text.encode('utf-8')) < 16 * 1024**2
    assert 16 * 1024**2 < path.stat().st_size < 32 * 1024**2
    digest = app.backup(tmp_path / 'backup')
    recovered = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', expected_manifest_sha256=digest)).transcription(workspace)
    assert recovered.edits(identity)[0]['text'] == text
    assert recovered.verify(identity)['status'] == 'completed'


@pytest.mark.parametrize('value', [None, 17, {}], ids=['null', 'number', 'object'])
def test_malformed_engine_checksum_is_a_typed_backup_error(prepared, tmp_path, value):
    app, _, service, _, _ = prepared
    configuration = service.configuration()
    configuration['worker_sha256'] = value
    atomic_json(service.root / 'engine.json', configuration, replace=True)
    with pytest.raises(ValueError, match='engine configuration'):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_shared_reporter_binds_actual_child_until_verified_completion(prepared):
    from carr_library.progress import ProgressHub, current_progress
    from carr_library.transcription_host import observe_operation, report_progress, require_completed
    service, original = prepared[2:4]
    identity = service.submit([original])
    task = ProgressHub().start('Transcription', total=1)
    def operation():
        assert current_progress() is task
        return require_completed(service.run(identity, timeout=30,
            progress=lambda done, total, phase: report_progress(task, done, total, phase)))
    result = observe_operation(task, operation)
    assert result == service.get(identity) and result['status'] == 'completed'
    assert current_progress() is None
    assert task.event.state == 'complete' and task.event.verified
    assert task.event.percent == 100


@pytest.mark.parametrize('kind, expected', [
    ('interrupted', 'cancelled'), ('audio_cancelled', 'cancelled'), ('failure', 'failed'),
])
def test_shared_reporter_restores_context_for_cancelled_and_failed_operations(kind, expected):
    from carr_library.progress import ProgressHub, current_progress
    from carr_library.transcription_host import AudioCancelled, observe_operation
    error_type = {'interrupted': InterruptedError, 'audio_cancelled': AudioCancelled,
                  'failure': RuntimeError}[kind]
    parent = ProgressHub().start('Unrelated parent')
    task = parent.child('Transcription')
    error = error_type('Purpose-written boundary failure.')
    def operation():
        assert current_progress() is task
        raise error
    with parent.bind():
        with pytest.raises(error_type) as caught:
            observe_operation(task, operation)
        assert caught.value is error
        assert current_progress() is parent
    assert current_progress() is None
    assert task.event.state == expected and task.event.percent is None


def test_terminal_observer_failure_cannot_reverse_a_committed_actual_child(prepared, monkeypatch):
    from carr_library import progress
    from carr_library.transcription_host import observe_operation, require_completed
    service, original = prepared[2:4]
    identity = service.submit([original])
    task = progress.ProgressHub().start('Transcription')
    calls = []
    def failed_observer(*args, **kwargs):
        raise RuntimeError('Synthetic detached progress observer.')
    monkeypatch.setattr(progress, 'finish_result', failed_observer)
    def operation():
        calls.append(identity)
        return require_completed(service.run(identity, timeout=30))
    result = observe_operation(task, operation)
    assert calls == [identity]
    assert result == service.get(identity) and result['status'] == 'completed'
    assert service.verify(identity)['status'] == 'completed'
    assert progress.current_progress() is None


def test_new_json_publication_preserves_concurrent_destination(tmp_path, monkeypatch):
    from carr_library import durable_state
    target = tmp_path / 'new-state.json'
    publish = durable_state.publish_directory_no_replace
    def concurrent(stage, destination):
        Path(destination).write_bytes(b'Already owned by another operation.')
        return publish(stage, destination)
    monkeypatch.setattr(durable_state, 'publish_directory_no_replace', concurrent)
    with pytest.raises(FileExistsError):
        durable_state.atomic_json(target, {'version': 1})
    assert target.read_bytes() == b'Already owned by another operation.'
    assert not list(tmp_path.glob('.pending-*'))


def test_submit_holds_engine_lock_before_creating_a_workspace_namespace(prepared):
    from carr_library.durable_state import state_lock
    _, _, service, original, _ = prepared
    assert not service.jobs_root.exists()
    with state_lock(service.root, 'engine'):
        with pytest.raises(RuntimeError, match='already being changed'):
            service.submit([original])
    assert not service.jobs_root.exists()
    assert original.exists()


@pytest.mark.parametrize('manager_installed', [False, True], ids=['before-keyboard', 'after-keyboard'])
def test_transcription_menu_preserves_semantic_shortcut_and_installs_once(tmp_path, monkeypatch, manager_installed):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from carr_library.keyboard_preferences import KeymapStore
    from carr_library.ui import keyboard_settings, transcription_dialog
    from carr_library.ui.builtin_keyboard_commands import builtin_commands
    from carr_library.ui.command_registry import CommandRegistry, CommandSpec
    # Reuse the existing explicit native-menu boundaries; no simulated OS dispatch.
    path = Path(__file__).parents[1] / 'ui' / 'test_keyboard_manager.py'
    spec = importlib.util.spec_from_file_location('asr_keyboard_boundaries', path)
    boundaries = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boundaries)
    frame = boundaries.Frame()
    file_menu = boundaries.Menu()
    frame.menu = boundaries.DynamicMenuBar([
        ('&Library', boundaries.Menu(frame.items), True), ('&File', file_menu, True)])
    frame.menu_bar = frame.menu
    frame.library = SimpleNamespace(root=tmp_path)
    frame.Bind = Mock()
    fake_wx = SimpleNamespace(Menu=boundaries.Menu, ID_ANY=-1, EVT_MENU=object(),
                              CallAfter=lambda callback, *args: callback(*args))
    monkeypatch.setitem(sys.modules, 'wx', fake_wx)
    monkeypatch.setattr(keyboard_settings, 'accelerator_tuple',
                        lambda key, identifier: (0, 0, identifier))
    command = CommandSpec('transcription.local', 'Transcription: local audio and video', 'main')
    store = KeymapStore(tmp_path / 'keyboard-shortcuts.json')
    expected = {'transcription.local': 'Ctrl+Alt+Shift+F8'}
    store.save(expected, CommandRegistry((*builtin_commands(), command)))
    original = store.path.read_bytes()
    if manager_installed:
        keyboard_settings.install_keyboard_settings(frame)
    opened = Mock()
    monkeypatch.setattr(transcription_dialog, 'show_transcription', opened)
    actions = transcription_dialog.install_transcription_actions(frame)
    assert transcription_dialog.install_transcription_actions(frame) is actions
    manager = keyboard_settings.install_keyboard_settings(frame)
    assert len(file_menu.items) == 1
    assert manager.registry.get('transcription.local') == command
    assert command.default_shortcut == ''
    assert manager.shortcut('transcription.local') == expected['transcription.local']
    assert manager.available('transcription.local')
    assert store.path.read_bytes() == original
    assert not getattr(frame, '_pending_keyboard_commands', [])
    opened.assert_not_called()
    actions.open()
    opened.assert_called_once_with(frame)


def _transcription_native_callbacks(frame, controls, cancel, report):
    """Execute the installed dialog's real closures with explicit widget boundaries."""
    import ast
    from carr_library.ui import transcription_dialog
    path = Path(transcription_dialog.__file__)
    tree = ast.parse(path.read_text(encoding='utf-8'))
    show = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                and node.name == 'show_transcription')
    begin = next(node for node in show.body if isinstance(node, ast.FunctionDef)
                 and node.name == 'begin')
    factory = ast.parse("""
def callbacks(frame, controls, cancel, report):
    alive, busy = True, False
    def can_replace():
        return True
    return begin, lambda: busy
""").body[0]
    factory.body.insert(-1, begin)
    namespace = {'__name__': 'carr_library.ui._transcription_callback_probe',
                 '__package__': 'carr_library.ui'}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[])),
                 str(path), 'exec'), namespace)
    return namespace['callbacks'](frame, controls, cancel, report)


@pytest.mark.parametrize('failure_mode', ['failed-callback', 'raised'])
def test_native_submission_failure_closes_progress_and_preserves_error(failure_mode):
    from threading import Event
    from types import SimpleNamespace
    from unittest.mock import Mock
    error = RuntimeError('Synthetic executor submission failure.')
    operation, completed, report, controls = Mock(), Mock(), Mock(), Mock()
    def run_job(label, task, done, failed):
        if failure_mode == 'raised':
            raise error
        failed(error)
        return None  # Exact current DesktopFrame contract, not False.
    frame = SimpleNamespace(working=False, _closing=False, run_job=run_job)
    begin, is_busy = _transcription_native_callbacks(frame, controls, Event(), report)
    assert begin('Transcription', operation, completed) is False
    assert is_busy() is False
    event, = frame.progress_hub.snapshot()
    assert event.state == 'failed' and event.terminal
    report.assert_called_once_with(error)
    operation.assert_not_called()
    completed.assert_not_called()


def test_late_native_failed_callback_preserves_verified_actual_result(prepared):
    from threading import Event
    from types import SimpleNamespace
    from unittest.mock import Mock
    from carr_library.progress import current_progress
    from carr_library.transcription_host import require_completed
    service, original = prepared[2:4]
    identity = service.submit([original])
    error = RuntimeError('Synthetic detached UI callback after committed output.')
    completed, report = Mock(), Mock()
    def run_job(label, operation, done, failed):
        result = operation()
        done(result)
        failed(error)
        return None
    frame = SimpleNamespace(working=False, _closing=False, run_job=run_job)
    begin, is_busy = _transcription_native_callbacks(frame, Mock(), Event(), report)
    begin('Transcription', lambda: require_completed(service.run(identity, timeout=30)), completed)
    assert is_busy() is False and current_progress() is None
    event, = frame.progress_hub.snapshot()
    assert event.state == 'complete' and event.verified
    assert service.verify(identity)['status'] == 'completed'
    completed.assert_called_once_with(service.get(identity))
    report.assert_called_once_with(error)
