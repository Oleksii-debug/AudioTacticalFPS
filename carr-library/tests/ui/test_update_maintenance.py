"""Actual maintenance closures, real signed review and explicit logical wx peers.

The fake controls record every forbidden post-deletion native access. Worker
operations below run on actual threads and use the existing TUF client/store.
Only wx and the Desktop queue scheduling are supplied by this portable fixture.
"""
import ast
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
                with bind_progress(task):
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
