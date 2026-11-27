"""Snapshot workflows run on a real worker; native controls are explicit doubles.

These tests verify state, authority and callback behavior, not NVDA acceptance.
"""
from concurrent.futures import Future
from pathlib import Path
from queue import Empty, Queue
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import Mock
import sys
import time

import pytest

from carr_library.ui.portable_sync import (
    InputError, NativePortableSync, PortableSyncController,
    install_portable_sync_actions,
)


TOKEN = 'a' * 64
OTHER = 'b' * 64
PASSWORD = 'Secret for one operation only!'


class View:
    def __init__(self):
        self.messages, self.rows, self.preview = [], (), ''
        self.busy, self.verified, self.closed = False, False, False
        self.dead = False
        self.threads = []

    def _touch(self):
        assert not self.dead, 'A completion touched a destroyed native window'
        self.threads.append(get_ident())

    def report(self, message, *, focus=False):
        self._touch()
        self.messages.append(message)

    def set_busy(self, value):
        self._touch()
        self.busy = value

    def show_history(self, rows):
        self._touch()
        self.rows = rows

    def show_preview(self, value, *, verified):
        self._touch()
        self.preview, self.verified = value, verified

    def close_window(self):
        self._touch()
        self.closed = True


class Dispatcher:
    def __init__(self):
        self.queue = Queue()

    def __call__(self, callback, *args):
        self.queue.put((callback, args))

    def pump(self, controller):
        deadline = time.monotonic() + 10
        while controller.running or not self.queue.empty():
            remaining = deadline - time.monotonic()
            assert remaining > 0, 'Snapshot worker did not finish'
            try:
                callback, args = self.queue.get(timeout=min(remaining, 1))
            except Empty:
                continue
            callback(*args)


class DeferredExecutor:
    """Deterministically expose the before-start and after-commit boundaries."""
    def __init__(self):
        self.pending, self.stopped = [], False

    def submit(self, operation):
        future = Future()
        self.pending.append((operation, future))
        return future

    def run(self):
        operation, future = self.pending.pop(0)
        try:
            future.set_result(operation())
        except BaseException as error:
            future.set_exception(error)
        return future

    def shutdown(self, **kwargs):
        self.stopped = True


class Service:
    def __init__(self):
        self.calls, self.worker_threads = [], []
        self.rows = []
        self.fail, self.on_call = None, None

    def _call(self, name, **values):
        self.worker_threads.append(get_ident())
        self.calls.append((name, values))
        if self.on_call:
            self.on_call(name, values)
        if self.fail:
            raise self.fail

    def list(self, folder):
        self._call('list', folder=folder)
        return self.rows

    def publish(self, folder, label, expected_head, password=None, **options):
        self._call('publish', folder=folder, label=label, head=expected_head, password=password, **options)
        return dict(id=TOKEN, token=TOKEN, head=TOKEN, encrypted=password is not None,
                    scope='All libraries in this data folder', message='Published')

    def inspect(self, folder, token, password=None, **options):
        self._call('inspect', folder=folder, token=token, password=password, **options)
        return dict(id=token, token=token, encrypted=password is not None,
            scope='All libraries in the published data folder', libraries=['Current library', 'Archived library'],
            schema_version=4, files=3, bytes=1200, backup_checksum=OTHER,
            warnings=['Pending imports need their separate export.'], restore_mode='new-folder-only', verified=True)

    def restore(self, folder, token, destination, password=None, **options):
        self._call('restore', folder=folder, token=token, destination=destination, password=password, **options)
        destination.mkdir()
        return dict(id=token, token=token, destination=str(destination), verified=True, message='Restored')


@pytest.fixture
def state(tmp_path):
    root = tmp_path / 'current-library'
    root.mkdir()
    app = SimpleNamespace(root=root, database=root / 'library.sqlite', assets_dir=root / 'originals')
    frame = SimpleNamespace(library=app, active=('library', 'topic'), _closing=False,
        working=True, busy=True, unsaved_note='Keep this exact unsaved research draft.',
        run_job=Mock(side_effect=AssertionError('Snapshot work must not disable the main frame')),
        refresh_scopes=Mock(side_effect=AssertionError('Do not change the open notebook')),
        _open_data_root=Mock(side_effect=AssertionError('Do not switch the open data folder')))
    view, dispatcher, service = View(), Dispatcher(), Service()
    controller = PortableSyncController(frame, view, service=service, dispatch=dispatcher)
    value = SimpleNamespace(frame=frame, view=view, dispatcher=dispatcher, service=service,
        controller=controller, folder=tmp_path / 'portable', destination=tmp_path / 'restored')
    yield value
    controller.dispose()


def finish(state):
    state.dispatcher.pump(state.controller)


def verify(state, *, password=None):
    state.controller.verify(state.folder, TOKEN, password=password)
    finish(state)
    assert state.controller.inspection is not None


def defer(state):
    state.controller.executor.shutdown(wait=True)
    state.controller.executor = DeferredExecutor()
    return state.controller.executor


def test_full_workflow_uses_dedicated_worker_and_preserves_open_unsaved_notebook(state):
    owner = get_ident()
    state.controller.refresh(state.folder)
    finish(state)
    state.controller.publish(state.folder, 'Research checkpoint', confirmed=True)
    finish(state)
    assert TOKEN in state.view.preview and not state.view.verified
    assert state.controller.inspection is None
    verify(state)
    assert 'Archived library' in state.view.preview and 'Pending imports' in state.view.preview
    preview = state.controller.restore_preview(state.folder, TOKEN, state.destination)
    assert str(state.destination) in preview and 'does not exist' in preview
    state.controller.restore(state.folder, TOKEN, state.destination, confirmed=True)
    finish(state)
    assert state.destination.is_dir() and 'Restore completed' in state.view.preview
    assert state.frame.unsaved_note == 'Keep this exact unsaved research draft.'
    assert state.frame.active == ('library', 'topic') and state.frame.working is True
    assert state.frame.run_job.call_count == state.frame.refresh_scopes.call_count == state.frame._open_data_root.call_count == 0
    assert all(thread != owner for thread in state.service.worker_threads)
    assert set(state.view.threads) == {owner}


def test_listed_snapshot_is_not_authority_and_never_supplies_a_token(state):
    state.service.rows = [dict(id=TOKEN, head=True, label='Foreign snapshot', encrypted=False)]
    state.controller.refresh(state.folder)
    finish(state)
    assert state.controller.expected_head == TOKEN
    assert state.controller.inspection is None and not state.view.verified
    state.controller.verify(state.folder, '')
    assert [name for name, _ in state.service.calls] == ['list']
    state.controller.verify(state.folder, OTHER)
    finish(state)
    assert state.service.calls[-1][1]['token'] == OTHER
    assert state.controller.inspection['id'] == OTHER


def test_explicit_head_marker_controls_publish_not_timestamp_order(state):
    state.service.rows = [dict(id=OTHER, head=False, label='Newer timestamp'),
                          dict(id=TOKEN, head=True, label='Actual head')]
    state.controller.refresh(state.folder)
    finish(state)
    state.controller.publish(state.folder, 'Next version', confirmed=True)
    finish(state)
    assert state.service.calls[-1][1]['head'] == TOKEN


def test_publish_requires_explicit_scope_confirmation_and_fresh_folder_history(state):
    state.controller.publish(state.folder, 'No history', confirmed=True)
    assert state.service.calls == []
    state.controller.refresh(state.folder)
    finish(state)
    state.controller.publish(state.folder, 'No confirmation')
    state.controller.publish(state.folder.parent / 'different', 'Other folder', confirmed=True)
    assert [name for name, _ in state.service.calls] == ['list']
    state.controller.publish(state.folder, 'Explicit scope', confirmed=True)
    finish(state)
    assert state.service.calls[-1][0] == 'publish'


def test_history_conflict_never_silently_retries_or_uses_a_newer_head(state):
    class SyncConflict(ValueError):
        pass
    state.controller.refresh(state.folder)
    finish(state)
    state.service.fail = SyncConflict('Private diagnostic ' + PASSWORD)
    state.controller.publish(state.folder, 'Conflict', password=PASSWORD, confirmed=True)
    finish(state)
    assert state.controller.history_folder is None
    assert PASSWORD not in '\n'.join(state.view.messages)
    assert 'histories differ' in state.view.messages[-1]
    state.controller.publish(state.folder, 'Retry without refresh', confirmed=True)
    assert sum(name == 'publish' for name, _ in state.service.calls) == 1


def test_password_is_operation_only_and_never_displayed_in_status_or_exception(state):
    def leak(name, values):
        values['on_status']('Checking ' + values['password'])
        raise ValueError('Provider echoed ' + values['password'])
    state.service.on_call = leak
    state.controller.verify(state.folder, TOKEN, password=PASSWORD)
    finish(state)
    output = '\n'.join(state.view.messages) + state.view.preview
    assert PASSWORD not in output and '[redacted]' in output
    assert not any(value == PASSWORD for value in vars(state.controller).values())
    assert state.controller.inspection is None
    assert 'could not be confirmed' in state.view.messages[-1]


def test_password_is_not_retained_between_verify_and_restore(state):
    verify(state, password=PASSWORD)
    assert state.service.calls[-1][1]['password'] == PASSWORD
    state.controller.restore(state.folder, TOKEN, state.destination, confirmed=True)
    finish(state)
    assert state.service.calls[-1][1]['password'] is None


@pytest.mark.parametrize('folder_changed', [False, True])
def test_input_edit_revokes_verified_preview_before_restore(state, folder_changed):
    verify(state)
    state.controller.inputs_changed(folder=folder_changed)
    state.controller.restore(state.folder, TOKEN, state.destination, confirmed=True)
    assert state.controller.inspection is None and not state.view.verified
    assert all(name != 'restore' for name, _ in state.service.calls)
    assert not state.destination.exists()


@pytest.mark.parametrize('kind', ['existing', 'dangling-link', 'inside-live', 'inside-vault', 'missing-parent'])
def test_restore_preflight_rejects_conflicting_or_unsafe_destination(state, kind):
    verify(state)
    destination = state.destination
    if kind == 'existing':
        destination.mkdir()
        (destination / 'retain.txt').write_text('Existing user data')
    elif kind == 'dangling-link':
        destination.symlink_to(state.destination.parent / 'missing-target', target_is_directory=True)
    elif kind == 'inside-live':
        destination = state.frame.library.root / 'new'
    elif kind == 'inside-vault':
        state.folder.mkdir()
        destination = state.folder / 'new'
    else:
        destination = destination / 'missing-parent' / 'new'
    state.controller.restore(state.folder, TOKEN, destination, confirmed=True)
    assert all(name != 'restore' for name, _ in state.service.calls)
    if kind == 'existing':
        assert (destination / 'retain.txt').read_text() == 'Existing user data'


def test_destination_created_after_preview_is_rechecked_before_submit(state):
    verify(state)
    state.controller.restore_preview(state.folder, TOKEN, state.destination)
    state.destination.mkdir()
    state.controller.restore(state.folder, TOKEN, state.destination, confirmed=True)
    assert all(name != 'restore' for name, _ in state.service.calls)


def test_malformed_inspection_cannot_enable_restore(state):
    original = state.service.inspect
    state.service.inspect = lambda *args, **kwargs: dict(original(*args, **kwargs), verified=False)
    state.controller.verify(state.folder, TOKEN)
    finish(state)
    assert state.controller.inspection is None and not state.view.verified
    assert 'unsupported receipt' in state.view.messages[-1]


def test_cancel_before_worker_start_avoids_service_call(state):
    executor = defer(state)
    state.controller.verify(state.folder, TOKEN, password=PASSWORD)
    state.controller.cancel()
    outcome = executor.run().result()
    finish(state)
    assert outcome.cancelled and state.service.calls == []
    assert PASSWORD not in repr(outcome)
    assert state.controller.inspection is None


def test_cancellation_reaches_real_worker_and_keeps_main_frame_available(state):
    entered, release = Event(), Event()
    def paused(name, values):
        entered.set()
        assert release.wait(5)
        assert values['cancelled']()
        raise InterruptedError('Cancelled ' + str(values['password']))
    state.service.on_call = paused
    state.controller.verify(state.folder, TOKEN, password=PASSWORD)
    assert entered.wait(5)
    assert state.view.busy and state.frame.run_job.call_count == 0
    state.controller.cancel()
    release.set()
    finish(state)
    assert not state.view.busy and state.controller.inspection is None
    assert 'cancelled' in state.view.messages[-1].lower()
    assert PASSWORD not in '\n'.join(state.view.messages)


def test_late_cancel_after_verification_does_not_publish_preview(state):
    executor = defer(state)
    state.controller.verify(state.folder, TOKEN)
    executor.run()  # Completed service; UI completion is still queued.
    state.controller.cancel()
    finish(state)
    assert state.controller.inspection is None and not state.view.verified


def test_late_cancel_after_successful_mutation_reports_the_actual_receipt(state):
    state.controller.refresh(state.folder)
    finish(state)
    executor = defer(state)
    state.controller.publish(state.folder, 'Committed', confirmed=True)
    executor.run()
    state.controller.cancel()
    finish(state)
    assert TOKEN in state.view.preview
    assert 'completed before cancellation' in state.view.messages[-1]


def test_input_change_suppresses_stale_success(state):
    executor = defer(state)
    state.controller.verify(state.folder, TOKEN)
    executor.run()
    state.controller.inputs_changed(folder=True)
    finish(state)
    assert state.controller.inspection is None and not state.view.verified
    assert 'inputs changed' in state.view.messages[-1]


def test_data_folder_switch_before_start_withdraws_work(state):
    executor = defer(state)
    state.controller.verify(state.folder, TOKEN)
    state.frame.library = SimpleNamespace(root=state.folder, database=state.folder / 'library.sqlite')
    executor.run()
    finish(state)
    assert state.service.calls == [] and state.controller.inspection is None


def test_changing_current_topic_within_same_data_folder_does_not_retarget_or_cancel_snapshot(state):
    executor = defer(state)
    state.controller.verify(state.folder, TOKEN)
    state.frame.active = ('another-library', 'another-topic')
    executor.run()
    finish(state)
    assert state.controller.inspection is not None
    assert state.frame.active == ('another-library', 'another-topic')


def test_normal_close_waits_for_worker_cancellation_then_disposes(state):
    executor = defer(state)
    state.controller.verify(state.folder, TOKEN)
    assert state.controller.request_close() is False
    assert state.controller.cancel_event.is_set() and not state.view.closed
    executor.run()
    finish(state)
    assert state.view.closed and not state.controller.alive and executor.stopped


def test_forced_destruction_prevents_all_late_control_access(state):
    executor = defer(state)
    state.controller.verify(state.folder, TOKEN)
    state.controller.dispose()
    state.view.dead = True
    executor.run()
    finish(state)
    assert not state.controller.alive and state.service.calls == []


def test_executor_start_failure_releases_busy_state_and_hides_secrets(state):
    state.controller.executor.shutdown(wait=True)
    state.controller.executor = SimpleNamespace(submit=Mock(side_effect=RuntimeError(PASSWORD)), shutdown=Mock())
    state.controller.verify(state.folder, TOKEN, password=PASSWORD)
    assert not state.controller.running and not state.view.busy
    assert state.view.messages[-1] == 'The snapshot worker could not start.'
    assert PASSWORD not in '\n'.join(state.view.messages)


def test_partial_publication_exposes_only_typed_recovery_receipt(state):
    from carr_library.sync.service import SyncPublishedError
    state.controller.refresh(state.folder)
    finish(state)
    state.service.fail = SyncPublishedError(dict(id=TOKEN, token=TOKEN, head=TOKEN, encrypted=True,
                                               password=PASSWORD))
    state.controller.publish(state.folder, 'Partial publication', password=PASSWORD, confirmed=True)
    finish(state)
    assert TOKEN in state.view.preview and 'history was not saved' in state.view.preview
    assert PASSWORD not in state.view.preview
    assert 'recovery receipt' in state.view.messages[-1]
    assert state.controller.history_folder is None and not state.view.verified


def test_partial_restore_reports_existing_destination_without_switching(state):
    from carr_library.sync.service import SyncRestoredError
    verify(state)
    state.service.fail = SyncRestoredError(state.destination, TOKEN)
    state.controller.restore(state.folder, TOKEN, state.destination, confirmed=True)
    finish(state)
    assert str(state.destination) in state.view.preview and TOKEN in state.view.preview
    assert 'final completion check failed' in state.view.preview
    assert state.frame._open_data_root.call_count == 0


def test_status_from_previous_operation_cannot_overwrite_new_operation(state):
    verify(state)
    old_epoch = state.controller.epoch
    executor = defer(state)
    state.controller.verify(state.folder, TOKEN)
    message = state.view.messages[-1]
    state.controller._status(old_epoch, 'Stale status from a previous operation')
    assert state.view.messages[-1] == message
    executor.run()
    finish(state)


def test_native_forced_destroy_ignores_child_events_and_disposes_own_window(state):
    native = object.__new__(NativePortableSync)
    native.window = object()
    native.controller = state.controller
    native._window_alive, native._notified = True, False
    native.on_closed = Mock()
    event = SimpleNamespace(GetEventObject=lambda: object(), Skip=Mock())
    native._destroyed(event)
    assert native._window_alive and state.controller.alive
    event.GetEventObject = lambda: native.window
    native._destroyed(event)
    native._destroyed(event)
    assert not native._window_alive and not state.controller.alive
    native.on_closed.assert_called_once_with(native)


def test_native_options_change_during_password_prompt_does_not_start_stale_verification(state):
    native = object.__new__(NativePortableSync)
    native._window_alive, native.window, native.controller = True, object(), state.controller
    native.folder = SimpleNamespace(GetValue=lambda: str(state.folder))
    selected = {'token': TOKEN}
    native.token = SimpleNamespace(GetValue=lambda: selected['token'])

    def password(parent):
        selected['token'] = OTHER
        return True, PASSWORD
    native.dialogs = SimpleNamespace(password=password)
    native._verify()
    assert state.service.calls == [] and not state.controller.running
    assert 'options changed' in state.view.messages[-1]


def test_native_destination_change_during_confirmation_is_not_restored(state):
    verify(state)
    native = object.__new__(NativePortableSync)
    native._window_alive, native.window, native.controller = True, object(), state.controller
    native.folder = SimpleNamespace(GetValue=lambda: str(state.folder))
    native.token = SimpleNamespace(GetValue=lambda: TOKEN)
    selected = {'destination': str(state.destination)}
    native.destination = SimpleNamespace(GetValue=lambda: selected['destination'])

    def confirm(*args):
        selected['destination'] = str(state.destination.parent / 'changed-destination')
        return True
    native.dialogs = SimpleNamespace(confirm=confirm)
    native._restore()
    assert all(name != 'restore' for name, _ in state.service.calls)
    assert 'options changed' in state.view.messages[-1]


def test_menu_installation_preserves_existing_commands_and_is_idempotent(monkeypatch):
    callback = object()
    library, research = SimpleNamespace(items=[]), SimpleNamespace(items=[])
    library.Append = lambda identifier, title: library.items.append(title) or len(library.items)
    research.Append = lambda identifier, title: research.items.append(title) or len(research.items)
    frame = SimpleNamespace(menu_bar=SimpleNamespace(GetMenuCount=lambda: 2,
        GetMenuLabel=lambda index: ('&Research', '&Library')[index],
        GetMenu=lambda index: (research, library)[index]), Bind=Mock())
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(ID_ANY=-1, EVT_MENU=callback))
    actions = install_portable_sync_actions(frame)
    assert install_portable_sync_actions(frame) is actions
    assert library.items == ['Portable snapshots and transfer…'] and research.items == []
    frame.Bind.assert_called_once_with(callback, actions.open, 1)


@pytest.mark.parametrize('password', [None, PASSWORD], ids=['plain', 'encrypted'])
def test_real_service_snapshot_round_trip_retains_all_libraries_without_switching(tmp_path, password):
    from carr_library.application import LibraryApp
    from carr_library.sync.service import SyncService
    app = LibraryApp(tmp_path / 'source')
    current = app.create_workspace('Current library')
    archived = app.create_workspace('Archived library')
    app.add_text(current, 'Checked original', 'The exact source remains unchanged.')
    app.archive_workspace(archived)
    frame = SimpleNamespace(library=app, active=(current, None), _closing=False)
    view, dispatch = View(), Dispatcher()
    controller = PortableSyncController(frame, view, service=SyncService(app), dispatch=dispatch)
    portable, destination = tmp_path / 'portable', tmp_path / 'restored'
    try:
        controller.refresh(portable)
        dispatch.pump(controller)
        controller.publish(portable, 'Portable academic library', password=password, confirmed=True)
        dispatch.pump(controller)
        token = controller.expected_head
        assert isinstance(token, str) and len(token) == 64, view.messages
        controller.verify(portable, token, password=password)
        dispatch.pump(controller)
        assert controller.inspection is not None, view.messages
        assert set(controller.inspection['libraries']) == {'Current library', 'Archived library'}
        assert 'Backup warnings and exclusions' in view.preview
        controller.restore(portable, token, destination, password=password, confirmed=True)
        dispatch.pump(controller)
        assert 'Restore completed' in view.preview, view.messages
        restored = LibraryApp(destination)
        assert {(row.name, row.archived) for row in restored.workspaces(include_archived=True)} == {
            ('Current library', False), ('Archived library', True)}
        assert frame.library is app and frame.active == (current, None)
    finally:
        controller.dispose()


def test_real_orphan_manifest_can_be_listed_and_publication_resumed(tmp_path, monkeypatch):
    from carr_library.application import LibraryApp
    from carr_library.sync.service import SyncService
    from carr_library.sync import vault
    app = LibraryApp(tmp_path / 'source')
    app.create_workspace('Interrupted snapshot source')
    frame = SimpleNamespace(library=app, active=None, _closing=False)
    view, dispatch = View(), Dispatcher()
    service = SyncService(app)
    controller = PortableSyncController(frame, view, service=service, dispatch=dispatch)
    portable = tmp_path / 'portable'
    original = vault._write_file

    def interrupt_before_head(path, *args, **kwargs):
        if path.name == 'head.json':
            raise InterruptedError('Synthetic interruption after the complete manifest, before HEAD')
        return original(path, *args, **kwargs)

    try:
        controller.refresh(portable)
        dispatch.pump(controller)
        with monkeypatch.context() as patch:
            patch.setattr(vault, '_write_file', interrupt_before_head)
            controller.publish(portable, 'Resumable checkpoint', confirmed=True)
            dispatch.pump(controller)
        rows = service.list(portable)
        assert rows and all(row['head'] is False for row in rows)
        controller.refresh(portable)
        dispatch.pump(controller)
        assert controller.history_folder == portable and controller.expected_head is None
        assert all('not the current head' in row for row in view.rows)
        assert controller.inspection is None
        controller.publish(portable, 'Resumable checkpoint', confirmed=True)
        dispatch.pump(controller)
        assert controller.expected_head == vault.Vault(portable).head(), view.messages
        assert 'Snapshot published' in view.preview
    finally:
        controller.dispose()



@pytest.fixture
def confirmation_probe():
    """Import only the native probe's portable policy, without creating wx."""
    import importlib.util
    path = Path(__file__).with_name('probe_portable_sync_native.py')
    spec = importlib.util.spec_from_file_location('portable_sync_confirmation_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _accessible_operation(probe, *, output=None, returncode=0):
    request = dict(hwnd=101, owner=99, pid=301, title='Synthetic confirmation',
                   yes='Yes', no='No', answer='Yes')
    receipt = dict(status='NATIVE_DIALOG', method='UIA_Invoke', pid=301,
                   hwnd=101, owner=99, title='Synthetic confirmation',
                   button_name='Yes', button_type=50000, button_id='CommandButton_0',
                   button_hwnd=0, enabled=True, offscreen=False)
    operation = probe.AccessibleAnswer.__new__(probe.AccessibleAnswer)
    operation.request = request
    operation.deadline = 10
    operation.process = Mock()
    operation.process.poll.return_value = returncode
    operation.process.returncode = returncode
    operation.process.communicate.return_value = (
        probe.json.dumps(receipt if output is None else output).encode('utf-8'), b'')
    return operation, receipt


def test_native_confirmation_accepts_correlated_virtual_button_receipt(confirmation_probe):
    operation, _ = _accessible_operation(confirmation_probe)
    assert operation.poll() is True


@pytest.mark.parametrize(('field', 'value'), [
    ('pid', 302), ('hwnd', 102), ('owner', 98), ('title', 'Other confirmation'),
    ('button_name', 'No'), ('button_type', 50004), ('enabled', False),
    ('offscreen', True), ('button_hwnd', True), ('button_hwnd', -1),
    ('status', 'PASS'), ('method', 'EndModal'),
])
def test_native_confirmation_rejects_uncorrelated_receipt(confirmation_probe, field, value):
    operation, receipt = _accessible_operation(confirmation_probe)
    receipt[field] = value
    operation.process.communicate.return_value = (
        confirmation_probe.json.dumps(receipt).encode('utf-8'), b'')
    with pytest.raises(AssertionError, match='does not match'):
        operation.poll()


def test_native_confirmation_pending_timeout_kills_helper_without_success(confirmation_probe, monkeypatch):
    operation, _ = _accessible_operation(confirmation_probe, returncode=None)
    monkeypatch.setattr(confirmation_probe, 'monotonic', lambda: 9)
    assert operation.poll() is False
    operation.process.communicate.assert_not_called()
    monkeypatch.setattr(confirmation_probe, 'monotonic', lambda: 11)
    with pytest.raises(AssertionError, match='eight seconds'):
        operation.poll()
    operation.process.kill.assert_called_once()
    operation.process.communicate.assert_called_once_with(timeout=1)


def test_native_confirmation_failed_helper_cannot_report_success(confirmation_probe):
    operation, _ = _accessible_operation(confirmation_probe, returncode=1)
    with pytest.raises(AssertionError, match='interaction failed'):
        operation.poll()


def _native_confirmation_tree(probe, monkeypatch, *, controls=None, owner=99, pid=None):
    import ctypes
    from ctypes import wintypes
    confirmation = probe.WindowsConfirmations.__new__(probe.WindowsConfirmations)
    confirmation.yes_label, confirmation.no_label = 'Yes', 'No'
    confirmation.pending = None
    confirmation.ctypes, confirmation.types = ctypes, wintypes
    confirmation.callback = lambda callback: callback
    nodes = {101: dict(kind='#32770', text='Synthetic confirmation', owner=owner,
                       pid=probe.os.getpid() if pid is None else pid, visible=True, enabled=True, id=0)}
    nodes.update(controls or {201: dict(kind='DirectUIHWND', text='', owner=0,
                                      visible=True, enabled=True, id=0)})
    api = Mock()
    api.EnumWindows.side_effect = lambda callback, unused: callback(101, 0)
    api.EnumChildWindows.side_effect = lambda window, callback, unused: [
        callback(handle, 0) for handle in nodes if handle != 101]
    api.GetWindowThreadProcessId.side_effect = lambda window, pointer: setattr(pointer._obj, 'value', nodes[window]['pid'])
    api.GetWindow.side_effect = lambda window, flag: nodes.get(window, {}).get('owner', 0)
    api.GetDlgCtrlID.side_effect = lambda window: nodes[window]['id']
    api.IsWindowVisible.side_effect = lambda window: nodes[window]['visible']
    api.IsWindowEnabled.side_effect = lambda window: nodes[window]['enabled']
    confirmation._string = lambda function, window: nodes[window][
        'kind' if function is api.GetClassNameW else 'text']
    confirmation.api = api
    operation = Mock()
    operation.poll.return_value = False
    constructor = Mock(return_value=operation)
    monkeypatch.setattr(probe, 'AccessibleAnswer', constructor)
    return confirmation, operation, constructor


def test_native_virtual_confirmation_is_polled_and_uses_requested_answer(confirmation_probe, monkeypatch):
    confirmation, operation, constructor = _native_confirmation_tree(confirmation_probe, monkeypatch)
    assert confirmation.reply(99, 'Synthetic confirmation', accept=False) is False
    request = constructor.call_args.args[0]
    assert request == dict(hwnd=101, owner=99, pid=confirmation_probe.os.getpid(),
                           title='Synthetic confirmation', yes='Yes', no='No', answer='No')
    assert confirmation.reply(99, 'Synthetic confirmation', accept=False) is False
    operation.poll.return_value = True
    assert confirmation.reply(99, 'Synthetic confirmation', accept=False) is True
    assert confirmation.pending is None
    constructor.assert_called_once()
    confirmation.api.PostMessageW.assert_not_called()


@pytest.mark.parametrize('change', ['owner', 'process', 'title'])
def test_native_virtual_confirmation_never_uses_another_window(confirmation_probe, monkeypatch, change):
    kwargs = {'owner': 98} if change == 'owner' else (
        {'pid': confirmation_probe.os.getpid() + 1} if change == 'process' else {})
    confirmation, _, constructor = _native_confirmation_tree(confirmation_probe, monkeypatch, **kwargs)
    title = 'Other confirmation' if change == 'title' else 'Synthetic confirmation'
    assert confirmation.reply(99, title, accept=True) is False
    constructor.assert_not_called()
    confirmation.api.PostMessageW.assert_not_called()


def test_native_confirmation_pending_action_cannot_be_retargeted(confirmation_probe, monkeypatch):
    confirmation, operation, constructor = _native_confirmation_tree(confirmation_probe, monkeypatch)
    assert confirmation.reply(99, 'Synthetic confirmation', accept=True) is False
    with pytest.raises(AssertionError, match='different owned confirmation'):
        confirmation.reply(99, 'Synthetic confirmation', accept=False)
    confirmation.close()
    operation.close.assert_called_once()
    assert confirmation.pending is None


def test_native_confirmation_retains_real_hwnd_button_click(confirmation_probe, monkeypatch):
    controls = {201: dict(kind='Button', text='Yes', owner=0,
                         visible=True, enabled=True, id=6)}
    confirmation, _, constructor = _native_confirmation_tree(confirmation_probe, monkeypatch, controls=controls)
    assert confirmation.reply(99, 'Synthetic confirmation', accept=True) is True
    confirmation.api.PostMessageW.assert_called_once_with(201, 0x00F5, 0, 0)
    constructor.assert_not_called()


def test_native_confirmation_rejects_ambiguous_hwnd_buttons(confirmation_probe, monkeypatch):
    controls = {handle: dict(kind='Button', text='Yes', owner=0,
                             visible=True, enabled=True, id=6) for handle in (201, 202)}
    confirmation, _, constructor = _native_confirmation_tree(confirmation_probe, monkeypatch, controls=controls)
    with pytest.raises(AssertionError, match='no unique native answer'):
        confirmation.reply(99, 'Synthetic confirmation', accept=True)
    confirmation.api.PostMessageW.assert_not_called()
    constructor.assert_not_called()



def test_native_confirmation_poll_error_closes_helper_and_retains_original_failure(confirmation_probe, monkeypatch):
    confirmation, operation, _ = _native_confirmation_tree(confirmation_probe, monkeypatch)
    assert confirmation.reply(99, 'Synthetic confirmation', accept=True) is False
    operation.poll.side_effect = AssertionError('Original failure')
    with pytest.raises(AssertionError, match='Original failure'):
        confirmation.reply(99, 'Synthetic confirmation', accept=True)
    operation.close.assert_called_once()
    assert confirmation.pending is None


def test_native_confirmation_failed_cleanup_retains_handle_for_retry(confirmation_probe, monkeypatch):
    confirmation, operation, _ = _native_confirmation_tree(confirmation_probe, monkeypatch)
    assert confirmation.reply(99, 'Synthetic confirmation', accept=True) is False
    operation.poll.side_effect = AssertionError('Original failure')
    operation.close.side_effect = TimeoutError('Helper did not exit yet')
    with pytest.raises(AssertionError, match='Original failure'):
        confirmation.reply(99, 'Synthetic confirmation', accept=True)
    assert confirmation.pending is not None
    operation.close.side_effect = None
    confirmation.close()
    assert confirmation.pending is None


def test_native_confirmation_waits_for_answer_controls_to_become_ready(confirmation_probe, monkeypatch):
    controls = {201: dict(kind='Button', text='Yes', owner=0,
                         visible=False, enabled=True, id=6)}
    confirmation, _, constructor = _native_confirmation_tree(
        confirmation_probe, monkeypatch, controls=controls)
    assert confirmation.reply(99, 'Synthetic confirmation', accept=True) is False
    confirmation.api.PostMessageW.assert_not_called()
    constructor.assert_not_called()
    confirmation.api.IsWindowVisible.side_effect = lambda window: True
    assert confirmation.reply(99, 'Synthetic confirmation', accept=True) is True
    confirmation.api.PostMessageW.assert_called_once_with(201, 0x00F5, 0, 0)


def test_native_confirmation_cleanup_drains_current_title_without_retargeting(confirmation_probe, monkeypatch):
    confirmation, operation, constructor = _native_confirmation_tree(confirmation_probe, monkeypatch)
    titles = ('Other confirmation', 'Synthetic confirmation')
    assert confirmation.dismiss(99, titles) is True
    assert constructor.call_args.args[0]['answer'] == 'No'
    assert confirmation.dismiss(99, titles) is True
    constructor.assert_called_once()
    operation.poll.return_value = True
    assert confirmation.dismiss(99, titles) is True
    assert confirmation.pending is None


def test_native_confirmation_cleanup_rejects_another_owner(confirmation_probe, monkeypatch):
    confirmation, operation, _ = _native_confirmation_tree(confirmation_probe, monkeypatch)
    assert confirmation.reply(99, 'Synthetic confirmation', accept=False) is False
    with pytest.raises(AssertionError, match='cannot retarget'):
        confirmation.dismiss(98, ('Synthetic confirmation',))
    operation.poll.assert_not_called()
    operation.close.assert_not_called()


def test_native_confirmation_helper_expires_without_wx_polling(confirmation_probe, monkeypatch):
    import subprocess
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(subprocess, 'Popen', Mock(return_value=process))
    monkeypatch.setenv('SystemRoot', str(Path.cwd()))
    timer = Mock()
    timer_factory = Mock(return_value=timer)
    monkeypatch.setattr(confirmation_probe, 'Timer', timer_factory)
    request = dict(hwnd=101, owner=99, pid=301, title='Synthetic confirmation',
                   yes='Yes', no='No', answer='Yes')
    operation = confirmation_probe.AccessibleAnswer(request)
    interval, expire = timer_factory.call_args.args
    assert interval == 8 and timer.daemon is True
    timer.start.assert_called_once()
    process.kill.assert_not_called()
    expire()  # The UI thread never calls operation.poll().
    process.kill.assert_called_once()
    process.poll.return_value = 1
    operation.close()
    timer.cancel.assert_called_once()
    process.communicate.assert_called_once_with(timeout=1)


def test_native_confirmation_helper_thread_failure_closes_launched_process(confirmation_probe, monkeypatch):
    import subprocess
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(subprocess, 'Popen', Mock(return_value=process))
    monkeypatch.setenv('SystemRoot', str(Path.cwd()))
    timer = Mock()
    timer.start.side_effect = RuntimeError('Synthetic timer startup failure')
    monkeypatch.setattr(confirmation_probe, 'Timer', Mock(return_value=timer))
    with pytest.raises(RuntimeError, match='timer startup failure'):
        confirmation_probe.AccessibleAnswer(dict(hwnd=101))
    process.kill.assert_called_once()
    process.communicate.assert_called_once_with(timeout=1)
    timer.cancel.assert_called_once()


def test_native_uia_failure_exposes_stage_and_matches_without_private_text(confirmation_probe, capsys):
    secret = 'Purpose-written private control message and local path'
    payload = dict(status='NATIVE_DIALOG_UIA_FAILED', stage='answer_match',
        error_type='System.Management.Automation.RuntimeException',
        inner_type='System.Windows.Automation.ElementNotAvailableException', hresult=-2146233087,
        button_count=2, yes_matches=0, no_matches=None, owner_match=True,
        dialog_match=dict(pid=True, title=True, hwnd=True, **{'class': True}),
        controls=[dict(type=50000, pid_match=True, yes_exact=False, no_exact=False,
            yes_without_mnemonic=True, no_without_mnemonic=False, enabled=True, offscreen=False,
            name=secret)], message=secret, stack=secret)
    operation, _ = _accessible_operation(confirmation_probe, output=payload, returncode=1)
    operation.process.communicate.return_value = (
        confirmation_probe.json.dumps(payload).encode('utf-8'), secret.encode('utf-8'))
    with pytest.raises(AssertionError, match='interaction failed'):
        operation.poll()
    output = capsys.readouterr().out
    diagnostic = confirmation_probe.json.loads(output)
    assert diagnostic['stage'] == 'helper_exit' and diagnostic['helper_stage'] == 'answer_match'
    assert diagnostic['error_type'] == payload['error_type']
    assert diagnostic['inner_type'] == payload['inner_type']
    assert diagnostic['returncode'] == 1 and diagnostic['yes_matches'] == 0
    assert diagnostic['controls'][0]['yes_without_mnemonic'] is True
    assert diagnostic['controls'][0]['yes_exact'] is False
    assert secret not in output and 'message' not in diagnostic and 'stack' not in diagnostic


def test_native_uia_failure_rejects_untyped_unbounded_diagnostic_metadata(confirmation_probe, capsys):
    secret = 'Purpose-written diagnostic input, not a message to publish'
    payload = dict(status='NATIVE_DIALOG_UIA_FAILED', stage=[secret], error_type=secret,
        inner_type='System.Invalid\\nType', hresult=True, button_count=secret,
        yes_matches=True, owner_match=secret, dialog_match=dict(title=secret),
        controls=[dict(type=True, enabled=True, name=secret) for _ in range(40)])
    operation, _ = _accessible_operation(confirmation_probe, output=payload, returncode=1)
    with pytest.raises(AssertionError, match='interaction failed'):
        operation.poll()
    output = capsys.readouterr().out
    diagnostic = confirmation_probe.json.loads(output)
    assert 'helper_stage' not in diagnostic and 'error_type' not in diagnostic
    assert 'inner_type' not in diagnostic and 'hresult' not in diagnostic
    assert 'yes_matches' not in diagnostic and 'owner_match' not in diagnostic
    assert diagnostic['dialog_match'] == {}
    assert diagnostic['controls'] == [dict(enabled=True)] * 32
    assert secret not in output and len(output) < 4096


def test_native_uia_receipt_mismatch_logs_field_names_only(confirmation_probe, capsys):
    operation, receipt = _accessible_operation(confirmation_probe)
    secret = 'Purpose-written foreign window or control name'
    receipt.update(title=secret, button_name=secret)
    operation.process.communicate.return_value = (
        confirmation_probe.json.dumps(receipt).encode('utf-8'), b'')
    with pytest.raises(AssertionError, match='does not match'):
        operation.poll()
    output = capsys.readouterr().out
    diagnostic = confirmation_probe.json.loads(output)
    assert diagnostic['stage'] == 'receipt_mismatch'
    assert diagnostic['mismatched_fields'] == ['title', 'button_name']
    assert secret not in output


@pytest.mark.parametrize('kind, stage', [('json', 'helper_json'), ('oversized', 'helper_output_budget'),
                                        ('expired', 'helper_timeout')])
def test_native_uia_transport_failure_remains_bounded_and_cannot_pass(confirmation_probe, capsys, kind, stage):
    operation, _ = _accessible_operation(confirmation_probe, returncode=1)
    private = b'Purpose-written private child diagnostic'
    operation.process.communicate.return_value = (private if kind != 'oversized' else private * 500, private)
    operation.expired = kind == 'expired'
    with pytest.raises(AssertionError, match='interaction failed'):
        operation.poll()
    output = capsys.readouterr().out
    diagnostic = confirmation_probe.json.loads(output)
    assert diagnostic['stage'] == stage and diagnostic['returncode'] == 1
    assert private.decode() not in output and len(output) < 1024


def test_native_successor_password_does_not_accept_pending_confirmation(confirmation_probe):
    confirmation = ('Synthetic confirmation', None)
    responses = [confirmation, ('Snapshot password', 'synthetic-password')]
    dialog = object()
    answer = Mock()
    find = Mock(return_value=dialog)
    assert confirmation_probe.answer_successor_password(
        responses, confirmation_gone=True, find_dialog=find, answer=answer)
    assert responses == [confirmation]  # The UIA completion receipt is still required.
    find.assert_called_once_with('Snapshot password')
    answer.assert_called_once_with(dialog, 'synthetic-password')


@pytest.mark.parametrize('case', ['still-visible', 'no-successor', 'other-dialog', 'not-open'])
def test_native_successor_password_requires_closed_confirmation_and_exact_owned_dialog(
        confirmation_probe, case):
    responses = [('Synthetic confirmation', None), ('Snapshot password', 'synthetic-password')]
    if case == 'no-successor':
        responses.pop()
    elif case == 'other-dialog':
        responses[1] = ('Unrelated dialog', 'synthetic-password')
    before = list(responses)
    answer = Mock()
    find = Mock(return_value=None if case == 'not-open' else object())
    assert not confirmation_probe.answer_successor_password(
        responses, confirmation_gone=case != 'still-visible', find_dialog=find, answer=answer)
    assert responses == before
    answer.assert_not_called()


def test_native_successor_password_failure_preserves_pending_response(confirmation_probe):
    responses = [('Synthetic confirmation', None), ('Snapshot password', 'synthetic-password')]
    before = list(responses)
    with pytest.raises(RuntimeError, match='field rejected'):
        confirmation_probe.answer_successor_password(
            responses, confirmation_gone=True, find_dialog=lambda _: object(),
            answer=Mock(side_effect=RuntimeError('field rejected')))
    assert responses == before


def test_native_confirmation_successor_requires_original_hwnd_to_disappear(
        confirmation_probe, monkeypatch):
    confirmation, operation, _ = _native_confirmation_tree(confirmation_probe, monkeypatch)
    assert not confirmation.pending_dialog_is_gone(99, 'Synthetic confirmation', accept=True)
    confirmation.reply(99, 'Synthetic confirmation', accept=True)
    operation.request = dict(hwnd=101)
    confirmation.api.IsWindow.return_value = True
    assert not confirmation.pending_dialog_is_gone(99, 'Synthetic confirmation', accept=True)
    confirmation.api.IsWindow.return_value = False
    assert confirmation.pending_dialog_is_gone(99, 'Synthetic confirmation', accept=True)
    confirmation.api.IsWindow.assert_called_with(101)


def _native_dismiss_function(probe, namespace):
    import ast
    source = Path(probe.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == 'dismiss_one_modal')
    module = ast.Module(body=[function], type_ignores=[])
    exec(compile(module, str(probe.__file__), 'exec'), namespace)
    return namespace['dismiss_one_modal']


def test_native_failed_cleanup_does_not_retarget_pending_no_to_other_title(confirmation_probe):
    from types import SimpleNamespace
    windows = SimpleNamespace(pending=None)
    calls = []

    def reply(owner, title, *, accept):
        calls.append((owner, title, accept))
        if windows.pending is None:
            windows.pending = (object(), (owner, title, accept))
            return False
        assert windows.pending[1] == (owner, title, accept)
        windows.pending = None
        return True

    windows.reply = reply
    windows.dismiss = lambda owner, titles: confirmation_probe.WindowsConfirmations.dismiss(
        windows, owner, titles)
    namespace = dict(wx=SimpleNamespace(GetTopLevelWindows=lambda: [], Dialog=type('Dialog', (), {})),
                     windows=windows, native=SimpleNamespace(_window_alive=True,
                     window=SimpleNamespace(GetHandle=lambda: 99)),
                     frame=object(), confirmation_titles=('Publish', 'Restore'),
                     descendant=lambda *_: True)
    dismiss = _native_dismiss_function(confirmation_probe, namespace)
    assert dismiss() is True
    assert calls == [(99, 'Publish', False)]
    assert dismiss() is True  # The existing controller reports its completed No action.
    assert windows.pending is None
    assert calls == [(99, 'Publish', False), (99, 'Publish', False)]


def test_native_failed_cleanup_releases_wx_successor_before_polling_uia(confirmation_probe):
    from types import SimpleNamespace
    frame = object()

    class Dialog:
        def IsModal(self): return True
        def GetParent(self): return frame
        def EndModal(self, value): self.answer = value

    dialog = Dialog()
    windows = SimpleNamespace(dismiss=Mock(side_effect=AssertionError('UIA still waits for password')))
    namespace = dict(wx=SimpleNamespace(GetTopLevelWindows=lambda: [dialog], Dialog=Dialog, ID_CANCEL=7),
                     windows=windows, native=SimpleNamespace(_window_alive=True,
                     window=SimpleNamespace(GetHandle=lambda: 99)),
                     frame=frame, confirmation_titles=('Publish', 'Restore'),
                     descendant=lambda *_: True)
    dismiss = _native_dismiss_function(confirmation_probe, namespace)
    assert dismiss() is True and dialog.answer == 7
    windows.dismiss.assert_not_called()


@pytest.mark.parametrize(('owner', 'title', 'accept'), [
    (98, 'Synthetic confirmation', True),
    (99, 'Other confirmation', True),
    (99, 'Synthetic confirmation', False),
])
def test_native_successor_never_consumes_response_for_another_pending_confirmation(
        confirmation_probe, monkeypatch, owner, title, accept):
    confirmation, operation, _ = _native_confirmation_tree(confirmation_probe, monkeypatch)
    confirmation.reply(99, 'Synthetic confirmation', accept=True)
    operation.request = dict(hwnd=101)
    confirmation.api.IsWindow.return_value = False
    responses = [('Synthetic confirmation', None), ('Snapshot password', 'synthetic-password')]
    before = list(responses)
    answer = Mock()
    find = Mock()
    with pytest.raises(AssertionError, match='different owned confirmation'):
        confirmation_probe.answer_successor_password(
            responses, confirmation_gone=confirmation.pending_dialog_is_gone(
                owner, title, accept=accept), find_dialog=find, answer=answer)
    assert responses == before
    find.assert_not_called()
    answer.assert_not_called()
    confirmation.api.IsWindow.assert_not_called()
