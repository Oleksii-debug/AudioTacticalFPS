"""Native-controller lifetime and the real DOCX workflow with controlled widgets.

The format worker, saved translations, source/library and output publisher are
real. Widgets/pickers and the already reviewed synthetic translator are doubles;
actual wx/keyboard acceptance is the separate required Windows probe.
"""
import importlib.util
from pathlib import Path
from threading import Event
from types import SimpleNamespace
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.ui import translation_layout as view
from carr_library.ui.translation_documents import DocumentTranslationController

_spec = importlib.util.spec_from_file_location('layout_controller_cases',
    Path(__file__).with_name('test_translation_documents.py'))
_controller = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_controller)
control = _controller.control

_spec = importlib.util.spec_from_file_location('layout_actual_document_cases',
    Path(__file__).parents[1] / 'assistant/test_translation_layout.py')
_document = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_document)
bound, ready = _document.bound, _document.ready


@pytest.mark.parametrize('host_stop', [False, True])
def test_committed_export_receipt_survives_late_stop_for_the_same_open_view(control, host_stop):
    c = control
    receipt = {'status': 'complete', 'path': str(c.tmp_path / 'published.docx')}
    c.controller.start('Publish new DOCX', lambda: receipt, c.accepted.append, c.failed.append,
                       accept_committed=True)
    operation, done, failed = c.queue.pop()
    assert operation() is receipt
    (c.frame.cancel_event if host_stop else c.controller.cancel).set()
    done(receipt)
    assert c.accepted == [receipt] and not c.failed and not c.controller.busy


@pytest.mark.parametrize('mode', ['closed', 'closing', 'object', 'root', 'replaced-root', 'workspace', 'topic'])
def test_committed_receipt_never_updates_a_closed_or_different_library_view(control, mode):
    c = control
    receipt = {'status': 'complete', 'path': str(c.tmp_path / 'published.docx')}
    c.controller.start('Publish new DOCX', lambda: receipt, c.accepted.append, c.failed.append,
                       accept_committed=True)
    operation, done, failed = c.queue.pop()
    assert operation() is receipt
    _controller.mutation(c, mode)
    done(receipt)
    assert not c.accepted and not c.failed and not c.controller.busy


@pytest.mark.parametrize('value', [
    {'status': 'paused', 'path': 'pending.docx'},
    {'status': 'complete'}, {'status': 'complete', 'path': ''}, ['saved-job']])
def test_late_stop_does_not_promote_an_uncommitted_or_malformed_result(control, value):
    c = control
    c.controller.start('Unfinished formatting', lambda: value, c.accepted.append, c.failed.append,
                       accept_committed=True)
    operation, done, failed = c.queue.pop()
    operation()
    c.controller.cancel.set()
    done(value)
    assert not c.accepted and not c.failed


@pytest.mark.parametrize('flag', [1, 'yes', None])
def test_committed_delivery_mode_requires_explicit_boolean_before_scheduling(control, flag):
    c = control
    with pytest.raises(ValueError, match='boolean'):
        c.controller.start('Invalid', lambda: None, c.accepted.append, c.failed.append,
                           accept_committed=flag)
    assert not c.queue and not c.controller.busy


class Widget:
    def __init__(self, owner=None, *, label='', value='', choices=(), **kwargs):
        self.owner, self.label, self.value = owner, label, value
        self.choices, self.selection, self.handlers = list(choices), 0, {}
        self.enabled, self.destroyed = True, False
        self.updates = []
        self.name = ''
        harness.widgets.append(self)

    def SetName(self, value):
        self.name = value
        harness.named[value] = self

    def Bind(self, event, callback):
        self.handlers[event] = callback

    def SetValue(self, value):
        self.value = value
        self.updates.append(('value', value))

    ChangeValue = SetValue

    def GetValue(self):
        return self.value

    def GetSelection(self):
        return self.selection

    def SetSelection(self, value):
        self.selection = value

    def GetSelections(self):
        return tuple(range(len(self.choices)))

    def Enable(self, value=True):
        self.enabled = bool(value)
        self.updates.append(('enabled', self.enabled))

    def SetFocus(self):
        harness.focus = self
        self.updates.append(('focus', True))

    def SetSizer(self, unused):
        pass

    def EndModal(self, unused):
        pass

    def Destroy(self):
        if self.destroyed:
            return
        self.destroyed = True
        callback = self.handlers.get('destroy')
        if callback:
            callback(SimpleNamespace(GetEventObject=lambda: self, Skip=lambda: None))

    def Clear(self):
        self.choices.clear()

    def Append(self, value):
        self.choices.append(value)


class Dialog(Widget):
    def __init__(self, owner=None, *, title='', **kwargs):
        super().__init__(owner, **kwargs)
        self.title = title

    def ShowModal(self):
        harness.dialog = self
        harness.plan()
        return 1


class Picker(Widget):
    def __init__(self, owner, message='', title='', choices=(), **kwargs):
        super().__init__(owner, choices=choices, **kwargs)
        self.title = title

    def ShowModal(self):
        if harness.picker_action:
            harness.picker_action()
        return 1

    def GetPath(self):
        return str(harness.output)


class Sizer:
    def __init__(self, *args):
        pass

    def Add(self, *args):
        pass


harness = None


@pytest.fixture
def screen(bound, monkeypatch, tmp_path):
    global harness
    harness = SimpleNamespace(widgets=[], named={}, queue=[], focus=None, dialog=None,
        plan=lambda: None, picker_action=None, answer=0, messages=[],
        output=tmp_path / 'Translated Ω.docx')
    wx = SimpleNamespace(Dialog=Dialog, TextCtrl=Widget, StaticText=Widget, Choice=Widget,
        Button=Widget, ListBox=Widget, BoxSizer=Sizer, WrapSizer=Sizer,
        SingleChoiceDialog=Picker, MultiChoiceDialog=Picker, FileDialog=Picker,
        EVT_BUTTON='button', EVT_CLOSE='close', EVT_WINDOW_DESTROY='destroy',
        DEFAULT_DIALOG_STYLE=0, RESIZE_BORDER=0, VERTICAL=0, HORIZONTAL=0,
        ALL=0, EXPAND=0, TE_MULTILINE=0, TE_READONLY=0, FD_SAVE=0,
        YES_NO=2, NO_DEFAULT=4, YES=1, ID_OK=1, ID_CLOSE=2)
    def message(value, title, flags, parent):
        harness.messages.append((value, title, flags))
        return harness.answer
    wx.MessageBox = message
    monkeypatch.setitem(sys.modules, 'wx', wx)
    row = SimpleNamespace(title='Retained DOCX Ω', edition_id=bound.edition)
    catalog = SimpleNamespace(ready=True, workspace=bound.workspace, rows={bound.edition: row})
    frame = SimpleNamespace(library=bound.app, active=(bound.workspace, None),
        working=False, _closing=False, cancel_event=Event(),
        book=SimpleNamespace(GetPage=lambda index: catalog))
    def run_job(title, operation, done, failed):
        harness.queue.append((operation, done, failed))
        return True
    frame.run_job = run_job
    harness.frame, harness.bound, harness.wx = frame, bound, wx
    yield harness
    harness = None


def click(name):
    button = harness.named[name]
    assert button.enabled, name
    button.handlers['button'](None)


def drain():
    while harness.queue:
        operation, done, failed = harness.queue.pop(0)
        try:
            result = operation()
        except Exception as error:
            failed(error)
        else:
            done(result)


def test_native_workstation_default_no_then_explicit_jobs_run_and_new_docx_export(screen):
    h = screen
    def plan():
        assert not h.named['Approve and save DOCX jobs'].enabled
        click('Choose retained DOCX')
        assert h.queue
        drain()
        h.named['DOCX source language; auto detects'].SetValue('en')
        h.named['DOCX total approved USD budget; local service requires 0'].SetValue('0')
        click('Preview DOCX translation')
        drain()
        assert h.named['Approve and save DOCX jobs'].enabled
        before = list(h.bound.state['calls'])
        click('Approve and save DOCX jobs')
        assert not h.queue and not h.bound.service.list()
        assert h.messages[-1][2] & h.wx.NO_DEFAULT
        h.answer = h.wx.YES
        click('Approve and save DOCX jobs')
        drain()
        assert len(h.bound.service.list()) == 6 and h.bound.state['calls'] == before
        click('Run / resume DOCX jobs')
        drain()
        assert all(job['status'] == 'complete' for job in h.bound.service.list())
        click('Export new translated DOCX')
        drain()
        assert h.output.is_file()
        assert 'complete' in h.named['Formatted translation status and limitations'].GetValue()
        assert h.focus is h.named['Formatted translation status and limitations']
        assert h.bound.source.read_bytes() == h.bound.original
    h.plan = plan
    view.show_translation_layout(h.frame)


@pytest.mark.parametrize('mode', ['destroy', 'object', 'replaced-root'])
def test_actual_formatted_view_does_not_read_or_update_after_queued_scope_change(screen, monkeypatch, mode):
    h, read = screen, []
    monkeypatch.setattr(view, 'capture_layout', lambda *args, **kwargs: read.append(True))
    def plan():
        click('Choose retained DOCX')
        assert len(h.queue) == 1
        if mode == 'destroy':
            h.dialog.Destroy()
        elif mode == 'object':
            h.frame.library = LibraryApp(h.bound.source.parent / 'different-library')
        else:
            original = h.frame.library.root
            original.rename(original.with_name('old-library'))
            original.mkdir()
        marks = [len(item.updates) for item in h.widgets]
        drain()
        assert not read
        assert marks == [len(item.updates) for item in h.widgets]
    h.plan = plan
    view.show_translation_layout(h.frame)


def test_actual_formatted_picker_rechecks_same_path_library_replacement_before_queue(screen):
    h = screen
    def replace():
        root = h.frame.library.root
        root.rename(root.with_name('picker-old-library'))
        root.mkdir()
    def plan():
        h.picker_action = replace
        click('Choose retained DOCX')
        assert not h.queue
    h.plan = plan
    view.show_translation_layout(h.frame)


def test_existing_document_workstation_reaches_the_formatted_route_without_provider_calls(screen, monkeypatch):
    from carr_library.ui.translation_documents import show_translation_documents
    h, opened = screen, []
    monkeypatch.setattr(view, 'show_translation_layout', lambda frame: opened.append(frame))
    def plan():
        before = list(h.bound.state['calls'])
        click('Translate DOCX with layout')
        assert opened == [h.frame] and h.bound.state['calls'] == before and not h.queue
    h.plan = plan
    show_translation_documents(h.frame)
