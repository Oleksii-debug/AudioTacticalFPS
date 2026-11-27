"""Faithful native interfaces plus the real existing single-worker controller."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import gc
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import threading
import weakref

import pytest

from carr_library.audio.voice_lifecycle import ProtectedVoicePackageReview
from carr_library.audio.voice_packages import VoicePackageReview
from carr_library.ui import voice_packages as ui

PASSWORD = 'UI-only protected fixture password'


def protected_review(root):
    document = {'name': 'Synthetic', 'id': 'fixture', 'version': '1.0.0',
        'engine': {'id': 'omnivoice'}, 'languages': ['uk'],
        'files': {'sample.txt': {'role': 'reference_text'}},
        'provenance': {'source': 'synthetic UI fixture', 'license': 'Fixture'}}
    return ProtectedVoicePackageReview(str(root), (1, 2), 'a' * 64, 1,
        str(root.parent / 'selected.carr-voice.enc'), 'b' * 64, 100,
        'c' * 64, 50, json.dumps(document))


@pytest.fixture
def view(tmp_path, monkeypatch):
    main = threading.get_ident()
    callbacks, queued, callback_lock = [], threading.Event(), threading.Lock()
    pools, calls, failures = [], [], []
    wx = SimpleNamespace(picker_path=str(tmp_path / 'selected.carr-voice.enc'),
        on_picker=lambda: None, on_password=lambda: None)
    for index, name in enumerate((
            'DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
            'TE_MULTILINE', 'TE_READONLY', 'TE_PASSWORD', 'EXPAND', 'ALL', 'LEFT', 'TOP',
            'ALIGN_CENTER_VERTICAL', 'ALIGN_RIGHT', 'FD_OPEN', 'FD_FILE_MUST_EXIST',
            'FD_SAVE', 'OK', 'CANCEL', 'ICON_INFORMATION')):
        setattr(wx, name, 1 << index)
    wx.ID_CLOSE, wx.ID_CANCEL, wx.ID_OK = 5102, 5101, 5100
    wx.EVT_TEXT, wx.EVT_CHOICE, wx.EVT_BUTTON = 'text', 'choice', 'button'
    wx.EVT_CLOSE, wx.EVT_WINDOW_DESTROY = 'close', 'destroy'
    wx.picker_answer = wx.ID_OK
    ids = iter(range(100, 10000))
    widgets = []

    class Widget:
        def __init__(self, parent=None, *args, value='', label='', choices=(), title='', **kwargs):
            self.parent, self.value, self.label = parent, value, label
            self.choices, self.selection, self.title = list(choices), 0, title
            self.id, self.dead = next(ids), False
            self.bindings, self.enabled, self.destroy_count = {}, True, 0
            widgets.append(self)
        def live(self):
            assert threading.get_ident() == main, 'Native control accessed on worker.'
            current = self
            while isinstance(current, Widget):
                assert not current.dead, 'Deleted native wrapper was touched.'
                current = current.parent
        def __bool__(self):
            current = self
            while isinstance(current, Widget):
                if current.dead:
                    return False
                current = current.parent
            return True
        def SetName(self, value): self.live(); self.name = value
        def GetLabel(self): self.live(); return self.label
        def GetId(self): self.live(); return self.id
        def GetValue(self): self.live(); return self.value
        def SetValue(self, value): self.live(); self.value = value
        def ChangeValue(self, value): self.SetValue(value)
        def SetSelection(self, value): self.live(); self.selection = value
        def GetSelection(self): self.live(); return self.selection
        def GetStringSelection(self): self.live(); return self.choices[self.selection]
        def SetStringSelection(self, value): self.live(); self.selection = self.choices.index(value)
        def SetFocus(self): self.live()
        def SetInsertionPoint(self, value): self.live()
        def SetMinSize(self, value): self.live()
        def SetSizer(self, value): self.live()
        def Wrap(self, value): self.live()
        def Enable(self, value=True): self.live(); self.enabled = value
        def Bind(self, event, callback): self.live(); self.bindings[event] = callback
        def IsModal(self): self.live(); return False
        def EndModal(self, value): self.live()
        def Hide(self): self.live()
        def SetAffirmativeId(self, value): self.live()
        def SetEscapeId(self, value): self.live()
        def CreateStdDialogButtonSizer(self, style): self.live(); return Sizer()
        def ShowModal(self):
            self.live()
            if self.title == 'Protected voice package password':
                return wx.on_password_window(self)
            return wx.ID_CANCEL
        def Destroy(self):
            self.live()
            self.destroy_count += 1
            self.dead = True
            event = SimpleNamespace(GetId=lambda: self.id, Skip=lambda: None,
                GetEventObject=lambda: pytest.fail('Do not touch a dying owner proxy.'))
            if wx.EVT_WINDOW_DESTROY in self.bindings:
                self.bindings[wx.EVT_WINDOW_DESTROY](event)

    class CheckBox(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, value=False, **kwargs)

    class Sizer:
        def __init__(self, *args, **kwargs): pass
        def Add(self, *args, **kwargs): assert threading.get_ident() == main
        def AddGrowableCol(self, *args, **kwargs): assert threading.get_ident() == main

    class Picker(Widget):
        def ShowModal(self): self.live(); wx.on_picker(); return wx.picker_answer
        def GetPath(self): self.live(); return wx.picker_path

    for name in ('Dialog', 'TextCtrl', 'StaticText', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.CheckBox, wx.FileDialog = CheckBox, Picker
    wx.BoxSizer = wx.WrapSizer = wx.FlexGridSizer = Sizer
    wx.MessageBox = lambda *a, **k: wx.ID_OK
    def call_after(callback):
        with callback_lock:
            callbacks.append(callback)
            queued.set()
    wx.CallAfter = call_after
    monkeypatch.setitem(sys.modules, 'wx', wx)

    root = tmp_path / 'library'
    root.mkdir()
    manager = SimpleNamespace(store=SimpleNamespace(projects=SimpleNamespace(root=root)))
    manager.configuration = lambda **kwargs: dict(revision=1, reference_engine='omnivoice',
        models='models', reference_model='models/Holos', references='models/voices')
    review = protected_review(root)
    def inspect(path, *, passphrase, cancelled):
        assert threading.get_ident() != main
        assert passphrase == PASSWORD and not cancelled()
        calls.append(('inspect', Path(path)))
        return review
    def apply(value, *, passphrase, rights_approved, supported_languages, cancelled):
        assert threading.get_ident() != main
        assert passphrase == PASSWORD and rights_approved is True
        assert supported_languages == ('uk',) and not cancelled()
        calls.append(('apply', value))
        return {'revision': 2}
    def export(value, path, *, passphrase, export_rights_approved, cancelled):
        assert threading.get_ident() != main
        assert passphrase == PASSWORD and export_rights_approved is True and not cancelled()
        calls.append(('export', value, Path(path)))
        return dict(path=str(path), sha256='d' * 64, encrypted=True, size_bytes=100)
    lifecycle = SimpleNamespace(inspect_protected=inspect, apply_protected=apply,
                                export_protected=export)
    monkeypatch.setattr(ui, 'VoicePackageManager', lambda root: manager)
    monkeypatch.setattr(ui, 'VoiceProfileLifecycle', lambda root, manager: lifecycle)
    def pool(*args, **kwargs):
        executor = ThreadPoolExecutor(*args, **kwargs)
        pools.append(executor)
        return executor
    monkeypatch.setattr(ui, 'ThreadPoolExecutor', pool)
    frame = SimpleNamespace(library=SimpleNamespace(root=root), _closing=False)
    controller = ui.VoicePackageDialog(frame, root)
    def finish():
        assert queued.wait(5), 'The existing worker did not deliver completion.'
        with callback_lock:
            ready = callbacks[:]
            callbacks.clear()
            queued.clear()
        for callback in ready:
            callback()
    finish()
    def password(*, confirm=False):
        wx.on_password()
        return PASSWORD
    controller._password = password
    host = SimpleNamespace(controller=controller, wx=wx, lifecycle=lifecycle, manager=manager,
        calls=calls, review=review, root=root, finish=finish, pools=pools, widgets=widgets,
        callbacks=callbacks, queued=queued, frame=frame)
    try:
        yield host
    finally:
        controller.close()
        for executor in pools:
            executor.shutdown(wait=True, cancel_futures=True)
        if not controller.destroyed:
            controller.dialog.Destroy()


def inspect_ready(host):
    assert host.controller.inspect_protected()
    host.finish()
    assert host.controller.review is host.review


def installed_review(host):
    old = host.review
    review = VoicePackageReview('activate', old.manifest_sha256, old.settings_revision,
                               'fixture', old.plaintext_sha256, old.document_json)
    host.controller.reviewed(review)
    return review


def test_existing_worker_inspect_then_fresh_rights_and_password_apply(view):
    host = view
    c = host.controller
    inspect_ready(host)
    assert not c.rights.GetValue() and c.supported.GetValue() == ''
    assert c.apply() is False
    assert [call[0] for call in host.calls] == ['inspect']
    c.rights.SetValue(True)
    c.supported.ChangeValue('uk')
    assert c.apply()
    assert c.review is None and not c.rights.GetValue()
    host.finish()
    assert [call[0] for call in host.calls] == ['inspect', 'apply']
    assert len(host.pools) == 1 and c.revision == 2
    assert PASSWORD not in c.status.GetValue()


@pytest.mark.parametrize('changed', ['permission', 'language', 'review', 'cancel', 'scope', 'destroy'])
def test_apply_rechecks_all_inputs_after_password_modal(view, changed):
    host, c = view, view.controller
    inspect_ready(host)
    c.rights.SetValue(True)
    c.supported.ChangeValue('uk')
    def alter():
        if changed == 'permission':
            c.rights.SetValue(False)
        elif changed == 'language':
            c.supported.ChangeValue('sk')
        elif changed == 'review':
            c.invalidate()
        elif changed == 'cancel':
            c.stop()
        elif changed == 'scope':
            host.frame.library = SimpleNamespace(root=host.root)
        else:
            c.dialog.Destroy()
    host.wx.on_password = alter
    assert c.apply() is False
    assert [call[0] for call in host.calls] == ['inspect']


@pytest.mark.parametrize('changed', ['permission', 'review', 'cancel', 'scope', 'destroy'])
def test_export_rechecks_review_after_picker_and_password_modal(view, changed):
    host, c = view, view.controller
    installed_review(host)
    c.protected_export_rights.SetValue(True)
    def alter():
        if changed == 'permission':
            c.protected_export_rights.SetValue(False)
        elif changed == 'review':
            c.invalidate()
        elif changed == 'cancel':
            c.stop()
        elif changed == 'scope':
            host.frame.library = SimpleNamespace(root=host.root)
        else:
            c.dialog.Destroy()
    host.wx.on_password = alter
    assert c.export_protected() is False
    assert host.calls == []


def test_protected_export_has_separate_permission_and_existing_plain_route(view):
    host, c = view, view.controller
    review = installed_review(host)
    c.export_rights.SetValue(True)
    assert c.export_protected() is False and host.calls == []
    c.protected_export_rights.SetValue(True)
    assert c.export_protected()
    host.finish()
    assert host.calls == [('export', review, Path(host.wx.picker_path))]
    assert 'encrypted reference package saved' in c.status.GetValue()
    assert not c.export_rights.GetValue() and not c.protected_export_rights.GetValue()
    assert PASSWORD not in c.status.GetValue()


def test_cancelled_inspection_completion_never_becomes_apply_authority(view):
    host, c = view, view.controller
    assert c.inspect_protected()
    assert host.queued.wait(5)
    c.stop()
    host.finish()
    assert c.review is None and not c.rights.GetValue()
    assert [call[0] for call in host.calls] == ['inspect']


def test_direct_destroy_discards_completed_review_without_touching_controls(view):
    host, c = view, view.controller
    assert c.inspect_protected()
    assert host.queued.wait(5)
    c.dialog.Destroy()
    host.finish()
    assert c.closed and c.destroyed and c.review is None
    assert [call[0] for call in host.calls] == ['inspect']


def test_queued_cancel_drops_secret_even_when_gui_completion_is_suppressed(view):
    host, c = view, view.controller
    started, release = threading.Event(), threading.Event()
    blocker = c.executor.submit(lambda: (started.set(), release.wait(5)))
    assert started.wait(5)
    class Secret(str):
        pass
    password = Secret(PASSWORD)
    retained = weakref.ref(password)
    try:
        assert c._start_secret(lambda value: pytest.fail('Cancelled queued operation ran.'),
                               lambda value: pytest.fail('Destroyed GUI received completion.'), password)
        del password
        assert retained() is not None
        c.dialog.Destroy()
        gc.collect()
        assert retained() is None
    finally:
        release.set()
        blocker.result(timeout=5)
    assert c.closed and c.destroyed


def test_secret_submit_failure_clears_handoff_and_restores_idle_state(view, monkeypatch):
    c = view.controller
    observed = []
    def reject(action):
        observed.append(action)
        raise RuntimeError('Synthetic existing-worker rejection.')
    monkeypatch.setattr(c.executor, 'submit', reject)
    class Secret(str):
        pass
    password = Secret(PASSWORD)
    retained = weakref.ref(password)
    try:
        c._start_secret(lambda value: None, lambda value: None, password)
    except RuntimeError:
        pass
    else:
        pytest.fail('The rejected submission must be visible.')
    del password
    gc.collect()
    assert retained() is None and not c.busy
    assert len(observed) == 1


@pytest.mark.parametrize('destroy', [False, True])
def test_native_password_entry_clears_controls_and_uses_captured_destroy_identity(view, destroy):
    host, c, wx = view, view.controller, view.wx
    entries = []
    def on_password(window):
        fields = [widget for widget in host.widgets if widget.parent is window
                  and getattr(widget, 'name', '').endswith('protected voice operation only')]
        assert len(fields) == 2
        entries.extend(fields)
        for field in fields:
            field.ChangeValue(PASSWORD)
        if destroy:
            window.Destroy()
        return wx.ID_OK
    wx.on_password_window = on_password
    result = ui.VoicePackageDialog._password(c, confirm=True)
    if destroy:
        assert result is None
    else:
        assert result == PASSWORD and all(field.value == '' for field in entries)
    windows = [widget for widget in host.widgets if widget.title == 'Protected voice package password']
    assert len(windows) == 1 and windows[0].destroy_count == 1


def test_completed_inspection_from_replaced_library_is_not_adopted(view):
    host, c = view, view.controller
    assert c.inspect_protected()
    assert host.queued.wait(5)
    host.frame.library = SimpleNamespace(root=host.root)
    host.finish()
    assert c.review is None and not c.rights.GetValue()
    assert [call[0] for call in host.calls] == ['inspect']
