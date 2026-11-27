"""Native-controller lifecycle tests, without downloading or executing a runtime."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

import pytest

from carr_library.ui import audio_runtime_setup as ui
from carr_library.runtime_settings import load_settings


class Binding:
    def __init__(self, root, *, notice=''):
        self.settings = load_settings(root / 'state')
        self.directory = root / 'reviewed-runtime'
        self.report = {'manifest_sha256': 'a' * 64}
        self.notice = notice
        self.closed, self.activations = False, []
    def activate(self, **options):
        self.activations.append(options)
        return self
    def close(self):
        self.closed = True


class Frame:
    def __init__(self, root):
        self.library = SimpleNamespace(root=root)
        self._closing = False
        self.working = False
        self.cancel_event = threading.Event()
        self.audio_runtime_binding = object()
        self.events = []
    def run_job(self, title, operation, done, failed, *, with_progress, discard=None):
        assert with_progress
        assert not self.working
        self.working = True
        self.cancel_event.clear()
        self.operation, self.done, self.failed = operation, done, failed
        self.discard = discard
    def perform(self):
        return self.operation(lambda completed, total, phase: self.events.append((completed, total, phase)))
    def finish(self, result):
        self.working = False
        try:
            self.done(result)
        except Exception as error:
            self.failed(error)


def test_setup_service_runs_only_in_shared_worker_and_activates_in_completion(tmp_path, monkeypatch):
    frame = Frame(tmp_path)
    controller = ui.AudioSetupController(frame)
    binding = Binding(tmp_path)
    calls, result, errors = [], [], []
    main_thread = threading.get_ident()
    def select(*args, **kwargs):
        calls.append(threading.get_ident())
        kwargs['progress']('Verified bytes', 2, 3)
        assert kwargs['expected_settings'] == controller.settings
        return {'status': 'REGISTERED', 'binding': binding}
    monkeypatch.setattr(ui, 'install_or_select', select)
    controller.start(existing=tmp_path, complete=lambda value, error: result.append((value, error)))
    assert not calls and not binding.activations and controller.busy
    def worker():
        try: result.append(frame.perform())
        except BaseException as error: errors.append(error)
    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive() and not errors
    receipt = result.pop()
    assert calls and calls[0] != main_thread
    assert not binding.activations and frame.events == [(2, 3, 'Verified bytes')]
    old = frame.audio_runtime_binding
    frame.finish(receipt)
    assert binding.activations == [{'previous': old, 'strict': False}]
    assert frame.audio_runtime_binding is binding
    assert receipt['status'] == 'COMPLETED' and not controller.busy
    assert result[0][0]['status'] == 'ACTIVE' and result[0][1] is None


@pytest.mark.parametrize('change', ['closed', 'library', 'closing', 'cancelled'])
def test_late_completion_never_activates_closed_cancelled_or_changed_scope(tmp_path, monkeypatch, change):
    frame = Frame(tmp_path)
    controller = ui.AudioSetupController(frame)
    binding = Binding(tmp_path)
    called = []
    monkeypatch.setattr(ui, 'install_or_select',
                        lambda *a, **k: {'status': 'REGISTERED', 'binding': binding})
    controller.start(existing=tmp_path, complete=lambda *args: called.append(args))
    result = frame.perform()
    old = frame.audio_runtime_binding
    if change == 'closed':
        controller.close()
    elif change == 'library':
        frame.library = SimpleNamespace(root=tmp_path / 'other')
    elif change == 'closing':
        frame._closing = True
    else:
        controller.cancel()
    frame.finish(result)
    assert binding.closed and not binding.activations
    assert frame.audio_runtime_binding is old
    if change == 'cancelled':
        assert called[0][0]['status'] == 'REGISTERED_NOT_ACTIVATED'
        assert 'saved' in called[0][0]['message']
    else:
        assert not called


def test_cancel_before_worker_does_not_call_setup_or_network(tmp_path, monkeypatch):
    frame = Frame(tmp_path)
    controller = ui.AudioSetupController(frame)
    monkeypatch.setattr(ui, 'install_or_select', lambda *a, **k: pytest.fail('cancelled setup ran'))
    controller.start(download=True, complete=lambda *a: None)
    assert controller.cancel()
    with pytest.raises(InterruptedError):
        frame.perform()


def test_invalid_prepared_selection_is_installed_as_a_blocked_binding(tmp_path, monkeypatch):
    frame = Frame(tmp_path)
    controller = ui.AudioSetupController(frame)
    binding = Binding(tmp_path, notice='Selected manifest is missing.')
    called = []
    monkeypatch.setattr(ui, 'prepare_local_audio_runtime', lambda *a, **k: binding)
    controller.start(verify=True, complete=lambda *a: called.append(a))
    frame.finish(frame.perform())
    assert frame.audio_runtime_binding is binding and len(binding.activations) == 1
    assert called[0][0] is None and 'manifest is missing' in str(called[0][1])


def test_another_shared_job_prevents_audio_setup_submission(tmp_path):
    frame = Frame(tmp_path)
    frame.working = True
    controller = ui.AudioSetupController(frame)
    with pytest.raises(ValueError, match='current operation'):
        controller.start(download=True, complete=lambda *a: None)
    assert not hasattr(frame, 'operation')


def test_native_download_confirmation_defaults_to_no(monkeypatch):
    style = []
    class Dialog:
        def __init__(self, parent, message, title, flags):
            style.append(flags)
            assert 'SHA256' in message and 'redistribute' in message
        def ShowModal(self): return 200
        def Destroy(self): style.append('destroyed')
    fake = SimpleNamespace(MessageDialog=Dialog, YES_NO=1, NO_DEFAULT=2,
                           ICON_QUESTION=4, ID_YES=100)
    monkeypatch.setitem(sys.modules, 'wx', fake)
    assert ui.confirm_download(object()) is False
    assert style == [7, 'destroyed']


def test_existing_studio_menu_gets_one_native_action_without_shortcut_collision(monkeypatch):
    bindings, labels = [], []
    menu = SimpleNamespace(Append=lambda ident, label: labels.append(label) or 'action')
    bar = SimpleNamespace(GetMenuCount=lambda: 2,
        GetMenuLabel=lambda index: ('&Help', 'Audio &Studio')[index], GetMenu=lambda index: menu)
    frame = SimpleNamespace(menu_bar=bar, Bind=lambda *args: bindings.append(args))
    fake = SimpleNamespace(ID_ANY=-1, EVT_MENU='menu')
    monkeypatch.setitem(sys.modules, 'wx', fake)
    ui.install_audio_runtime_setup(frame)
    assert labels == ['Local audio runtime setup...'] and len(bindings) == 1
    assert '\t' not in labels[0]


def test_forced_frame_close_disposes_prepared_binding_even_if_done_is_suppressed(tmp_path, monkeypatch):
    frame = Frame(tmp_path)
    controller = ui.AudioSetupController(frame)
    binding = Binding(tmp_path)
    monkeypatch.setattr(ui, 'prepare_local_audio_runtime', lambda *a, **k: binding)
    controller.start(verify=True, complete=lambda *a: pytest.fail('closed frame completed on UI'))
    result = frame.perform()
    old = frame.audio_runtime_binding
    frame._closing = True
    # The shared run_job owns this close race and calls discard instead of done.
    assert callable(frame.discard)
    frame.discard(result)
    assert binding.closed and not binding.activations and frame.audio_runtime_binding is old


@pytest.mark.parametrize('shortcut,flags,code', [('Ctrl+Alt+G', 3, ord('G')),
                                               ('Alt+F9', 2, 348), ('', None, None)])
def test_modal_progress_reuses_current_menu_binding_including_unassigned(monkeypatch, shortcut, flags, code):
    calls, tables = [], []
    entry = None if not shortcut else SimpleNamespace(GetFlags=lambda: flags, GetKeyCode=lambda: code)
    item = SimpleNamespace(GetAccel=lambda: entry, GetId=lambda: 71,
                           GetItemLabel=lambda: 'Current operation progress' + ('\t' + shortcut if shortcut else ''))
    binding = SimpleNamespace(command=SimpleNamespace(identity='Help/Current operation progress'), item=item)
    frame = SimpleNamespace(keyboard_actions=SimpleNamespace(bindings=(binding,)))
    dialog = SimpleNamespace(Bind=lambda *a, **k: calls.append((a, k)),
                             SetAcceleratorTable=lambda table: tables.append(table))
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(EVT_MENU='menu', AcceleratorTable=lambda entries: entries))
    handler = object()
    assert ui._bind_modal_progress_query(frame, dialog, handler) == shortcut
    if shortcut:
        assert tables == [[(flags, code, 71)]]
        assert calls == [(('menu', handler), {'id': 71})]
    else:
        assert not tables and not calls
