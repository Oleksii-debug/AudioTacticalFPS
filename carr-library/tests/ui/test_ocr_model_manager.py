"""Real event/Future routing with control doubles; not native/NVDA evidence."""
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.ui.ocr_model_manager import (
    INSTRUCTIONS, OcrModelManagerActions, create_ocr_model_manager, format_bytes,
)


class Control:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get('value', '')
        self.items = list(kwargs.get('choices', ()))
        self.selection = -1
        self.checked = set()
        self.enabled = True
        self.focused = self.destroyed = False
        self.bindings = {}
        self.name = ''
        self.label = kwargs.get('label', '')
        self.children = []
        if args and isinstance(args[0], Control):
            args[0].children.append(self)

    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SetName(self, value): self.name = value
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def SetItems(self, values):
        self.items, self.checked, self.selection = list(values), set(), -1
    def Check(self, index, checked=True):
        assert 0 <= index < len(self.items)
        self.checked.add(index) if checked else self.checked.discard(index)
    def IsChecked(self, index):
        assert 0 <= index < len(self.items)
        return index in self.checked
    def Enable(self, value=True): self.enabled = value
    def SetFocus(self): self.focused = True
    def Bind(self, event, callback): self.bindings[event] = callback
    def Add(self, *args, **kwargs): pass
    def Wrap(self, width): pass
    def SetMinSize(self, size): pass
    def SetMaxLength(self, size): pass
    def SetSizer(self, sizer): pass
    def SetEscapeId(self, identity): self.escape_id = identity
    def EndModal(self, result): self.modal_result = result
    def Destroy(self): self.destroyed = True


class Executor:
    def __init__(self):
        self.calls = []

    def submit(self, operation):
        future = Future()
        self.calls.append((operation, future))
        return future

    def run(self):
        operation, future = self.calls[-1]
        try:
            future.set_result(operation())
        except Exception as exc:
            future.set_exception(exc)


def model(identifier, label, size, profile='fast', kind='language'):
    return SimpleNamespace(identifier=identifier, label=label, size=size,
                           profile=profile, kind=kind, revision=('a' if profile == 'fast' else 'b') * 40,
                           blob_sha1='c' * 40, relative_path=identifier + '.traineddata')


@pytest.fixture
def data():
    rows = {
        'fast': (model('eng', 'English', 1024), model('ukr', 'Ukrainian', 2048),
                 model('script/Latin', 'Latin script', 4096, kind='script'),
                 model('osd', 'Orientation and script detection', 8192, kind='auxiliary')),
        'best': (model('eng', 'English', 16384, 'best'),
                 model('ukr', 'Ukrainian', 32768, 'best')),
    }
    def catalogue(profile, query=''):
        return tuple(row for row in rows[profile]
                     if query.casefold() in f'{row.identifier} {row.label}'.casefold())
    installed = {'fast': {'eng'}, 'best': set()}
    store = SimpleNamespace(available=Mock(side_effect=lambda profile: tuple(sorted(installed[profile]))))

    def install(profile, source, identifiers, *, cancelled, progress):
        if cancelled():
            raise InterruptedError('Cancelled before publication')
        previous = set(installed[profile])
        for identifier in identifiers:
            progress(identifier, 1, 2)
            if cancelled():
                raise InterruptedError('Cancelled before publication')
            progress(identifier, 2, 2)
        installed[profile].update(identifiers)
        return SimpleNamespace(installed=identifiers,
            already_present=tuple(identifier for identifier in identifiers if identifier in previous),
            bytes=sum(row.size for row in rows[profile]
                      if row.identifier in identifiers and row.identifier not in previous),
            profile=profile, revision=rows[profile][0].revision)

    store.install = Mock(side_effect=install)
    return SimpleNamespace(rows=rows, catalogue=catalogue, store=store, installed=installed)


@pytest.fixture
def manager(data):
    form = OcrModelManagerActions()
    for name in ('profile', 'search', 'entries', 'summary', 'details', 'source', 'progress',
                 'status', 'select_button', 'clear_button', 'refresh_button', 'choose_button',
                 'install_button', 'cancel_button', 'close_button'):
        setattr(form, name, Control())
    form.mutable = (form.profile, form.search, form.entries, form.select_button,
                    form.clear_button, form.refresh_button, form.choose_button, form.install_button)
    form.callbacks = []
    form.configure(data.store, data.catalogue, Executor(), form.callbacks.append)
    def end():
        form.closed = True
        form.cancelled.set()
    form._end = end
    return form


def drain(form):
    while form.callbacks:
        form.callbacks.pop(0)()


def select(form, *identifiers):
    for index, row in enumerate(form._rows):
        form.entries.Check(index, row.identifier in identifiers)
    form.selection_changed()


def test_initial_catalogue_installed_status_details_and_no_automatic_install(manager, data):
    assert len(manager.entries.items) == 4
    assert 'English — eng — language — installed' in manager.entries.items
    assert 'auxiliary' in manager.entries.items[-1]
    manager.entries.SetSelection(2)
    manager.highlight_changed()
    assert 'script/Latin.traineddata' in manager.details.value
    assert 'a' * 40 in manager.details.value
    assert '0 models selected' in manager.summary.value
    assert not manager.cancel_button.enabled
    data.store.install.assert_not_called()
    data.store.available.assert_called_once_with('fast')


def test_search_preserves_hidden_selection_and_does_not_reread_installed_files(manager, data):
    select(manager, 'ukr', 'script/Latin')
    manager.search.SetValue('English')
    manager.filter_changed()
    assert '2 models selected; 2 outside' in manager.summary.value
    assert '(6,144 bytes)' in manager.summary.value
    manager.select_shown()
    assert manager._selected['fast'] == {'eng', 'ukr', 'script/Latin'}
    manager.entries.Check(0, False)
    manager.selection_changed()
    manager.search.SetValue('')
    manager.filter_changed()
    assert manager.entries.checked == {1, 2}
    data.store.available.assert_called_once_with('fast')


def test_profiles_keep_separate_selections_and_clear_includes_hidden_rows(manager):
    select(manager, 'ukr')
    manager.profile.SetSelection(1)
    manager.profile_changed()
    select(manager, 'eng')
    assert '(16,384 bytes)' in manager.summary.value
    manager.profile.SetSelection(0)
    manager.profile_changed()
    assert manager.entries.checked == {1}
    manager.search.SetValue('English')
    manager.filter_changed()
    manager.clear_selection()
    assert manager._selected['fast'] == set()
    assert manager._selected['best'] == {'eng'}


@pytest.mark.parametrize('index', [-1, 2, 1000])
def test_invalid_native_profile_selection_cannot_change_profile_or_start_install(manager, index):
    manager.profile.SetSelection(index)
    manager.profile_changed()
    assert manager._profile == 'fast'
    assert manager.status.value == 'Choose Fast or Best.'
    assert manager.executor.calls == []


def test_install_captures_checked_ids_profile_and_folder_and_verifies_result(manager, data, tmp_path):
    select(manager, 'eng', 'ukr')
    manager.source.SetValue(str(tmp_path))
    manager.install_selected()
    assert manager.pending and manager.cancel_button.enabled
    assert all(not control.enabled for control in manager.mutable)
    assert manager.close_button.enabled
    data.store.install.assert_not_called()
    # A changed control cannot alter the already approved operation.
    manager.source.SetValue('/somewhere-else')
    manager.profile.SetSelection(1)
    manager.profile_changed()
    manager.executor.run()
    args, kwargs = data.store.install.call_args
    assert args == ('fast', tmp_path, ('eng', 'ukr'))
    assert not kwargs['cancelled']()
    assert manager.result is None  # callbacks must be marshalled to the UI thread
    drain(manager)
    assert not manager.pending and not manager.cancel_button.enabled
    assert all(control.enabled for control in manager.mutable)
    assert manager.result.installed == ('eng', 'ukr')
    assert '1 newly installed, 1 already present' in manager.status.value
    assert '(2,048 bytes)' in manager.status.value
    assert manager.progress.value == 1000
    assert 'Ukrainian — ukr — language — installed' in manager.entries.items


@pytest.mark.parametrize('folder', ['', 'https://models.example/data', 'bad\x00folder'])
def test_install_requires_explicit_local_folder(manager, folder):
    select(manager, 'ukr')
    manager.source.SetValue(folder)
    manager.install_selected()
    assert not manager.pending
    assert manager.executor.calls == []
    assert 'Choose an offline source folder' in manager.status.value


def test_install_requires_selection(manager, tmp_path):
    manager.source.SetValue(str(tmp_path))
    manager.install_selected()
    assert manager.executor.calls == []
    assert 'Select at least one model' in manager.status.value


def test_progress_coalesces_callbacks_without_stealing_focus(manager, tmp_path):
    select(manager, 'ukr')
    manager.source.SetValue(str(tmp_path))
    manager.install_selected()
    manager.status.focused = False
    for complete in range(1, 101):
        manager._queue_progress(manager._job, 'ukr', complete, 100)
    assert len(manager.callbacks) == 1
    drain(manager)
    assert manager.progress.value == 999
    assert '100 of 100 bytes processed' in manager.status.value
    assert not manager.status.focused


def test_cancel_is_live_and_does_not_claim_installation(manager, data, tmp_path):
    select(manager, 'ukr')
    manager.source.SetValue(str(tmp_path))
    manager.install_selected()
    manager.cancel_operation()
    manager._queue_progress(manager._job, 'ukr', 1, 2)
    drain(manager)
    assert 'Cancellation requested' in manager.status.value
    manager.executor.run()
    drain(manager)
    assert data.installed['fast'] == {'eng'}
    assert manager.result is None
    assert 'Installation cancelled' in manager.status.value
    assert manager.entries.checked == {1}
    assert not manager.pending


def test_close_waits_for_cancelled_worker_before_destroying_controls(manager, tmp_path):
    select(manager, 'ukr')
    manager.source.SetValue(str(tmp_path))
    manager.install_selected()
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock(), Skip=Mock())
    manager.on_close(event)
    assert manager.close_when_idle and not manager.closed
    event.Veto.assert_called_once()
    manager.executor.run()
    drain(manager)
    assert manager.closed and not manager.pending


def test_submit_failure_reenables_controls_and_preserves_selection(manager, tmp_path):
    select(manager, 'ukr')
    manager.source.SetValue(str(tmp_path))
    manager.executor.submit = Mock(side_effect=RuntimeError('worker unavailable'))
    manager.install_selected()
    assert not manager.pending and not manager.cancel_button.enabled
    assert all(control.enabled for control in manager.mutable)
    assert manager.entries.checked == {1}
    assert 'worker unavailable' in manager.status.value


def test_copy_failure_keeps_selection_and_previous_registered_inventory(manager, data, tmp_path):
    select(manager, 'ukr')
    manager.source.SetValue(str(tmp_path))
    data.store.install.side_effect = ValueError('Selected model hash differs from the catalogue')
    manager.install_selected()
    manager.executor.run()
    drain(manager)
    assert manager.result is None and not manager.pending
    assert 'hash differs' in manager.status.value
    assert manager.entries.checked == {1}
    assert 'English — eng — language — installed' in manager.entries.items
    assert 'Ukrainian — ukr — language — not installed' in manager.entries.items


@pytest.mark.parametrize('change', [
    {'profile': 'best'}, {'revision': 'x' * 40}, {'installed': ('eng',)},
    {'already_present': ('unknown',)}, {'bytes': True},
])
def test_inconsistent_worker_report_is_never_success(manager, data, tmp_path, change):
    select(manager, 'ukr')
    manager.source.SetValue(str(tmp_path))
    report = dict(profile='fast', revision='a' * 40, installed=('ukr',),
                  already_present=(), bytes=2048)
    report.update(change)
    data.store.install.side_effect = None
    data.store.install.return_value = SimpleNamespace(**report)
    manager.install_selected()
    manager.executor.run()
    drain(manager)
    assert manager.result is None
    assert 'does not match' in manager.status.value


def test_missing_registered_selected_model_is_not_reported_success(manager, data, tmp_path):
    select(manager, 'ukr')
    manager.source.SetValue(str(tmp_path))
    data.store.install.side_effect = None
    data.store.install.return_value = SimpleNamespace(profile='fast', revision='a' * 40,
        installed=('ukr',), already_present=(), bytes=2048)
    manager.install_selected()
    manager.executor.run()
    drain(manager)
    assert manager.result is None
    assert 'does not confirm' in manager.status.value


def test_destroyed_dialog_ignores_late_worker_completion_and_progress(manager, tmp_path):
    select(manager, 'ukr')
    manager.source.SetValue(str(tmp_path))
    manager.install_selected()
    manager.closed = True
    manager.cancelled.set()
    previous = manager.status.value
    manager.executor.run()
    manager._queue_progress(manager._job, 'ukr', 1, 2)
    drain(manager)
    assert manager.status.value == previous
    assert manager.result is None


def test_inventory_failure_marks_unknown_and_is_readable(manager, data):
    data.store.available.side_effect = ValueError('Invalid model manifest')
    manager.refresh()
    assert all('status unknown' in label for label in manager.entries.items)
    assert 'Invalid model manifest' in manager.status.value
    assert manager.status.focused


def fake_wx(monkeypatch):
    wx = SimpleNamespace()
    for name in ('Dialog', 'Choice', 'TextCtrl', 'CheckListBox', 'Button', 'Gauge',
                 'StaticText', 'BoxSizer'):
        setattr(wx, name, Control)
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'EXPAND',
             'ALL', 'LEFT', 'RIGHT', 'TOP', 'BOTTOM', 'ALIGN_RIGHT', 'TE_READONLY',
             'TE_MULTILINE', 'ID_CANCEL', 'ID_OK', 'EVT_CHOICE', 'EVT_TEXT', 'EVT_CHECKLISTBOX',
             'EVT_LISTBOX', 'EVT_BUTTON', 'EVT_CLOSE', 'DD_DEFAULT_STYLE', 'DD_DIR_MUST_EXIST')
    for index, name in enumerate(names):
        setattr(wx, name, 1 << index)
    wx.callbacks = []
    wx.CallAfter = wx.callbacks.append
    monkeypatch.setitem(sys.modules, 'wx', wx)
    return wx


def test_native_factory_names_keyboard_routes_and_label_creation_order(data, monkeypatch):
    wx = fake_wx(monkeypatch)
    form = create_ocr_model_manager(None, data.store, catalogue=data.catalogue, executor=Executor())
    assert form.search.focused
    assert form.escape_id == wx.ID_CANCEL
    assert form.entries.name == 'OCR models; Space selects or clears a model'
    assert form.summary.name == 'Selected models and disk-size estimate'
    assert form.status.name == 'OCR model installation status and errors'
    for control in (form.profile, form.search, form.entries):
        position = form.children.index(control)
        assert form.children[position - 1].label.startswith('&')
    assert wx.EVT_CHECKLISTBOX in form.entries.bindings
    assert wx.EVT_LISTBOX in form.entries.bindings
    assert wx.EVT_BUTTON in form.cancel_button.bindings
    assert wx.EVT_CLOSE in form.bindings
    assert 'script/Latin.traineddata' in INSTRUCTIONS
    assert 'not automatic language detection' in INSTRUCTIONS


def test_cancelled_folder_chooser_preserves_previous_source(manager, monkeypatch):
    wx = fake_wx(monkeypatch)
    chooser = SimpleNamespace(ShowModal=lambda: wx.ID_CANCEL, GetPath=Mock(), Destroy=Mock())
    wx.DirDialog = Mock(return_value=chooser)
    manager.source.SetValue('/previous')
    manager.choose_folder()
    assert manager.source.value == '/previous'
    chooser.GetPath.assert_not_called()
    chooser.Destroy.assert_called_once()


def test_real_catalogue_and_empty_store_match_native_manager_contract(tmp_path, monkeypatch):
    from carr_library.extract.model_catalog import models
    from carr_library.extract.ocr_model_store import OcrModelStore
    fake_wx(monkeypatch)
    form = create_ocr_model_manager(None, OcrModelStore(tmp_path / 'packs'), executor=Executor())
    assert len(form.entries.items) == len(models('fast'))
    assert all('not installed' in text for text in form.entries.items)
    assert any(row.identifier == 'script/Latin' for row in form._rows)
    assert 'official fast models' in form.status.value


def test_byte_estimates_remain_exact_for_large_model_sets():
    assert format_bytes(3 * 1024 ** 3) == '3.00 GiB (3,221,225,472 bytes)'
    with pytest.raises(ValueError):
        format_bytes(True)
