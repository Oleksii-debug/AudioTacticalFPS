"""Real backup work on a retained host; native peers reject every dead access.

Widget peers here cover event ownership only. Actual Windows keyboard acceptance
is separate; every snapshot, delta, restore and held completion below is genuine.
"""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread, get_ident
from types import SimpleNamespace
import json
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_budget import BackupBudget, MIB
from carr_library.backup_incremental import BackupReference, create_incremental_backup
from carr_library.backup_options import FIELDS
from carr_library.library_backup import create_backup, verify_backup
from carr_library.ui import backup_actions as actions
from carr_library.ui.backup_incremental_dialog import IncrementalBackupActions, create_base_review
from carr_library.ui.backup_resources import BackupOptions, create_backup_options


class Frame:
    def __init__(self, app):
        self.library, self.working, self._closing = app, False, False
        self.cancel_event, self.queued, self.messages = Event(), [], []
        self.children, self.opened = [], []
        self.deleted, self.native_id = False, 170
        self.gui_thread = get_ident()
        self.admission_error = None

    def _native(self):
        assert get_ident() == self.gui_thread, 'The worker touched a native peer.'

    def __bool__(self):
        self._native()
        return not self.deleted

    def GetId(self):
        self._native()
        assert not self.deleted, 'Dead host identifier access.'
        return self.native_id

    def IsBeingDeleted(self):
        self._native()
        assert not self.deleted, 'Dead host deletion query.'
        return self._closing

    def run_job(self, label, work, done):
        self._native()
        if self.admission_error is not None:
            raise self.admission_error
        self.queued.append((label, work, done))

    def report(self, message):
        self._native()
        assert not self.deleted
        self.messages.append(str(message))

    def _open_data_root(self, path):
        self._native()
        assert not self.deleted
        self.opened.append(Path(path))

    def destroy(self):
        self._native()
        self._closing = True
        for child in tuple(self.children):
            if child:
                child.Destroy()
        self.deleted = True


class EventPeer:
    def __init__(self, identifier):
        self.identifier, self.skipped = identifier, False
    def GetId(self):
        return self.identifier
    def Skip(self):
        self.skipped = True


class Widget:
    toolkit = None
    sequence = 200
    def __init__(self, parent=None, *args, value='', label='', **kwargs):
        Widget.sequence += 1
        self.identifier, self.parent = Widget.sequence, parent
        self.deleted, self.children, self.bindings = False, [], {}
        self.value, self.label, self.name = value, label, ''
        self.enabled, self.visible, self.modal = True, True, True
        self.selection, self.values = -1, []
        self.reads, self.destroy_count, self.modal_result = 0, 0, None
        if parent is not None and hasattr(parent, 'children'):
            parent.children.append(self)
        self.toolkit.instances.append(self)

    def _live(self):
        assert not self.deleted, 'A destroyed native peer was accessed.'

    def __bool__(self):
        return not self.deleted

    def GetId(self):
        self._live()
        return self.identifier

    def Bind(self, event, callback):
        self._live()
        self.bindings[event] = callback

    def IsBeingDeleted(self):
        self._live()
        return False

    def SetName(self, value):
        self._live()
        self.name = value

    def GetName(self):
        self._live()
        return self.name

    def SetValue(self, value):
        self._live()
        self.value = value

    def GetValue(self):
        self._live()
        self.reads += 1
        return self.value

    def SetLabel(self, value):
        self._live()
        self.label = value

    def GetLabel(self):
        self._live()
        return self.label

    def SetFocus(self):
        self._live()
        self.toolkit.focus = self

    def Enable(self, value):
        self._live()
        self.enabled = value

    def Show(self, value=True):
        self._live()
        self.visible = value

    def Wrap(self, value):
        self._live()

    def SetSizer(self, value):
        self._live()
        self.sizer = value

    def SetScrollRate(self, *args):
        self._live()

    def SetMinSize(self, *args):
        self._live()

    def SetEscapeId(self, *args):
        self._live()

    def GetClientSize(self):
        self._live()
        return SimpleNamespace(width=760)

    def Layout(self):
        self._live()

    def SetInsertionPoint(self, position):
        self._live()

    def Append(self, value):
        self._live()
        self.values.append(value)

    def Delete(self, index):
        self._live()
        self.values.pop(index)

    def SetSelection(self, value):
        self._live()
        self.selection = value

    def GetSelection(self):
        self._live()
        return self.selection

    def IsModal(self):
        self._live()
        return self.modal

    def EndModal(self, result):
        self._live()
        self.modal_result, self.modal = result, False

    def ShowModal(self):
        self._live()
        if self.toolkit.on_show is not None:
            return self.toolkit.on_show(self)
        return self.toolkit.ID_CANCEL

    def Destroy(self):
        self._live()
        self.destroy_count += 1
        for child in tuple(self.children):
            if child:
                child.Destroy()
        callback = self.bindings.get(self.toolkit.EVT_WINDOW_DESTROY)
        if callback is not None:
            callback(EventPeer(self.identifier))
        self.deleted = True
        return True


class Sizer:
    def __init__(self, *args, **kwargs):
        self.children = []
    def Add(self, child, *args, **kwargs):
        self.children.append(child)
    def AddGrowableCol(self, *args):
        pass


def wx_peer():
    wx = SimpleNamespace(instances=[], focus=None, on_show=None, message_calls=[])
    for index, name in enumerate(('VERTICAL', 'HORIZONTAL', 'EXPAND', 'ALL',
            'ALIGN_RIGHT', 'ALIGN_CENTER_VERTICAL', 'DEFAULT_DIALOG_STYLE',
            'RESIZE_BORDER', 'TE_READONLY', 'TE_MULTILINE', 'YES_NO', 'ICON_QUESTION')):
        setattr(wx, name, 1 << index)
    wx.ID_OK, wx.ID_CANCEL, wx.YES, wx.NO = 1, 2, 3, 4
    wx.EVT_WINDOW_DESTROY, wx.EVT_CLOSE, wx.EVT_BUTTON, wx.EVT_CHECKBOX = object(), object(), object(), object()
    class Peer(Widget):
        toolkit = wx
    wx.Dialog = wx.TextEntryDialog = wx.ScrolledWindow = wx.StaticText = Peer
    wx.CheckBox = wx.TextCtrl = wx.Button = wx.ListBox = Peer
    wx.BoxSizer = wx.FlexGridSizer = Sizer
    def message(*args):
        wx.message_calls.append(args)
        return wx.NO
    wx.MessageBox = message
    return wx


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Owned backup actions')
    assert app.add_text(workspace, 'Original', 'Exact retained quotation Ω.',
                        edition_id='original')['state'] == 'complete'
    try:
        yield app, workspace
    finally:
        assert app.close_search() is not False


def selected():
    return BackupBudget(memory_bytes=768*MIB, staging_bytes=256*MIB, seconds=300)


def execute(frame, *, deliver=True):
    assert len(frame.queued) == 1
    _, work, done = frame.queued.pop()
    result, errors = [], []
    def run():
        try:
            result.append(work())
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=run)
    thread.start()
    thread.join(30)
    assert not thread.is_alive(), 'The real backup worker did not close.'
    if errors:
        raise errors[0]
    assert len(result) == 1
    if deliver:
        done(result[0])
    return result[0], done


def full_choices(monkeypatch, wx, options, folders, checksum=None, reports=None):
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr(actions, 'review_backup_options', lambda *a, **k: options)
    choices = iter(folders)
    monkeypatch.setattr(actions, 'choose_directory', lambda *a: next(choices))
    if checksum is not None:
        monkeypatch.setattr(actions, 'choose_checksum', lambda *a: checksum)
    if reports is not None:
        monkeypatch.setattr(actions, 'show_report',
            lambda owner, title, body, toolkit: reports.append((title, body)))


@pytest.mark.parametrize('reviewed', [False, True])
def test_real_full_backup_runs_only_on_existing_host_and_retains_omitted_policy(
        library, tmp_path, monkeypatch, reviewed):
    app, _ = library
    frame, wx, reports = Frame(app), wx_peer(), []
    options = BackupOptions(True, selected() if reviewed else None)
    full_choices(monkeypatch, wx, options, [tmp_path], reports=reports)
    real = app.backup
    calls = []
    def observed(destination, **kwargs):
        calls.append(dict(kwargs))
        return real(destination, **kwargs)
    monkeypatch.setattr(app, 'backup', observed)
    actions.create_backup(frame)
    assert len(frame.queued) == 1 and not list(tmp_path.glob('ResearchLibrary-backup-*'))
    assert frame._backup_review is None
    checksum, _ = execute(frame)
    folder, = tmp_path.glob('ResearchLibrary-backup-*')
    assert verify_backup(folder, checksum)['version'] == 4
    assert calls[0]['include_generated'] is True
    assert ('backup_budget' in calls[0]) is reviewed
    if reviewed:
        assert calls[0]['backup_budget'] is options.budget
    assert reports[0][0] == 'Backup complete' and checksum in reports[0][1]
    assert not frame.messages


def test_real_full_restore_requires_independent_checksum_and_does_not_auto_open(
        library, tmp_path, monkeypatch):
    app, workspace = library
    base = tmp_path / 'full'
    checksum = create_backup(app, base)
    frame, wx, reports = Frame(app), wx_peer(), []
    full_choices(monkeypatch, wx, BackupOptions(False, selected()), [base, tmp_path],
                 checksum=checksum, reports=reports)
    actions.restore_backup(frame)
    assert not list(tmp_path.glob('ResearchLibrary-restored-*'))
    restored, _ = execute(frame)
    assert not frame.opened and len(wx.message_calls) == 1
    assert LibraryApp(restored).edition_metadata(workspace, 'original').title == 'Original'
    assert reports[0][0] == 'Recovery details'
    assert reports[-1][0] == 'Restore complete'


@pytest.mark.parametrize('change', ['instance', 'root', 'closing', 'cancelled'])
def test_queued_full_work_refuses_changed_owner_before_publication(
        library, tmp_path, monkeypatch, change):
    app, _ = library
    frame = Frame(app)
    full_choices(monkeypatch, wx_peer(), BackupOptions(False, selected()), [tmp_path], reports=[])
    actions.create_backup(frame)
    old_root = app.root
    if change == 'instance':
        frame.library = LibraryApp(tmp_path / 'other')
    elif change == 'root':
        app.root = tmp_path / 'changed-root'
    elif change == 'closing':
        frame._closing = True
    else:
        frame.cancel_event.set()
    try:
        with pytest.raises((ValueError, InterruptedError)):
            execute(frame)
        assert not list(tmp_path.glob('ResearchLibrary-backup-*'))
    finally:
        app.root = old_root


@pytest.mark.parametrize('failure', [OSError('admission'), RuntimeError('admission'), KeyboardInterrupt()])
def test_declined_or_raised_admission_releases_review_token_for_explicit_retry(
        library, tmp_path, monkeypatch, failure):
    app, _ = library
    frame = Frame(app)
    full_choices(monkeypatch, wx_peer(), BackupOptions(False, selected()),
                 [tmp_path, tmp_path], reports=[])
    frame.admission_error = failure
    actions.create_backup(frame)
    assert not frame.queued and frame._backup_review is None
    assert not list(tmp_path.glob('ResearchLibrary-backup-*'))
    frame.admission_error = None
    actions.create_backup(frame)
    checksum, _ = execute(frame)
    folder, = tmp_path.glob('ResearchLibrary-backup-*')
    verify_backup(folder, checksum)


def test_actual_committed_full_backup_remains_successful_after_owner_dies_before_return(
        library, tmp_path, monkeypatch):
    app, _ = library
    frame, wx, reports = Frame(app), wx_peer(), []
    full_choices(monkeypatch, wx, BackupOptions(False, selected()), [tmp_path], reports=reports)
    committed, release = Event(), Event()
    real = app.backup
    def held(*args, **kwargs):
        result = real(*args, **kwargs)
        committed.set()
        assert release.wait(15)
        return result
    monkeypatch.setattr(app, 'backup', held)
    actions.create_backup(frame)
    _, work, done = frame.queued.pop()
    results, errors = [], []
    def run():
        try:
            results.append(work())
        except BaseException as error:
            errors.append(error)
    thread = Thread(target=run)
    thread.start()
    try:
        assert committed.wait(15)
        folder, = tmp_path.glob('ResearchLibrary-backup-*')
        raw = (folder / 'manifest.json').read_bytes()
        frame.destroy()
    finally:
        release.set()
        thread.join(20)
    assert not thread.is_alive() and not errors and len(results) == 1
    done(results[0])
    assert not reports
    assert sha256(raw).hexdigest() == results[0]
    assert (folder / 'manifest.json').read_bytes() == raw
    verify_backup(folder, results[0])


class Dialogs:
    def __init__(self, folders, checksum, *, options=None, bases=()):
        self.folders = iter(folders)
        self.saved_checksum, self.results = checksum, []
        self.selected = options or BackupOptions(False, selected())
        self.bases = bases
    def folder(self, _title):
        return next(self.folders)
    def checksum(self, owner):
        owner.guard()
        return self.saved_checksum
    def options(self, owner, action):
        owner.guard()
        return self.selected
    def chain(self, first, choose_reference, guard):
        guard()
        return self.bases, 64*1024**3
    def show(self, title, message, owner):
        assert owner.current()
        self.results.append((title, message))


def test_actual_incremental_create_uses_same_host_and_returns_exact_receipt(library, tmp_path):
    app, _ = library
    base = tmp_path / 'full'
    checksum = create_backup(app, base)
    frame, dialogs = Frame(app), Dialogs([base, tmp_path], checksum)
    controller = IncrementalBackupActions(frame, dialogs)
    controller.create()
    assert len(frame.queued) == 1 and not list(tmp_path.glob('ResearchLibrary-incremental-*'))
    receipt, _ = execute(frame)
    assert receipt['base_sha256'] == checksum and Path(receipt['path']).is_dir()
    assert dialogs.results[0][0] == 'Incremental backup complete'
    assert receipt['manifest_sha256'] in dialogs.results[0][1]
    assert 'not a standalone backup' in dialogs.results[0][1]


@pytest.mark.parametrize('action', ['verify', 'restore'])
def test_actual_incremental_selected_chain_is_explicit_and_passive(library, tmp_path, action):
    app, workspace = library
    base_path = tmp_path / 'full'
    base = BackupReference(base_path, create_backup(app, base_path))
    layer = create_incremental_backup(app, tmp_path / 'delta', base=base, backup_budget=selected())
    frame = Frame(app)
    dialogs = Dialogs([layer.path, *([tmp_path] if action == 'restore' else [])],
                      layer.manifest_sha256, bases=(base,))
    getattr(IncrementalBackupActions(frame, dialogs), action)()
    result, _ = execute(frame)
    assert not frame.messages and not frame.opened
    if action == 'verify':
        assert result['chain'] == [layer.manifest_sha256, base.sha256]
    else:
        assert LibraryApp(result['restored_root']).edition_metadata(workspace, 'original').title == 'Original'
        assert 'require explicit review' in dialogs.results[0][1]


def test_wrong_incremental_checksum_is_verified_on_worker_without_gui_manifest_reads(library, tmp_path):
    app, _ = library
    base = tmp_path / 'full'
    create_backup(app, base)
    frame, dialogs = Frame(app), Dialogs([base, tmp_path], '0'*64)
    IncrementalBackupActions(frame, dialogs).create()
    assert len(frame.queued) == 1
    with pytest.raises(ValueError, match='digest mismatch'):
        execute(frame)
    assert not list(tmp_path.glob('ResearchLibrary-incremental-*'))
    assert not dialogs.results


def test_nested_review_or_cancelled_picker_never_admits_a_second_operation(library, tmp_path):
    app, _ = library
    frame = Frame(app)
    dialogs = Dialogs([None], '0'*64)
    IncrementalBackupActions(frame, dialogs).create()
    assert not frame.queued and frame._backup_review is None
    frame._backup_review = object()
    IncrementalBackupActions(frame, Dialogs([], '0'*64)).create()
    assert not frame.queued


@pytest.mark.parametrize('death', ['direct', 'parent', 'new-library'])
def test_checksum_modal_does_not_read_or_destroy_a_dead_peer(library, tmp_path, death):
    app, _ = library
    frame, wx = Frame(app), wx_peer()
    owner = actions.BackupOwner(frame)
    seen = []
    def modal(dialog):
        seen.append(dialog)
        dialog.SetValue('a'*64)
        if death == 'direct':
            dialog.Destroy()
        elif death == 'parent':
            frame.destroy()
        else:
            frame.library = LibraryApp(tmp_path / 'other-library')
        return wx.ID_OK
    wx.on_show = modal
    assert actions.choose_checksum(owner, 'Checksum', 'Independent checksum', wx) is None
    assert seen[0].reads == 0 and seen[0].destroy_count == 1


def test_checksum_modal_returns_only_an_explicit_live_accepted_value(library):
    app, _ = library
    frame, wx = Frame(app), wx_peer()
    def modal(dialog):
        dialog.SetValue('f'*64)
        return wx.ID_OK
    wx.on_show = modal
    assert actions.choose_checksum(actions.BackupOwner(frame), 'Checksum', 'Independent checksum', wx) == 'f'*64
    dialog, = wx.instances
    assert dialog.reads == 1 and dialog.destroy_count == 1


@pytest.mark.parametrize('death', ['direct', 'parent'])
def test_actual_committed_result_report_can_lose_its_modal_peer_without_changing_success(
        library, tmp_path, monkeypatch, death):
    app, _ = library
    frame, wx = Frame(app), wx_peer()
    full_choices(monkeypatch, wx, BackupOptions(False, selected()), [tmp_path])
    seen = []
    def modal(dialog):
        seen.append(dialog)
        if death == 'direct':
            dialog.Destroy()
        else:
            frame.destroy()
        return wx.ID_CANCEL
    wx.on_show = modal
    actions.create_backup(frame)
    result, _ = execute(frame)
    folder, = tmp_path.glob('ResearchLibrary-backup-*')
    assert seen[0].destroy_count == 1
    assert not frame.messages
    assert verify_backup(folder, result)['version'] == 4


def test_real_options_constructor_captures_typed_review_and_default_audio_scope(library):
    app, _ = library
    frame, wx = Frame(app), wx_peer()
    owner = actions.BackupOwner(frame)
    dialog = create_backup_options(frame, guard=owner.guard, wx_module=wx)
    assert dialog.include_audio.GetValue() is True
    assert dialog.reviewed.GetValue() is False
    assert all(not peer.enabled for peer in dialog.fields.values())
    dialog.reviewed.SetValue(True)
    dialog.toggle()
    assert all(peer.enabled for peer in dialog.fields.values())
    dialog.fields['original_bytes'].SetValue('2048')
    dialog.fields['assets'].SetValue('50000')
    dialog.fields['segments'].SetValue('10000')
    dialog.fields['memory_bytes'].SetValue('1024')
    dialog.fields['staging_bytes'].SetValue('256')
    dialog.confirm()
    assert dialog.modal_result == wx.ID_OK
    assert dialog.result.budget.original_bytes == 2*1024**3
    assert dialog.result.budget.assets == 50_000 and dialog.result.budget.segments == 10_000
    assert dialog.result.include_generated is True
    assert {peer.name for peer in dialog.fields.values()} == {'Backup resource: '+row[2] for row in FIELDS}
    dialog.Destroy()


@pytest.mark.parametrize('value', ['-1', 'nan', 'inf'])
def test_invalid_native_resource_review_remains_open(library, value):
    app, _ = library
    frame, wx = Frame(app), wx_peer()
    dialog = create_backup_options(frame, guard=actions.BackupOwner(frame).guard, wx_module=wx)
    dialog.reviewed.SetValue(True)
    dialog.fields['seconds'].SetValue(value)
    dialog.confirm()
    assert dialog.modal_result is None and dialog.result is None
    assert dialog.notice.GetLabel()
    dialog.Destroy()


def test_rewrapped_native_destruction_withdraws_resource_and_chain_actions(library, tmp_path):
    app, _ = library
    frame, wx = Frame(app), wx_peer()
    owner = actions.BackupOwner(frame)
    dialog = create_backup_options(frame, guard=owner.guard, wx_module=wx)
    identifier = dialog.GetId()
    dialog.destroyed(EventPeer(identifier))
    dialog.confirm()
    assert dialog.result is None and dialog.modal_result is None
    dialog.Destroy()
    base = tmp_path / 'base'
    base.mkdir()
    first = BackupReference(base, '1'*64)
    chain = create_base_review(frame, first, choose_reference=lambda *args: None,
                               guard=owner.guard, wx_module=wx)
    chain.destroyed(EventPeer(chain.GetId()))
    chain.confirm()
    assert chain.result is None and chain.modal_result is None
    chain.Destroy()


def test_chain_native_review_rejects_duplicate_and_ninth_ancestor_before_worker(library, tmp_path):
    app, _ = library
    frame, wx = Frame(app), wx_peer()
    owner = actions.BackupOwner(frame)
    paths = [tmp_path / ('generation-'+str(index)) for index in range(10)]
    for path in paths:
        path.mkdir()
    first = BackupReference(paths[0], '0'*64)
    refs = [BackupReference(paths[index], ('%064x' % index)) for index in range(1, 10)]
    choices = iter([refs[0], refs[0], *refs[1:]])
    dialog = create_base_review(frame, first, choose_reference=lambda *args: next(choices),
                                guard=owner.guard, wx_module=wx)
    dialog.add_reference()
    dialog.add_reference()
    assert dialog.references == [refs[0]] and 'once' in dialog.notice.GetLabel()
    for _ in range(7):
        dialog.add_reference()
    assert len(dialog.references) == 8
    dialog.add_reference()
    assert len(dialog.references) == 8 and 'eight' in dialog.notice.GetLabel()
    dialog.confirm()
    assert dialog.result == (tuple(refs[:8]), 64*1024**3)
    dialog.Destroy()
