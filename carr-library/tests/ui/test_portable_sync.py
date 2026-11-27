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
                    include_generated=options.get('include_generated', False),
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


@pytest.mark.parametrize('include_generated', [False, True])
def test_publication_audio_scope_is_explicit_and_returned_in_the_receipt(state, include_generated):
    state.controller.history_folder = state.folder
    state.controller.publish(state.folder, 'Snapshot', confirmed=True, include_generated=include_generated)
    finish(state)
    assert state.service.calls[-1][1]['include_generated'] is include_generated
    assert ('Generated audio saved in this library: ' +
            ('Included' if include_generated else 'Not included')) in state.view.preview


def test_native_generated_audio_change_during_confirmation_invalidates_publication(state):
    native = object.__new__(NativePortableSync)
    native._window_alive, native.window, native.controller = True, object(), state.controller
    state.controller.history_folder = state.folder
    native.folder = SimpleNamespace(GetValue=lambda: str(state.folder))
    native.label = SimpleNamespace(GetValue=lambda: 'Snapshot')
    native.encrypt = SimpleNamespace(GetValue=lambda: False)
    choice = {'include': False}
    native.include_generated = SimpleNamespace(GetValue=lambda: choice['include'])
    messages = []
    def confirm(parent, title, message):
        messages.append(message)
        choice['include'] = True
        return True
    native.dialogs = SimpleNamespace(confirm=confirm)
    native._publish()
    assert 'Generated audio is not included' in messages[0]
    assert not state.service.calls and not state.controller.running
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
