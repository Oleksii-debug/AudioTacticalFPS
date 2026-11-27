"""Actual maintenance closures, real signed review and explicit logical wx peers.

The fake controls record every forbidden post-deletion native access. Worker
operations below run on actual threads and use the existing TUF client/store.
Only wx and the Desktop queue scheduling are supplied by this portable fixture.
"""
import ast
from contextlib import nullcontext
from hashlib import sha256
import json
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace

import pytest
import carr_library
from carr_library.application import LibraryApp
from carr_library.security.maintenance import UpdateConfiguration, MaintenanceSession
from carr_library.security.controller import release_text
from carr_library.security._files import json_bytes
from carr_library.security.download_state import PausedDownload
from carr_library.security.updates import UpdateCancelled
from carr_library.progress import ProgressHub, bind_progress, operation_outcome


def logical_ui():
    forbidden = []
    wx = SimpleNamespace()
    for name in ('EVT_WINDOW_DESTROY', 'EVT_CLOSE', 'EVT_BUTTON'):
        setattr(wx, name, name)
    for index, name in enumerate(('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL',
            'HORIZONTAL', 'TE_MULTILINE', 'TE_READONLY', 'EXPAND', 'ALL', 'ALIGN_RIGHT',
            'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION', 'FD_OPEN', 'FD_FILE_MUST_EXIST'), 1):
        setattr(wx, name, 1 << index)
    wx.ID_OK, wx.ID_YES, wx.ID_CANCEL, wx.ID_CLOSE = 1, 2, 3, 4
    wx.modal_hook = None
    wx.next_id = 100

    class Event:
        def __init__(self, identity):
            self.identity, self.skipped = identity, False
        def GetId(self):
            return self.identity
        def Skip(self):
            self.skipped = True

    class Window:
        def __init__(self, parent=None, *args, **kwargs):
            wx.next_id += 1
            self.native_id, self.live = wx.next_id, True
            self.parent, self.children, self.handlers = parent, [], {}
            self.value, self.enabled, self.destroy_calls = kwargs.get('value', ''), True, 0
            self.modal = False
            if parent is not None:
                parent.children.append(self)
        def native(self, name):
            if not self.live:
                forbidden.append((self.native_id, name))
                raise RuntimeError('Deleted logical native peer')
        def __bool__(self):
            return self.live
        def GetId(self):
            self.native('GetId'); return self.native_id
        def IsBeingDeleted(self):
            self.native('IsBeingDeleted'); return False
        def Bind(self, event, handler):
            self.native('Bind'); self.handlers[event] = handler
        def SetName(self, name):
            self.native('SetName'); self.name = name
        def Enable(self, enabled=True):
            self.native('Enable'); self.enabled = enabled
        def SetValue(self, value):
            self.native('SetValue'); self.value = value
        def GetValue(self):
            self.native('GetValue'); return self.value
        def GetPath(self):
            self.native('GetPath'); return self.value
        def SetFocus(self):
            self.native('SetFocus')
        def SetSizer(self, sizer):
            self.native('SetSizer')
        def IsModal(self):
            self.native('IsModal'); return self.modal
        def EndModal(self, result):
            self.native('EndModal'); self.modal = False
        def ShowModal(self):
            self.native('ShowModal'); self.modal = True
            return wx.modal_hook(self) if wx.modal_hook else wx.ID_YES
        def native_delete(self):
            if not self.live:
                return
            for child in tuple(self.children):
                child.native_delete()
            self.live = False
            if wx.EVT_WINDOW_DESTROY in self.handlers:
                self.handlers[wx.EVT_WINDOW_DESTROY](Event(self.native_id))
        def Destroy(self):
            self.native('Destroy'); self.destroy_calls += 1
            self.native_delete()
            return True

    class Sizer:
        def __init__(self, *args, **kwargs):
            pass
        def Add(self, *args, **kwargs):
            pass

    wx.Dialog = wx.TextCtrl = wx.Button = wx.MessageDialog = wx.FileDialog = wx.TextEntryDialog = Window
    wx.BoxSizer = Sizer
    path = Path(carr_library.__file__).resolve().parent / 'ui/update_maintenance.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    names = {'_live', 'MaintenanceDialog', 'show_maintenance', 'choose_update_configuration'}
    selected = ast.Module(body=[node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names], type_ignores=[])
    namespace = dict(wx=wx, threading=threading, UpdateConfiguration=UpdateConfiguration,
                     MaintenanceSession=MaintenanceSession, release_text=release_text,
                     PausedDownload=PausedDownload, UpdateCancelled=UpdateCancelled)
    exec(compile(selected, str(path), 'exec'), namespace)

    class Frame(Window):
        def __init__(self, app):
            super().__init__()
            self.library, self.working, self.cancel_event = app, False, threading.Event()
            self.admission, self.queue, self.threads, self.reports, self.calls = 'thread', [], [], [], 0
            self.hub, self.tasks, self.results = ProgressHub(), [], []
        def report(self, value):
            self.native('report'); self.reports.append(value)
        def run_job(self, label, operation, *, done, failed, with_progress=False):
            self.native('run_job'); self.calls += 1
            if self.admission is False or self.admission is None:
                return self.admission
            if isinstance(self.admission, type) and issubclass(self.admission, BaseException):
                raise self.admission('actual host admission rejected')
            self.working = True
            task = self.hub.start(label, unit='bytes') if with_progress else None
            if task is not None:
                self.tasks.append(task)
            def work():
                with bind_progress(task) if task is not None else nullcontext():
                    value = operation()
                self.results.append(value)
                return value
            if self.admission == 'sync':
                try:
                    value = work()
                except (Exception, KeyboardInterrupt) as error:
                    self.working = False; failed(error)
                else:
                    self.working = False; done(value)
                return None
            def run():
                try:
                    value = work()
                except (Exception, KeyboardInterrupt) as error:
                    self.queue.append(lambda error=error: failed(error))
                else:
                    self.queue.append(lambda value=value: done(value))
                finally:
                    self.working = False
            worker = threading.Thread(target=run)
            self.threads.append(worker); worker.start()
            return None
        def drain(self):
            for worker in self.threads:
                worker.join(10)
                assert not worker.is_alive()
            while self.queue:
                self.queue.pop(0)()
    return namespace, wx, Frame, Event, forbidden


@pytest.fixture
def selected(tmp_path):
    fixture = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'security/conftest.py'))
    repository = fixture['Repository'](tmp_path / 'signed-source')
    repository.publish(fixture['bundle']())
    library = LibraryApp(tmp_path / 'research')
    bootstrap = tmp_path / 'bootstrap.json'; bootstrap.write_bytes(repository.bootstrap)
    verifier = Path(__file__).resolve().parents[2] / 'packaging/install_windows.py'
    values = dict(schema='carr-update-owner/1', product='carr-test', channel='stable', current_build=1,
        bootstrap=dict(path=str(bootstrap), sha256=sha256(repository.bootstrap).hexdigest()),
        repository=dict(kind='local', path=str(repository.root)), target=repository.target_path,
        cache=str(tmp_path / 'cache'), installation=dict(kind='standard',
            program=str(tmp_path / 'program/versions/111111111111-1'),
            work=str(tmp_path / 'work'), manifest_sha256='a' * 64,
            setup_sha256='b' * 64, version='0.1.0'),
        anchor=dict(profile_id='synthetic-maintenance', binding='c' * 64,
                    reference='carr/provider/synthetic-maintenance/' + 'a' * 32),
        verifier=dict(path=str(verifier), sha256=sha256(verifier.read_bytes()).hexdigest()),
        helper_python=None)
    config = tmp_path / 'configuration.json'
    raw = json_bytes(values); config.write_bytes(raw)
    session = MaintenanceSession(library, UpdateConfiguration.read(config, sha256(raw).hexdigest()))
    return session, repository


@pytest.mark.parametrize('admission', [False, None, OSError, KeyboardInterrupt])
def test_rejected_actual_host_admission_leaves_no_phantom_operation_or_remote_check(selected, admission):
    session, _repository = selected
    namespace, wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app); frame.admission = admission
    dialog = namespace['MaintenanceDialog'](frame, session)
    dialog.on_check()
    assert frame.calls == 1 and session.release is None and session.package is None
    assert dialog._busy is False and dialog._token is None
    assert dialog.actions[0].enabled and not frame.cancel_event.is_set()
    assert 'not confirmed' in dialog.status.value
    assert forbidden == []


def test_existing_host_owner_is_not_cancelled_or_replaced(selected):
    session, _repository = selected
    namespace, _wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app); frame.working = True
    dialog = namespace['MaintenanceDialog'](frame, session)
    dialog.on_check()
    assert frame.calls == 0 and frame.working and not frame.cancel_event.is_set()
    assert dialog._token is None and not dialog._busy
    assert 'Another operation' in dialog.status.value and forbidden == []


def test_synchronous_accepted_host_runs_real_tuf_check_once_and_does_not_report_rejection(selected):
    session, _repository = selected
    namespace, _wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app); frame.admission = 'sync'
    dialog = namespace['MaintenanceDialog'](frame, session)
    dialog.on_check()
    assert frame.calls == 1 and session.release.build == 2
    assert not dialog._busy and dialog._token is None
    assert 'not confirmed' not in dialog.status.value
    assert dialog.actions[1].enabled and not dialog.actions[2].enabled
    assert forbidden == []


@pytest.mark.parametrize('death', ['direct', 'native-parent'])
@pytest.mark.parametrize('phase', ['pending', 'completed'])
def test_real_signed_worker_is_released_only_after_owner_death_without_late_native_access(
        selected, monkeypatch, death, phase):
    session, repository = selected
    namespace, _wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app)
    dialog = namespace['MaintenanceDialog'](frame, session)
    before = {path.relative_to(session.app.root): path.read_bytes()
              for path in session.app.root.rglob('*') if path.is_file()}
    config_before = session.configuration.path.read_bytes()
    entered, release = threading.Event(), threading.Event()
    actual = session.check
    def check(*, cancelled):
        if phase == 'completed':
            value = actual(cancelled=cancelled)
        entered.set()
        assert release.wait(10)
        return value if phase == 'completed' else actual(cancelled=cancelled)
    monkeypatch.setattr(session, 'check', check)
    try:
        dialog.on_check()
        assert entered.wait(10)
        if death == 'direct':
            assert dialog.Destroy() is True
            assert dialog.Destroy() is False
        else:
            frame.native_delete()  # deliberately bypasses Python Destroy.
        assert not dialog._alive and dialog._cancel.is_set() and not dialog
        assert not frame.cancel_event.is_set()
        release.set(); frame.drain()
        assert session.package is None
        assert (session.release is not None) == (phase == 'completed')
        assert session.configuration.path.read_bytes() == config_before
        assert {path.relative_to(session.app.root): path.read_bytes()
                for path in session.app.root.rglob('*') if path.is_file()} == before
        dialog.on_check(); dialog.on_download(); dialog.on_apply(); dialog.on_recover(); dialog.on_command()
        assert dialog.Destroy() is False and frame.calls == 1 and forbidden == []
    finally:
        release.set(); frame.drain()


def test_child_destruction_does_not_detach_the_real_update_owner(selected):
    session, _repository = selected
    namespace, _wx, Frame, Event, forbidden = logical_ui()
    frame = Frame(session.app)
    dialog = namespace['MaintenanceDialog'](frame, session)
    event = Event(dialog.actions[0].GetId())
    dialog._destroyed(event)
    assert event.skipped and dialog._alive and not dialog._cancel.is_set()
    assert forbidden == []


@pytest.mark.parametrize('recovery', [False, True])
def test_parent_deletion_inside_confirmation_never_reads_or_destroys_dead_modal(selected, recovery):
    session, _repository = selected
    release = session.check(); session.download(release)
    namespace, wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app)
    dialog = namespace['MaintenanceDialog'](frame, session)
    def destroy(chooser):
        frame.native_delete()
        return wx.ID_YES
    wx.modal_hook = destroy
    (dialog.on_recover if recovery else dialog.on_apply)()
    assert not dialog._alive and frame.calls == 0
    assert session.last_result is None and forbidden == []


@pytest.mark.parametrize('stage', ['file', 'digest'])
def test_configuration_chooser_does_not_touch_modal_after_parent_deletion(selected, stage):
    session, _repository = selected
    namespace, wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app)
    count = 0
    def choose(chooser):
        nonlocal count
        count += 1
        chooser.value = str(session.configuration.path) if count == 1 else session.configuration.digest
        if (stage == 'file' and count == 1) or (stage == 'digest' and count == 2):
            frame.native_delete()
        return wx.ID_OK
    wx.modal_hook = choose
    namespace['choose_update_configuration'](frame)
    assert frame.calls == 0 and forbidden == []


def test_initial_configuration_failure_callback_is_inert_after_frame_deletion(selected):
    session, _repository = selected
    namespace, wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app)
    count = 0
    def choose(chooser):
        nonlocal count
        count += 1
        chooser.value = str(session.configuration.path) if count == 1 else session.configuration.digest
        return wx.ID_OK
    wx.modal_hook = choose
    held = {}
    def hold(label, operation, *, done, failed):
        held.update(operation=operation, done=done, failed=failed)
        frame.working = True
    frame.run_job = hold
    namespace['choose_update_configuration'](frame)
    assert held
    frame.native_delete()
    held['failed'](ValueError('unreviewed configuration'))
    held['done'](session)
    assert frame.reports == [] and forbidden == []



def _large_signed_source(session, repository):
    payload = bytes(range(256)) * 4096
    repository.publish(payload)
    return payload


def _hold_actual_prefix(monkeypatch):
    from carr_library.security.download_state import TargetDownload
    entered, released = threading.Event(), threading.Event()
    seen = []
    actual = TargetDownload.report
    def report(transfer, *args):
        actual(transfer, *args)
        if transfer.received >= 65536 and not seen:
            seen.append(transfer.received)
            entered.set()
            assert released.wait(10), 'The test must release its own retained-byte barrier.'
    monkeypatch.setattr(TargetDownload, 'report', report)
    return entered, released, seen


def test_native_handlers_pause_real_signed_download_and_reopen_resume_without_activation(selected, monkeypatch):
    session, repository = selected
    raw = _large_signed_source(session, repository)
    namespace, _wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app)
    dialog = namespace['MaintenanceDialog'](frame, session)
    dialog.on_check(); frame.drain()
    entered, released, seen = _hold_actual_prefix(monkeypatch)
    try:
        dialog.on_download()
        assert entered.wait(10) and dialog._busy and dialog._downloading
        assert dialog.download_actions[0].enabled
        dialog.on_pause_download()
        assert dialog._pause.is_set() and not dialog._cancel.is_set()
        assert not frame.cancel_event.is_set()
        released.set(); frame.drain()
        partial = session.download_state
        assert type(partial) is PausedDownload and partial.received == seen[0]
        assert session.package is None and session.last_result is None
        assert operation_outcome(frame.results[-1]) == 'paused'
        progress = frame.tasks[-1].snapshot()
        assert progress.completed == partial.received and progress.total == len(raw)
        assert progress.percent < 100
        assert 'fragment cannot be installed' in dialog.status.value
        assert not dialog.actions[1].enabled and not dialog.actions[2].enabled
        assert dialog.download_actions[1].enabled and dialog.download_actions[2].enabled
        retained = next(Path(session.configuration.checked()['cache']).glob('downloads/.paused-*'))
        before = {path.name: path.read_bytes() for path in retained.iterdir()}
        dialog.Destroy()
        assert {path.name: path.read_bytes() for path in retained.iterdir()} == before

        reopened = MaintenanceSession(session.app, session.configuration)
        frame2 = Frame(session.app)
        view = namespace['MaintenanceDialog'](frame2, reopened)
        assert reopened.download_state is None, 'Construction is passive.'
        view.on_check(); frame2.drain()
        assert reopened.download_state == partial and reopened.package is None
        assert view.download_actions[1].enabled and not view.actions[2].enabled
        view.on_resume_download(); frame2.drain()
        assert reopened.package.path.read_bytes() == raw
        assert reopened.download_state is None and reopened.last_result is None
        assert operation_outcome(frame2.results[-1]) == 'succeeded'
        assert not retained.exists() and not frame2.cancel_event.is_set()
        assert view.actions[2].enabled and not view.download_actions[1].enabled
        assert forbidden == []
    finally:
        released.set(); frame.drain()


@pytest.mark.parametrize('action', ['cancel', 'direct', 'native-parent'])
def test_active_download_cancel_or_native_death_releases_only_its_actual_partial(
        selected, monkeypatch, action):
    session, repository = selected
    raw = _large_signed_source(session, repository)
    namespace, _wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app)
    dialog = namespace['MaintenanceDialog'](frame, session)
    dialog.on_check(); frame.drain()
    cache = Path(session.configuration.checked()['cache'])
    unrelated = cache / 'downloads/user-note.txt'
    unrelated.write_bytes(b'keep this separate file')
    entered, released, _seen = _hold_actual_prefix(monkeypatch)
    try:
        dialog.on_download()
        assert entered.wait(10)
        if action == 'cancel':
            dialog.on_cancel_download()
        elif action == 'direct':
            dialog.Destroy()
        else:
            frame.native_delete()
        assert dialog._cancel.is_set() and not frame.cancel_event.is_set()
        released.set(); frame.drain()
        assert session.package is None and session.download_state is None
        assert session.last_result is None
        assert operation_outcome(frame.results[-1]) == 'cancelled'
        assert not tuple(cache.glob('downloads/.paused-*')) and not tuple(cache.glob('downloads/*.zip'))
        assert unrelated.read_bytes() == b'keep this separate file'
        target = repository.md['targets'].signed.targets[repository.target_path]
        original = repository.root / 'targets' / (target.hashes['sha256'] + '.release.zip')
        assert original.read_bytes() == raw
        if action != 'cancel':
            calls = frame.calls
            dialog.on_pause_download(); dialog.on_resume_download(); dialog.on_cancel_download()
            assert frame.calls == calls and not dialog._alive
        assert forbidden == []
    finally:
        released.set(); frame.drain()


@pytest.mark.parametrize('death', ['direct', 'native-parent'])
def test_actual_verified_download_completed_before_owner_death_is_retained_without_late_widgets(
        selected, monkeypatch, death):
    session, repository = selected
    raw = _large_signed_source(session, repository)
    namespace, _wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app)
    dialog = namespace['MaintenanceDialog'](frame, session)
    dialog.on_check(); frame.drain()
    entered, released = threading.Event(), threading.Event()
    actual = session.download
    def held(*args, **kwargs):
        value = actual(*args, **kwargs)
        assert value.path.read_bytes() == raw
        entered.set()
        assert released.wait(10)
        return value
    monkeypatch.setattr(session, 'download', held)
    try:
        dialog.on_download()
        assert entered.wait(10)
        package = session.package
        before = package.path.read_bytes()
        if death == 'direct':
            dialog.Destroy()
        else:
            frame.native_delete()
        released.set(); frame.drain()
        assert session.package is package and package.path.read_bytes() == before == raw
        assert session.last_result is None
        assert frame.results[-1] is package and operation_outcome(package) == 'succeeded'
        assert not frame.cancel_event.is_set() and forbidden == []
    finally:
        released.set(); frame.drain()


@pytest.mark.parametrize('admission', [False, None, OSError, KeyboardInterrupt])
def test_resume_admission_failure_keeps_real_checkpoint_and_allows_explicit_retry(
        selected, monkeypatch, admission):
    session, repository = selected
    raw = _large_signed_source(session, repository)
    release = session.check()
    stop = threading.Event()
    paused = session.download(release, paused=stop.is_set,
                              progress=lambda done, total: stop.set() if done >= 65536 else None)
    assert type(paused) is PausedDownload
    cache = Path(session.configuration.checked()['cache'])
    folder = next(cache.glob('downloads/.paused-*'))
    before = {path.name: path.read_bytes() for path in folder.iterdir()}
    namespace, _wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app); frame.admission = admission
    dialog = namespace['MaintenanceDialog'](frame, session)
    dialog.on_resume_download()
    assert not dialog._busy and dialog._token is None and not dialog._downloading
    assert dialog.download_actions[1].enabled and dialog.download_actions[2].enabled
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == before
    assert session.package is None and not frame.cancel_event.is_set()
    frame.admission = 'thread'
    dialog.on_resume_download(); frame.drain()
    assert session.package.path.read_bytes() == raw and session.last_result is None
    assert forbidden == []


def test_idle_cancel_and_pause_do_not_cancel_an_unrelated_desktop_operation(selected):
    session, repository = selected
    _large_signed_source(session, repository)
    release = session.check()
    stop = threading.Event()
    session.download(release, paused=stop.is_set,
                     progress=lambda done, total: stop.set() if done >= 65536 else None)
    namespace, _wx, Frame, _Event, forbidden = logical_ui()
    frame = Frame(session.app); frame.working = True
    dialog = namespace['MaintenanceDialog'](frame, session)
    retained = session.download_state
    dialog.on_pause_download(); dialog.on_cancel_download()
    assert frame.working and not frame.cancel_event.is_set()
    assert frame.calls == 0 and session.download_state is retained
    assert not dialog._cancel.is_set() and not dialog._busy and forbidden == []
