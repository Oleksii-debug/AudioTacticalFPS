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


def _current_progress_host():
    # Load only the shared test boundary. Its job methods are AST-extracted from
    # the actually imported source/installed host, not copied implementation.
    path = Path(__file__).parents[1] / 'ui' / 'test_progress_view.py'
    spec = importlib.util.spec_from_file_location('asr_current_progress_host', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('failure_mode', ['failed-callback', 'raised'])
def test_native_submission_failure_closes_progress_and_preserves_error(failure_mode, monkeypatch):
    from threading import Event
    from unittest.mock import Mock
    helper = _current_progress_host()
    frame, callbacks = helper._host_frame()
    error = RuntimeError('Synthetic executor submission failure.')
    operation, completed, report, controls = Mock(), Mock(), Mock(), Mock()
    def rejected(*args, **kwargs):
        raise error
    if failure_mode == 'raised':
        monkeypatch.setattr(frame, 'run_job', rejected)  # Rejected before host admission.
    else:
        monkeypatch.setattr(frame.executor, 'submit', rejected)  # Actual host callback/None contract.
    try:
        begin, is_busy = _transcription_native_callbacks(frame, controls, Event(), report)
        assert begin('Transcription', operation, completed) is False
        assert is_busy() is False
        hub = getattr(frame, 'progress_hub', None)
        events = hub.snapshot() if hub is not None else ()
        assert len(events) == (1 if failure_mode == 'failed-callback' else 0)
        assert all(event.state == 'failed' and event.terminal for event in events)
        report.assert_called_once_with(error)
        operation.assert_not_called()
        completed.assert_not_called()
        assert not callbacks
    finally:
        frame.executor.shutdown(wait=True, cancel_futures=True)


def test_late_native_failed_callback_preserves_verified_actual_result(prepared):
    from threading import Event
    from unittest.mock import Mock
    from carr_library.progress import current_progress
    from carr_library.transcription_host import require_completed, report_progress
    service, original = prepared[2:4]
    identity = service.submit([original])
    error = RuntimeError('Synthetic detached UI callback after committed output.')
    completed, report = Mock(), Mock()
    helper = _current_progress_host()
    frame, callbacks = helper._host_frame()
    original_runner = frame.run_job
    late = []
    def run_job(label, operation, done, failed, **kwargs):
        late.append(failed)
        return original_runner(label, operation, done, failed, **kwargs)
    frame.run_job = run_job
    def operation():
        reporter = current_progress()
        assert reporter is not None
        return require_completed(service.run(identity, timeout=30,
            progress=lambda done, total, phase: report_progress(reporter, done, total, phase)))
    try:
        begin, is_busy = _transcription_native_callbacks(frame, Mock(), Event(), report)
        begin('Transcription', operation, completed)
        helper._drain(frame, callbacks)
        late[0](error)
        assert is_busy() is False and current_progress() is None
        event, = frame.progress_hub.snapshot()  # One actual operation, no ASR-owned duplicate.
        assert event.state == 'complete' and event.verified and event.unit == 'files'
        assert service.verify(identity)['status'] == 'completed'
        completed.assert_called_once_with(service.get(identity))
        report.assert_called_once_with(error)
    finally:
        frame.executor.shutdown(wait=True, cancel_futures=True)

def test_live_worker_status_reader_allows_atomic_replacement(tmp_path, monkeypatch):
    from carr_library import transcription, transcription_host
    status = tmp_path / 'job_status.json'
    stage = tmp_path / 'status.tmp'
    old = b'{"state":"running","done":0}\n'
    new = b'{"state":"finished","done":1}\n'
    status.write_bytes(old)
    stage.write_bytes(new)
    worker = tmp_path / 'status-worker.py'
    worker.write_text("import os\nos.replace('status.tmp', 'job_status.json')\n", encoding='utf-8')
    actual_open = transcription_host._open_worker_status

    def replace_after_open(path):
        stream = actual_open(path)
        try:
            # Exercise the approved worker's status-only Windows replacement
            # while the host still owns its old snapshot, not a parent-only
            # MoveFileEx call that bypasses the worker bootstrap.
            run_process([str(Path(sys.executable).absolute()), '-I', '-S', '-B', '-c',
                         transcription_host._SITE_BOOTSTRAP + transcription._WORKER_BOOTSTRAP,
                         str(worker), str(tmp_path)],
                        cwd=tmp_path, watch=(tmp_path,), budget=AudioBudget(timeout=10))
        except BaseException:
            stream.close()
            raise
        return stream

    with monkeypatch.context() as scope:
        scope.setattr(transcription_host, '_open_worker_status', replace_after_open)
        assert transcription_host.read_worker_status(status) == {'state': 'running', 'done': 0}
    assert transcription_host.read_worker_status(status) == {'state': 'finished', 'done': 1}
    assert status.read_bytes() == new and not stage.exists()


# The following tests execute the whole installed dialog with small explicit
# native-control boundaries. Writes use the real retained-source service and
# the existing DesktopFrame executor/posted-result implementation.
def _drive_transcript_save_all(prepared, monkeypatch, script):
    from types import SimpleNamespace
    from carr_library.ui import transcription_dialog
    from carr_library.ui.save_all import SaveCoordinator
    app, workspace, service, original, _ = prepared
    identity, job = _fixture.completed(prepared)
    helper = _current_progress_host()
    frame, callbacks = helper._host_frame()
    frame.library, frame.active = app, (workspace, 'Synthetic workspace')
    frame._save_coordinator = SaveCoordinator()
    monkeypatch.setattr(app, 'transcription', lambda selected: service)
    named, dialogs = {}, []
    events = SimpleNamespace(button=object(), text=object(), choice=object(), close=object())

    class Event:
        vetoed = False
        def Skip(self): pass
        def CanVeto(self): return True
        def Veto(self): self.vetoed = True

    class Control:
        def __init__(self, parent=None, *, value='', choices=(), label='', **kwargs):
            self.value, self.items, self.selection = value, list(choices), -1
            self.enabled, self.handlers, self.focused = True, {}, False
        def SetName(self, name):
            assert name not in named
            named[name] = self
        def Bind(self, event, handler): self.handlers[event] = handler
        def Enable(self, value=True): self.enabled = value
        def IsEnabled(self): return self.enabled
        def GetValue(self): return self.value
        def ChangeValue(self, value): self.value = value
        def SetValue(self, value):
            self.value = value
            if events.text in self.handlers:
                self.handlers[events.text](Event())
        def SetItems(self, items): self.items, self.selection = list(items), -1
        def SetSelection(self, value): self.selection = value
        def GetSelection(self): return self.selection
        def SetFocus(self): self.focused = True

    class Dialog(Control):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.ended, self.destroyed = False, False
            dialogs.append(self)
        def SetSizer(self, value): pass
        def EndModal(self, code): self.ended = True
        def Destroy(self): self.destroyed = True
        def ShowModal(self):
            helper._drain(frame, callbacks)  # The actual initial text-read job.
            editor = named['Текст: редагування зберігається окремою версією']
            assert editor.IsEnabled()
            assert self._save_coordinator is frame._save_coordinator
            assert len(self._save_coordinator.participants) == 1
            def click(name):
                event = Event()
                named[name].handlers[events.button](event)
                return event
            script(SimpleNamespace(frame=frame, callbacks=callbacks, dialog=self,
                coordinator=frame._save_coordinator, editor=editor, controls=named,
                click=click, drain=lambda: helper._drain(frame, callbacks),
                service=service, identity=identity, job=job, original=original))
            return 0

    class Sizer:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): pass

    fake = SimpleNamespace(Dialog=Dialog, TextCtrl=Control, StaticText=Control,
        Choice=Control, CheckBox=Control, Button=Control, BoxSizer=Sizer, GridSizer=Sizer,
        EVT_BUTTON=events.button, EVT_TEXT=events.text, EVT_CHOICE=events.choice, EVT_CLOSE=events.close,
        DEFAULT_DIALOG_STYLE=1, RESIZE_BORDER=2, VERTICAL=4, ALL=8, EXPAND=16,
        TE_MULTILINE=32, TE_READONLY=64, YES_NO=128, NO_DEFAULT=256, YES=1, NO=0,
        ID_CLOSE=42, MessageBox=lambda *args: 0,
        CallAfter=lambda callback, *args: callbacks.append(lambda: callback(*args)))
    monkeypatch.setitem(sys.modules, 'wx', fake)
    try:
        transcription_dialog.show_transcription(frame)
        assert len(dialogs) == 1 and dialogs[0].destroyed
        assert frame._save_coordinator.participants == {}
    finally:
        frame.cancel_event.set()
        frame.executor.shutdown(wait=True, cancel_futures=True)


def _hold_actual_edit_publication(monkeypatch):
    from threading import Event
    from carr_library import transcription
    committed, release = Event(), Event()
    publish = transcription.atomic_json
    def gated(path, value, **kwargs):
        result = publish(path, value, **kwargs)
        if Path(path).parent.name == 'edits':
            committed.set()
            assert release.wait(10), 'Synthetic save-result gate was not released.'
        return result
    monkeypatch.setattr(transcription, 'atomic_json', gated)
    return committed, release


def test_native_transcript_save_all_waits_for_actual_durable_revision(prepared, monkeypatch):
    def script(h):
        committed, release = _hold_actual_edit_publication(monkeypatch)
        expected = h.original.read_bytes()
        source = h.job['sources'][0]['filename']
        original_text = h.service.text(h.identity, source)
        h.editor.SetValue('A separately saved transcript draft.')
        summaries = []
        try:
            assert h.coordinator.save_all(summaries.append)
            assert committed.wait(10)
            assert h.coordinator.busy and h.coordinator.has_unsaved() and summaries == []
            edit, = h.service.edits(h.identity)
            assert edit['text'] == h.editor.GetValue()
            assert edit['source_sha256'] == sha256(expected).hexdigest()
            assert edit['original_text_sha256'] == sha256(original_text.encode('utf-8')).hexdigest()
        finally:
            release.set()
        h.drain()
        assert len(summaries) == 1 and summaries[0].complete and len(summaries[0].saved) == 1
        assert not h.coordinator.busy and not h.coordinator.has_unsaved()
        assert not h.controls['Зберегти правки'].IsEnabled()
        assert h.original.read_bytes() == expected
        assert h.service.text(h.identity, source) == original_text
        assert h.coordinator.save_all(summaries.append)
        assert summaries[-1].complete and len(h.service.edits(h.identity)) == 1
    _drive_transcript_save_all(prepared, monkeypatch, script)


def test_native_transcript_save_all_keeps_newer_editor_value(prepared, monkeypatch):
    def script(h):
        committed, release = _hold_actual_edit_publication(monkeypatch)
        h.editor.SetValue('Snapshot submitted for saving.')
        summaries = []
        try:
            assert h.coordinator.save_all(summaries.append)
            assert committed.wait(10)
            # A queued native edit can arrive before the saved callback even
            # when user interaction was disabled while the worker ran.
            h.editor.SetValue('A newer draft that must remain dirty.')
        finally:
            release.set()
        h.drain()
        assert len(summaries) == 1 and not summaries[0].complete
        assert h.coordinator.has_unsaved() and h.controls['Зберегти правки'].IsEnabled()
        assert h.editor.GetValue() == 'A newer draft that must remain dirty.'
        edit, = h.service.edits(h.identity)
        assert edit['text'] == 'Snapshot submitted for saving.'
        assert h.coordinator.save_all(summaries.append)
        h.drain()
        assert summaries[-1].complete and not h.coordinator.has_unsaved()
        assert [item['text'] for item in h.service.edits(h.identity)] == [
            'Snapshot submitted for saving.', 'A newer draft that must remain dirty.']
    _drive_transcript_save_all(prepared, monkeypatch, script)


@pytest.mark.parametrize('failure', ['held-job-lock', 'changed-recognition'])
def test_native_transcript_save_all_actual_validation_failure_keeps_draft(prepared, monkeypatch, failure):
    from contextlib import nullcontext
    from carr_library.durable_state import state_lock
    def script(h):
        h.editor.SetValue('Retain these edits after a real service refusal.')
        path = h.service._path(h.identity)
        guard = nullcontext()
        if failure == 'held-job-lock':
            guard = state_lock(path, 'transcribe')
        else:
            artifact = next(item for item in h.job['artifacts'] if item['format'] == 'txt')
            (path / 'results' / artifact['path']).write_text('Changed original recognition.', encoding='utf-8')
        summaries = []
        with guard:
            assert h.coordinator.save_all(summaries.append)
            h.drain()
        assert len(summaries) == 1 and not summaries[0].complete and summaries[0].remaining
        assert not h.coordinator.busy and h.coordinator.has_unsaved()
        assert h.editor.GetValue() == 'Retain these edits after a real service refusal.'
        assert h.controls['Зберегти правки'].IsEnabled() and h.editor.focused
        assert h.service.edits(h.identity) == ()
    _drive_transcript_save_all(prepared, monkeypatch, script)


def test_native_transcript_save_all_executor_rejection_settles_once(prepared, monkeypatch):
    def script(h):
        h.editor.SetValue('Keep the draft when the real executor is closed.')
        h.frame.executor.shutdown(wait=True)
        summaries = []
        assert h.coordinator.save_all(summaries.append)
        assert len(summaries) == 1 and not summaries[0].complete
        assert not h.coordinator.busy and not h.frame.working
        assert h.coordinator.has_unsaved() and h.controls['Зберегти правки'].IsEnabled()
        assert h.service.edits(h.identity) == () and not h.callbacks
    _drive_transcript_save_all(prepared, monkeypatch, script)


def test_native_transcript_save_all_busy_host_preserves_draft(prepared, monkeypatch):
    from threading import Event
    def script(h):
        entered, release = Event(), Event()
        def operation():
            entered.set()
            assert release.wait(10)
            return 'Existing operation complete.'
        h.editor.SetValue('Save after the existing operation finishes.')
        summaries = []
        try:
            h.frame.run_job('Existing real background operation', operation, done=lambda result: None)
            assert entered.wait(10)
            assert h.coordinator.save_all(summaries.append)
            assert len(summaries) == 1 and not summaries[0].complete
            assert not h.coordinator.busy and h.coordinator.has_unsaved()
            assert h.service.edits(h.identity) == ()
        finally:
            release.set()
        h.drain()
        assert h.coordinator.save_all(summaries.append)
        h.drain()
        assert summaries[-1].complete and len(h.service.edits(h.identity)) == 1
    _drive_transcript_save_all(prepared, monkeypatch, script)


def test_native_transcript_save_all_button_defers_close_until_durable_completion(prepared, monkeypatch):
    def script(h):
        committed, release = _hold_actual_edit_publication(monkeypatch)
        h.editor.SetValue('Save All from the modal transcript editor.')
        try:
            h.click('Зберегти всі відкриті чернетки')
            assert committed.wait(10) and h.coordinator.busy
            event = h.click('Закрити')
            assert event.vetoed and not h.dialog.ended
            assert not h.frame.cancel_event.is_set()
            assert h.dialog._save_close_pending
        finally:
            release.set()
        h.drain()
        assert h.dialog.ended and not h.dialog._save_close_pending
        assert not h.coordinator.busy and not h.coordinator.has_unsaved()
        edit, = h.service.edits(h.identity)
        assert edit['text'] == 'Save All from the modal transcript editor.'
    _drive_transcript_save_all(prepared, monkeypatch, script)


def test_native_transcript_save_all_failed_pending_close_retains_editor(prepared, monkeypatch):
    from carr_library.durable_state import state_lock
    def script(h):
        h.editor.SetValue('A failed save cannot close this editor.')
        with state_lock(h.service._path(h.identity), 'transcribe'):
            h.click('Зберегти всі відкриті чернетки')
            assert h.coordinator.busy
            event = h.click('Закрити')
            assert event.vetoed and h.dialog._save_close_pending
            h.drain()
        assert not h.dialog.ended and not h.dialog._save_close_pending
        assert not h.coordinator.busy and h.coordinator.has_unsaved()
        assert h.editor.GetValue() == 'A failed save cannot close this editor.'
        assert h.service.edits(h.identity) == ()
        assert 'remains open' in h.controls['Стан транскрипції'].GetValue()
    _drive_transcript_save_all(prepared, monkeypatch, script)
