"""Actual acquisition handlers after owner deletion; no native-window claim."""
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest


_helpers = runpy.run_path(str(Path(__file__).with_name('test_acquisition_dialog.py')))


@pytest.fixture
def fixture(tmp_path):
    module = _helpers['module'].__wrapped__(tmp_path)
    return module, _helpers['setup'].__wrapped__(module, tmp_path)


def _event(identity):
    skipped = []
    def forbidden_object():
        raise AssertionError('A destroy event must use its captured native ID, not wrapper identity.')
    return SimpleNamespace(GetId=lambda: identity, GetEventObject=forbidden_object,
                           Skip=lambda: skipped.append(True)), skipped


def _delete(module, form, mode):
    if mode == 'direct':
        form.Destroy()
    else:
        # A C++ parent deletion bypasses the Python Destroy override. Its event
        # can carry a different wrapper; querying the deleted owner ID is invalid.
        form.destroyed = True
        event, skipped = _event(form._native_id)
        form.bindings[module.wx.EVT_WINDOW_DESTROY](event)
        assert skipped == [True]
    assert form._closed and form.destroyed


def _poison_native(module, form, monkeypatch, forbidden):
    controls = [form]
    controls.extend(value for value in vars(form).values()
                    if isinstance(value, _helpers['Control']))
    controls.extend(form.providers.values())
    controls.extend(module.wx.dialogs)
    for control in controls:
        names = ('GetId', 'GetValue', 'GetPath', 'GetSelection', 'SetValue',
                 'SetSelection', 'Set', 'SetFocus', 'Enable', 'SetName',
                 'SetLabel', 'SetInsertionPoint', 'Layout', 'EndModal')
        if control is not form:
            names += ('Destroy',)
        for name in names:
            if hasattr(control, name):
                def rejected(*_args, owner=control, operation=name, **_kwargs):
                    forbidden.append((type(owner).__name__, operation))
                    raise RuntimeError('Deleted native widget accessed: ' + operation)
                monkeypatch.setattr(control, name, rejected)


@pytest.mark.parametrize('mode', ['direct', 'parent'])
@pytest.mark.parametrize('phase', ['worker_pending', 'completion_queued'])
def test_pending_and_queued_lookup_detach_without_native_access_or_global_cancel(
        fixture, monkeypatch, mode, phase):
    module, (form, frame, library) = fixture
    form.doi.SetValue('10.1234/reading')
    form.online.SetValue(True)
    assert form.lookup() and form.pending
    child, skipped = _event(form._native_id + 1)
    form.bindings[module.wx.EVT_WINDOW_DESTROY](child)
    assert skipped == [True] and not form._closed
    if phase == 'completion_queued':
        frame.work()
        assert module.wx.callbacks and len(library.calls) == 1
    _delete(module, form, mode)
    forbidden = []
    _poison_native(module, form, monkeypatch, forbidden)
    if phase == 'worker_pending':
        frame.work()
        assert library.calls == []
    while module.wx.callbacks:
        module.wx.callbacks.pop(0)()
    for name in ('lookup', 'save_reference', 'apply_metadata', 'download_source',
                 'on_identifier_kind_changed', 'on_request_changed',
                 'on_online_changed', 'on_destination_scope_changed',
                 'on_rights_changed', 'on_source_changed', 'cancel_operation',
                 'close', '_enabled', '_clear_preview'):
        getattr(form, name)()
    form._message('An obsolete result must not touch the deleted status control.')
    assert form.Destroy() is False
    assert forbidden == [] and form.preview is None and not frame.jobs
    assert not frame.cancel_event.is_set(), 'Owner deletion must not stop an unrelated host operation.'


@pytest.mark.parametrize('raises_after_delete', [False, True])
def test_completion_rechecks_owner_after_callback(fixture, monkeypatch, raises_after_delete):
    module, (form, frame, _library) = fixture
    forbidden, completed = [], []
    def complete(value):
        assert value == 'actual queued result'
        completed.append(value)
        _delete(module, form, 'parent')
        _poison_native(module, form, monkeypatch, forbidden)
        if raises_after_delete:
            raise ValueError('The callback returned from a destroyed nested owner.')
    assert form._start('Synthetic completion', lambda _cancelled: 'actual queued result', complete)
    frame.finish()
    assert completed == ['actual queued result'] and forbidden == []
    assert not frame.cancel_event.is_set()


@pytest.mark.parametrize('kind', ['confirm', 'folder', 'name'])
@pytest.mark.parametrize('boundary', ['modal_return', 'chooser_cleanup'])
def test_successful_nested_chooser_return_after_owner_deletion_is_ignored(
        fixture, monkeypatch, kind, boundary):
    module, setup = fixture
    form, frame, library = _helpers['looked_up'](setup)
    form.rights.SetValue(True)
    forbidden, observed = [], []
    popup = {'confirm': module.wx.MessageDialog, 'folder': module.wx.DirDialog,
             'name': module.wx.TextEntryDialog}[kind]
    actual_show, actual_destroy = popup.ShowModal, popup.Destroy
    def detach(peer):
        observed.append(kind)
        peer.destroyed = True
        _delete(module, form, 'parent')
        _poison_native(module, form, monkeypatch, forbidden)
    def show(peer):
        result = actual_show(peer)
        if boundary == 'modal_return':
            detach(peer)
        return result
    def destroy(peer):
        result = actual_destroy(peer)
        if boundary == 'chooser_cleanup':
            detach(peer)
        return result
    monkeypatch.setattr(popup, 'ShowModal', show)
    monkeypatch.setattr(popup, 'Destroy', destroy)
    result = form.apply_metadata() if kind == 'confirm' else form.download_source()
    assert result is False and observed == [kind]
    assert forbidden == [] and not frame.jobs and library.calls == []
    assert not frame.cancel_event.is_set()
