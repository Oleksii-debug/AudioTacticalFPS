"""Actual dialog handlers preserve explicit approval across native deletion."""
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.ui import portable_sync


class Peer:
    def __init__(self, *args, **kwargs):
        self.dead = False
        self.forbidden = []
        self.destroyed = 0
        self.after_modal = lambda: None
        self.accepted = self
        self.text = self

    def __bool__(self): return not self.dead
    def IsBeingDeleted(self): return self.dead
    def _touch(self, name):
        if self.dead:
            self.forbidden.append(name)
            raise RuntimeError('Deleted native peer')
    def ShowModal(self):
        self.after_modal()
        return 1
    def GetPath(self):
        self._touch('GetPath')
        return '/explicitly/chosen/local-file'
    def GetValue(self):
        self._touch('GetValue')
        return True
    def Destroy(self):
        self._touch('Destroy')
        self.destroyed += 1
        return True
    def EndModal(self, value): self._touch('EndModal')
    def ChangeValue(self, value): self._touch('ChangeValue')
    def Add(self, *args, **kwargs): self._touch('Add')
    def SetName(self, *args): self._touch('SetName')
    def SetLabel(self, *args): self._touch('SetLabel')
    def SetFocus(self): self._touch('SetFocus')
    def SetSizer(self, *args): self._touch('SetSizer')
    def SetAffirmativeId(self, *args): self._touch('SetAffirmativeId')
    def SetEscapeId(self, *args): self._touch('SetEscapeId')
    def Wrap(self, *args): self._touch('Wrap')
    def FindWindow(self, *args): return self
    def CreateStdDialogButtonSizer(self, *args): return self


@pytest.fixture
def native(monkeypatch):
    dialog = Peer()
    constants = {name: 1 for name in ('ID_OK', 'ID_YES', 'ID_CANCEL', 'YES_NO', 'NO_DEFAULT',
        'ICON_QUESTION', 'FD_OPEN', 'FD_FILE_MUST_EXIST', 'DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER',
        'VERTICAL', 'HORIZONTAL', 'TE_MULTILINE', 'TE_READONLY', 'TE_PASSWORD', 'EXPAND', 'ALL',
        'ALIGN_RIGHT', 'OK', 'CANCEL', 'LEFT', 'TOP')}
    wx = SimpleNamespace(**constants, Dialog=Peer, FileDialog=lambda *a, **k: dialog,
        DirDialog=lambda *a, **k: dialog, MessageDialog=lambda *a, **k: dialog,
        TextCtrl=lambda *a, **k: dialog, StaticText=lambda *a, **k: dialog,
        BoxSizer=lambda *a, **k: dialog, Window=SimpleNamespace(FindFocus=lambda: None))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    path = Path(portable_sync.__file__).with_name('trust_status.py')
    spec = importlib.util.spec_from_file_location('carr_library.ui._tested_trust_status', path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    frame = Peer()
    frame.run_job, frame.report = Mock(), Mock()
    frame.cancel_event = SimpleNamespace(is_set=lambda: False)
    return SimpleNamespace(module=module, frame=frame, dialog=dialog, wx=wx)


@pytest.mark.parametrize('action', ['on_import', 'on_update'])
@pytest.mark.parametrize('destroy', [False, True])
def test_actual_signed_file_chooser_never_reads_a_deleted_native_peer(native, action, destroy):
    owner = object.__new__(native.module.TrustStatusDialog)
    Peer.__init__(owner)
    owner.frame, owner.controller, owner._alive = native.frame, Mock(), True
    def after_modal():
        if destroy:
            owner._alive = False
            owner.dead = native.frame.dead = native.dialog.dead = True
    native.dialog.after_modal = after_modal
    getattr(owner, action)()
    assert native.dialog.forbidden == owner.forbidden == []
    assert native.frame.run_job.call_count == (0 if destroy else 1)
    assert native.dialog.destroyed == (0 if destroy else 1)


@pytest.mark.parametrize('action', ['permission', 'update'])
@pytest.mark.parametrize('destroy', [False, True])
def test_actual_review_only_queues_an_explicit_live_acceptance(native, monkeypatch, action, destroy):
    monkeypatch.setattr(native.module, 'GrantReviewDialog', lambda *a: native.dialog)
    monkeypatch.setattr(native.wx, 'Dialog', lambda *a, **k: native.dialog)
    monkeypatch.setattr(native.module, 'release_text', lambda release: 'Actual reviewed details')
    controller = Mock()
    def after_modal():
        if destroy:
            native.frame.dead = native.dialog.dead = True
    native.dialog.after_modal = after_modal
    if action == 'permission':
        native.module.show_permission_review(native.frame, controller, 'reviewed-signed-file')
    else:
        native.module.show_update_review(native.frame, controller, 'selected-repository', 'verified-release')
    assert native.dialog.forbidden == []
    assert native.frame.run_job.call_count == (0 if destroy else 1)
    if not destroy:
        native.frame.run_job.call_args.args[1]()
        if action == 'permission':
            assert controller.import_review.call_args.kwargs['accepted'] is True
        else:
            assert controller.download_review.call_args.args == ('selected-repository', 'verified-release')


def test_trust_destroy_matches_native_id_without_accessing_original_wrapper(native):
    owner = object.__new__(native.module.TrustStatusDialog)
    Peer.__init__(owner)
    owner._alive, owner._native_id = True, 123
    child = SimpleNamespace(GetId=lambda: 124, GetEventObject=lambda: object(), Skip=Mock())
    owner._destroyed(child)
    assert owner._alive
    event = SimpleNamespace(GetId=lambda: 123, GetEventObject=lambda: object(), Skip=Mock())
    owner.dead = True
    owner._destroyed(event)
    assert not owner._alive and not owner.forbidden
    assert owner.Destroy() is False
    event.Skip.assert_called_once()


def test_trust_repeated_destroy_never_repeats_the_native_request(native):
    owner = object.__new__(native.module.TrustStatusDialog)
    Peer.__init__(owner)
    owner._alive = True
    assert owner.Destroy() is True
    assert owner.Destroy() is False
    assert not owner._alive and owner.destroyed == 1


@pytest.mark.parametrize('action', ['folder', 'confirm', 'password'])
def test_snapshot_modal_parent_deletion_never_reads_or_destroys_dead_controls(native, monkeypatch, action):
    monkeypatch.setattr(native.wx, 'Dialog', lambda *a, **k: native.dialog)
    def after_modal():
        native.dialog.dead = native.frame.dead = True
    native.dialog.after_modal = after_modal
    dialogs = portable_sync.NativeSyncDialogs()
    if action == 'folder':
        assert dialogs.folder(native.frame, 'Explicit folder') is None
    elif action == 'confirm':
        assert dialogs.confirm(native.frame, 'Confirm', 'Exact operation') is False
    else:
        assert dialogs.password(native.frame) == (False, None)
    assert native.dialog.forbidden == []
