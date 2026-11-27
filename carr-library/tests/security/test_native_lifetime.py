"""Real client callbacks with explicit native-control doubles; not Windows proof."""
from hashlib import sha256
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

import carr_library
from carr_library.security.controller import TrustController
from carr_library.security.entitlements import EntitlementStore
from carr_library.security.updates import LocalRepositoryClient
from .conftest import MemoryAnchor, bundle, envelope, grant


@pytest.fixture
def native(monkeypatch):
    modal = {}
    controls = []

    class Control:
        def __init__(self, parent=None, *args, title='', value='', label='', **kwargs):
            self.parent, self.title, self.value = parent, title, value
            self.label, self.name = label, ''
            self.alive, self.enabled = True, True
            self.children, self.bindings = [], {}
            self.identity = len(controls) + 1
            controls.append(self)
            if parent is not None:
                parent.children.append(self)

        def GetId(self):
            return self.identity

        def Bind(self, event, callback, *args):
            self.bindings.setdefault(event, []).append(callback)

        def SetName(self, name):
            self.name = name

        def GetName(self):
            return self.name

        def GetValue(self):
            assert self.alive, 'A destroyed native value was read.'
            return self.value

        def SetValue(self, value):
            assert self.alive
            self.value = value

        def Enable(self, value=True):
            assert self.alive, 'A destroyed native control was enabled.'
            self.enabled = value

        def Disable(self):
            self.Enable(False)

        def SetLabel(self, value):
            assert self.alive
            self.label = value

        def SetFocus(self):
            assert self.alive, 'A destroyed native control was focused.'

        def SetSizer(self, value):
            assert self.alive

        def CreateStdDialogButtonSizer(self, flags):
            self.ok = Control(self)
            return SimpleNamespace()

        def FindWindow(self, identity):
            return self.ok

        def ShowModal(self):
            assert self.alive
            action = modal.get(self.title)
            if action is not None:
                action(self)
            return 1

        def EndModal(self, result):
            assert self.alive, 'A destroyed modal was ended.'

        def GetPath(self):
            assert self.alive, 'A destroyed file chooser was read.'
            return '/synthetic-reviewed-file'

        def Destroy(self):
            assert self.alive, 'A native window was destroyed twice.'
            event = SimpleNamespace(GetId=lambda: self.identity, GetEventObject=lambda: object(),
                                    Skip=lambda: None)
            for callback in self.bindings.get('destroy', ()):
                callback(event)
            for child in self.children:
                if child.alive:
                    child.Destroy()
            self.alive = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.Destroy()

    class Chooser(Control):
        def __init__(self, parent, title, **kwargs):
            super().__init__(parent, title=title, **kwargs)

    class Timer(Control):
        def __init__(self, frame):
            super().__init__(frame)
            self.running = False

        def Start(self, milliseconds):
            assert self.alive
            self.running = True

        def Stop(self):
            assert self.alive
            self.running = False

    class Frame(Control):
        def __init__(self, controller=None):
            super().__init__()
            self.trust_controller = controller
            self.working = self._closing = False
            self.cancel_event = threading.Event()
            self.jobs, self.reports = [], []

        def GetMenuBar(self):
            menu = SimpleNamespace(Append=lambda *args: object())
            return SimpleNamespace(GetMenuCount=lambda: 1, GetMenuLabelText=lambda i: 'Help',
                                   GetMenu=lambda i: menu)

        def run_job(self, title, work, *, done=None, failed=None):
            assert self.alive, 'A job was submitted through a destroyed owner.'
            self.jobs.append(SimpleNamespace(title=title, work=work, done=done, failed=failed))

        def report(self, value):
            assert self.alive, 'A late callback touched the destroyed owner.'
            self.reports.append(value)

    wx = SimpleNamespace(Dialog=Control, TextCtrl=Control, CheckBox=Control, Button=Control,
        FileDialog=Chooser, DirDialog=Chooser, Timer=Timer, Window=SimpleNamespace(FindFocus=lambda: None),
        BoxSizer=lambda *args: SimpleNamespace(Add=lambda *a, **k: None),
        ID_OK=1, ID_CANCEL=2, ID_ANY=3, VERTICAL=0, HORIZONTAL=1,
        DEFAULT_DIALOG_STYLE=0, RESIZE_BORDER=0, TE_MULTILINE=1, TE_READONLY=2,
        EXPAND=1, ALL=2, ALIGN_RIGHT=4, OK=8, CANCEL=16, FD_OPEN=32, FD_FILE_MUST_EXIST=64,
        EVT_CHECKBOX='check', EVT_BUTTON='button', EVT_MENU='menu', EVT_TIMER='timer',
        EVT_WINDOW_DESTROY='destroy')
    monkeypatch.setitem(sys.modules, 'wx', wx)
    path = Path(carr_library.__file__).resolve().parent / 'ui' / 'trust_status.py'
    spec = importlib.util.spec_from_file_location('carr_library.ui._trust_lifetime_fixture', path)
    ui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ui)

    def owner(controller=None):
        frame = Frame(controller)
        ui.install_trust_actions(frame)
        return frame

    return ui, owner, modal


@pytest.fixture
def reviewed(tmp_path, signer, policy, clock, integration_context):
    terms = 'Synthetic reviewed terms for destruction tests.'
    digest = sha256(terms.encode()).hexdigest()
    store = EntitlementStore(tmp_path / 'client', policy, MemoryAnchor(), clock=clock)
    path = tmp_path / 'permission.json'
    path.write_bytes(envelope(signer, grant(clock, terms_sha256=digest)))
    controller = TrustController(entitlement=store, terms={digest: terms}, context=integration_context)
    return controller, store, controller.preview_file(path)


def test_parent_destroy_during_permission_review_never_accepts_dead_checkbox(native, reviewed):
    ui, owner, modal = native
    controller, store, review = reviewed
    frame = owner(controller)

    def destroy(dialog):
        dialog.accepted.SetValue(True)
        frame.Destroy()

    modal['Review signed permission'] = destroy
    ui.show_permission_review(frame, controller, review)
    assert not frame.jobs and store.anchor.load().sequence == 0
    assert not store.current_path.exists() and not store.pending_path.exists()


def test_parent_destroy_during_update_review_never_prepares_payload(native, repository, tmp_path, integration_context):
    ui, owner, modal = native
    repository.publish(bundle())
    client = LocalRepositoryClient(tmp_path / 'cache', repository.policy())
    controller = TrustController(context=integration_context, updates=client, target=repository.target_path)
    frame = owner(controller)
    release = controller.check_repository(repository.root)
    modal['Review signed update'] = lambda dialog: frame.Destroy()
    ui.show_update_review(frame, controller, repository.root, release)
    assert not frame.jobs and not list(client.downloads.iterdir())


def test_parent_destroy_during_status_display_does_not_double_destroy(native):
    ui, owner, modal = native
    frame = owner()
    modal['License and signed updates'] = lambda dialog: frame.Destroy()
    ui.show_trust_status(frame)
    assert not frame.jobs and not frame.reports


@pytest.mark.parametrize('action,title', [
    ('on_import', 'Choose the signed permission file'),
    ('on_update', 'Choose the signed offline repository folder'),
])
def test_parent_destroy_in_native_chooser_never_submits_action(native, reviewed, action, title):
    ui, owner, modal = native
    controller, store, review = reviewed
    frame = owner(controller)
    dialog = ui.TrustStatusDialog(frame, controller)
    modal[title] = lambda chooser: frame.Destroy()
    getattr(dialog, action)()
    assert not frame.jobs and store.anchor.load().sequence == 0


def test_destroy_before_queued_approved_import_retains_original_protected_state(native, reviewed):
    ui, owner, modal = native
    controller, store, review = reviewed
    frame = owner(controller)
    modal['Review signed permission'] = lambda dialog: dialog.accepted.SetValue(True)
    ui.show_permission_review(frame, controller, review)
    job, = frame.jobs
    frame.Destroy()
    with pytest.raises(InterruptedError):
        job.work()
    job.done(SimpleNamespace(sequence=1))
    job.failed(ValueError('Synthetic late failure'))
    assert not frame.reports and store.anchor.load().sequence == 0
    assert not store.current_path.exists() and not store.pending_path.exists()


def test_destroy_after_approved_import_commit_retains_result_without_late_controls(native, reviewed):
    ui, owner, modal = native
    controller, store, review = reviewed
    frame = owner(controller)
    modal['Review signed permission'] = lambda dialog: dialog.accepted.SetValue(True)
    ui.show_permission_review(frame, controller, review)
    job, = frame.jobs
    result = job.work()
    assert result.sequence == 1
    frame.Destroy()
    job.done(result)
    assert store.current().sequence == 1 and store.current_path.read_bytes() == review.raw
    assert not frame.reports
