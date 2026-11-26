"""Independent wx-doubled metadata dialog lifecycle and host-binding tests.

These exercise real event handlers and Futures, not native Windows or NVDA.
"""
from concurrent.futures import Future
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest

from carr_library.catalog.read_views import EditionView


class Control:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get('value', '')
        self.enabled = True
        self.focused = False
        self.destroyed = False
        self.bindings = {}
        self.modal_result = None
    def Bind(self, event, callback): self.bindings[event] = callback
    def SetName(self, name): self.name = name
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SetFocus(self): self.focused = True
    def SetMinSize(self, value): pass
    def SetSizer(self, value): pass
    def Add(self, *args): pass
    def SetDefault(self): pass
    def Enable(self, enabled): self.enabled = enabled
    def EndModal(self, result): self.modal_result = result
    def Destroy(self): self.destroyed = True


class Executor:
    def __init__(self):
        self.calls = []
        self.future = Future()
    def submit(self, action, **kwargs):
        self.calls.append((action, kwargs))
        return self.future


@pytest.fixture
def module():
    wx = ModuleType('wx')
    for name in ('Dialog', 'BoxSizer', 'StaticText', 'TextCtrl', 'Button'):
        setattr(wx, name, Control)
    for n, name in enumerate(('VERTICAL', 'HORIZONTAL', 'EXPAND', 'ALL', 'LEFT', 'RIGHT',
            'TOP', 'ALIGN_RIGHT', 'TE_MULTILINE', 'TE_READONLY', 'ID_SAVE', 'ID_CANCEL',
            'ID_OK', 'EVT_BUTTON', 'EVT_CLOSE')):
        setattr(wx, name, 1 << n)
    wx.callbacks = []
    wx.CallAfter = lambda callback: wx.callbacks.append(callback)
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/catalog_metadata_dialog.py'
    spec = importlib.util.spec_from_file_location('_metadata_dialog_independent', path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'wx': wx}):
        spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def edition():
    return EditionView('e1', 'w1', 'a' * 64, 'Original title', 'uk', 'First', 'II', ('Олена', 'Žan'))


def test_metadata_values_keep_unicode_author_order_and_unknowns(module):
    assert module.metadata_values('  Книга — Étude  ', '  Олена  \n\n Žan \r\n C. D.  ',
        ' uk ', ' ', '') == dict(title='Книга — Étude', authors=('Олена', 'Žan', 'C. D.'),
                               language='uk', edition=None, volume=None)
    assert module.metadata_values('A', '', '', '', '')['authors'] == ()


@pytest.mark.parametrize('values', [
    ('', '', '', '', ''), ('A' * 513, '', '', '', ''), ('A\x00B', '', '', '', ''),
    ('A', 'Name\n' * 101, '', '', ''), ('A', 'B' * 513, '', '', ''),
    ('A', 'Name\x00', '', '', ''), ('A', '', 'x' * 4001, '', ''),
    ('A', '', '', 'x\x00', ''), ('A', '', '', '', 'x\x00'),
])
def test_invalid_form_is_rejected_before_submission(module, values):
    with pytest.raises(ValueError):
        module.metadata_values(*values)


def dialog(module, edition):
    executor = Executor()
    save = lambda **values: 'new-revision'
    return module.MetadataDialog(None, edition, save=save, executor=executor), executor


def test_pending_save_disables_inputs_and_blocks_duplicate_submit_and_close(module, edition):
    form, executor = dialog(module, edition)
    assert form.fields['title'].focused
    assert form.fields['authors'].value == 'Олена\nŽan'
    form.fields['title'].SetValue('Edited title')
    form.on_save()
    form.on_save()
    assert form.pending and len(executor.calls) == 1
    assert not any(c.enabled for c in (*form.fields.values(), form.save_button, form.cancel_button))
    assert executor.calls[0][1]['title'] == 'Edited title'
    event = SimpleNamespace(vetoed=False, skipped=False)
    event.CanVeto = lambda: True
    event.Veto = lambda: setattr(event, 'vetoed', True)
    event.Skip = lambda: setattr(event, 'skipped', True)
    form.on_close(event)
    assert event.vetoed and not event.skipped
    assert form.modal_result is None


def test_async_success_closes_only_after_ui_callback(module, edition):
    form, executor = dialog(module, edition)
    form.on_save()
    executor.future.set_result('revision')
    assert form.pending and form.modal_result is None
    assert len(module.wx.callbacks) == 1
    module.wx.callbacks.pop()()
    assert not form.pending and form.modal_result == module.wx.ID_OK


def test_async_failure_preserves_all_typed_edits_and_reenables_retry(module, edition):
    form, executor = dialog(module, edition)
    form.fields['title'].SetValue('My draft title')
    form.fields['authors'].SetValue('Žan\nОлена')
    before = {key: value.GetValue() for key, value in form.fields.items()}
    form.on_save()
    executor.future.set_exception(ValueError('Book metadata changed. Refresh the catalog.'))
    module.wx.callbacks.pop()()
    assert not form.pending and form.modal_result is None
    assert all(c.enabled for c in (*form.fields.values(), form.save_button, form.cancel_button))
    assert {key: value.GetValue() for key, value in form.fields.items()} == before
    assert 'Book metadata changed' in form.status.value
    assert 'edits remain' in form.status.value and form.status.focused


def test_invalid_and_executor_rejected_save_keep_the_form_open(module, edition):
    form, executor = dialog(module, edition)
    form.fields['title'].SetValue(' ')
    form.on_save()
    assert executor.calls == [] and not form.pending and form.status.focused
    form.fields['title'].SetValue('Preserved draft')
    def reject(*args, **kwargs): raise RuntimeError('Worker unavailable')
    executor.submit = reject
    form.on_save()
    assert not form.pending and form.modal_result is None
    assert form.fields['title'].GetValue() == 'Preserved draft'
    assert form.save_button.enabled and form.cancel_button.enabled
    assert 'Worker unavailable' in form.status.value


def host(edition):
    calls = []
    panel = SimpleNamespace(workspace='a', ready=True, books=Control(), status=Control())
    panel.selection = ('collection', 'destination', 'e1')
    panel._selection = lambda: panel.selection
    panel.refreshed = []
    def refresh(*, selection):
        panel.refreshed.append(selection)
        panel.selection = selection
        return True
    panel.refresh = refresh
    frame = SimpleNamespace(working=False, busy=False, executor=Executor(), reports=[])
    frame._workspace = lambda: 'a'
    frame.book = SimpleNamespace(GetPage=lambda index: panel)
    frame.report = frame.reports.append
    def read_metadata(workspace, identity):
        calls.append(('read', workspace, identity))
        return edition
    def update(workspace, identity, **values):
        calls.append(('update', workspace, identity, values))
        return 'revision'
    frame.library = SimpleNamespace(edition_metadata=read_metadata, update_metadata=update)
    return frame, panel, calls


def fake_modal(module, monkeypatch, *, result, invoke_save=False):
    forms = []
    class Modal(Control):
        def __init__(self, frame, current, *, save, executor):
            super().__init__()
            self.current, self.save = current, save
            forms.append(self)
        def ShowModal(self):
            if invoke_save:
                self.save(title='Corrected', authors=('A',), language=None, edition=None, volume=None)
            return result
    monkeypatch.setattr(module, 'MetadataDialog', Modal)
    return forms


def test_host_cancel_restores_books_focus_and_selection_without_saving(module, edition, monkeypatch):
    frame, panel, calls = host(edition)
    forms = fake_modal(module, monkeypatch, result=module.wx.ID_CANCEL)
    selection = panel.selection
    module.edit_selected_book(frame)
    assert panel.selection == selection and panel.books.focused
    assert panel.refreshed == [] and calls == [('read', 'a', 'e1')]
    assert forms[0].destroyed and frame.reports == []


def test_host_save_forwards_exact_expected_metadata_and_refreshes_same_selection(module, edition, monkeypatch):
    frame, panel, calls = host(edition)
    forms = fake_modal(module, monkeypatch, result=module.wx.ID_OK, invoke_save=True)
    selection = panel.selection
    module.edit_selected_book(frame)
    assert calls[1][0:3] == ('update', 'a', 'e1')
    assert calls[1][3]['expected_metadata'] is edition
    assert panel.refreshed == [selection] and panel.selection == selection
    assert panel.status.focused and 'metadata saved' in panel.status.value
    assert forms[0].destroyed and frame.reports == []


@pytest.mark.parametrize('problem', ['working', 'busy', 'scope', 'not_ready', 'no_selection', 'no_library'])
def test_host_guard_rejects_unavailable_or_wrong_scope_before_read(module, edition, monkeypatch, problem):
    frame, panel, calls = host(edition)
    forms = fake_modal(module, monkeypatch, result=module.wx.ID_CANCEL)
    if problem in ('working', 'busy'):
        setattr(frame, problem, True)
    elif problem == 'scope': panel.workspace = 'b'
    elif problem == 'not_ready': panel.ready = False
    elif problem == 'no_selection': panel.selection = (None, None, None)
    elif problem == 'no_library': frame.book = None
    module.edit_selected_book(frame)
    assert not calls and not forms and len(frame.reports) == 1
