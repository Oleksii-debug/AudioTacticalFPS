"""Actual RIS dialog/controller with widget doubles and real disk-backed services.

These checks execute native handler logic, not a Windows/NVDA acceptance run.
Jobs are explicitly queued so neither parsing nor publication can happen while
the file chooser or review dialog is merely being opened.
"""
import importlib.util
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.ris_preview import preview_ris


class Control:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get('value', '')
        self.items = list(kwargs.get('choices', ()))
        self.selected = -1
        self.enabled = True
        self.focused = self.destroyed = False
        self.modal_result = None
        self.bindings = {}
    def SetName(self, value): self.name = value
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def SetItems(self, values): self.items = list(values)
    def SetSelection(self, value): self.selected = value
    def GetSelection(self): return self.selected
    def GetStringSelection(self): return self.items[self.selected] if self.selected >= 0 else ''
    def SetMinSize(self, value): pass
    def SetSizer(self, value): pass
    def SetEscapeId(self, value): pass
    def Add(self, *args, **kwargs): pass
    def Wrap(self, value): pass
    def Bind(self, event, callback): self.bindings[event] = callback
    def Enable(self, value): self.enabled = value
    def SetFocus(self): self.focused = True
    def EndModal(self, value): self.modal_result = value
    def Destroy(self): self.destroyed = True


@pytest.fixture
def module():
    wx = ModuleType('wx')
    for i, name in enumerate(('VERTICAL', 'HORIZONTAL', 'EXPAND', 'ALL', 'LEFT', 'RIGHT',
            'ALIGN_CENTER_VERTICAL', 'ALIGN_RIGHT', 'TE_MULTILINE', 'TE_READONLY',
            'ID_OK', 'ID_CANCEL', 'EVT_BUTTON', 'EVT_CHOICE', 'FD_OPEN', 'FD_FILE_MUST_EXIST')):
        setattr(wx, name, 1 << i)
    wx.NOT_FOUND = -1
    wx.file_result = wx.ID_OK
    wx.file_path = ''
    wx.forms = []
    wx.review_action = lambda form: None

    class Dialog(Control):
        def ShowModal(self):
            wx.forms.append(self)
            wx.review_action(self)
            return self.modal_result if self.modal_result is not None else wx.ID_CANCEL

    class FileDialog(Control):
        def ShowModal(self): return wx.file_result
        def GetPath(self): return wx.file_path

    wx.Dialog, wx.FileDialog = Dialog, FileDialog
    for name in ('StaticText', 'TextCtrl', 'Choice', 'CheckBox', 'Button', 'BoxSizer'):
        setattr(wx, name, Control)
    path = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/bibliography_import.py'
    spec = importlib.util.spec_from_file_location('carr_library.ui._bibliography_import_test', path)
    result = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'wx': wx}):
        spec.loader.exec_module(result)
    return result


class Frame:
    def __init__(self, app, workspace):
        self.library = app
        self.active = (workspace, None)
        self.working = self.busy = self._closing = False
        self.cancel_event = Event()
        self.messages, self.jobs, self.queue = [], [], []
        self.refreshed, self.invalidated = 0, 0
        self.selection = (None, None, 'e1')
        catalog = SimpleNamespace(workspace=workspace, ready=True,
            _selection=lambda: self.selection, refresh=self.refresh)
        search = SimpleNamespace(refresh_collections=self.invalidate)
        self.book = SimpleNamespace(GetPage=lambda i: (search, catalog)[i])
        self.unsaved_note = Control(value='Unfinished private research note')

    def refresh(self, *, selection):
        self.refreshed += 1
        self.selection = selection
        return True

    def invalidate(self): self.invalidated += 1
    def report(self, text): self.messages.append(text)

    def run_job(self, title, operation, done=None):
        assert not self.working
        self.working = self.busy = True
        self.cancel_event.clear()
        self.jobs.append(title)
        self.queue.append((operation, done))

    def complete_next(self, *, cancelled=False):
        operation, done = self.queue.pop(0)
        if cancelled:
            self.cancel_event.set()
        try:
            result = operation()
        except Exception as exc:
            self.working = self.busy = False
            self.report(str(exc))
            return
        self.working = self.busy = False
        try:
            if done:
                done(result)
        except Exception as exc:
            self.report(str(exc))


RIS = ('TY  - BOOK\nTI  - Українська історія\nTI  - Alternative title\n'
       'AU  - Олена\nAU  - Žan\nLA  - uk\nDO  - 10.1234/unverified\n'
       'L1  - ../../secret.pdf\nAB  - Not imported\nER  -\n').encode('utf-8')


@pytest.fixture
def frame(tmp_path, module):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bibliography target')
    app.add_text(workspace, 'Original title', 'Exact railway source text.',
                 authors=('Original author',), language='en', edition='First', volume='II', edition_id='e1')
    path = tmp_path / 'bibliography.ris'
    path.write_bytes(RIS)
    module.wx.file_path = str(path)
    return Frame(app, workspace)


def accept_title(form):
    form.checks['title'].SetValue(True)
    form.on_apply()


def test_real_workflow_reviews_then_queues_atomic_apply_and_refreshes_only_metadata_views(frame, module):
    before = frame.library.edition_metadata(frame.active[0], 'e1')
    def choose(form):
        assert not any(check.GetValue() for check in form.checks.values())
        assert form.target.focused
        assert all(control.name for control in (*form.checks.values(), *form.fields.values()))
        assert '10.1234/unverified' in form.details.GetValue()
        assert 'not imported' in form.details.GetValue()
        assert 'does not verify a quotation' in form.details.GetValue()
        assert not form.checks['edition'].enabled and not form.checks['volume'].enabled
        form.fields['title'].SetSelection(1)
        for field in ('title', 'authors', 'language'):
            form.checks[field].SetValue(True)
        form.on_apply()
    module.wx.review_action = choose
    module.open_bibliography_import(frame)
    assert len(frame.queue) == 1 and not module.wx.forms
    assert frame.library.edition_metadata(frame.active[0], 'e1') == before
    frame.complete_next()
    assert len(frame.queue) == 1 and module.wx.forms[0].destroyed
    assert frame.library.edition_metadata(frame.active[0], 'e1') == before
    frame.complete_next()
    after = frame.library.edition_metadata(frame.active[0], 'e1')
    assert (after.title, after.authors, after.language) == ('Alternative title', ('Олена', 'Žan'), 'uk')
    assert (after.edition, after.volume, after.asset) == (before.edition, before.volume, before.asset)
    assert frame.refreshed == frame.invalidated == 1
    assert frame.unsaved_note.GetValue() == 'Unfinished private research note'
    assert 'Selected RIS fields saved' in frame.messages[-1]


def test_apply_without_explicit_field_selection_keeps_dialog_open_and_does_not_write(frame, module):
    before = frame.library.edition_metadata(frame.active[0], 'e1')
    def choose(form):
        form.on_apply()
        assert form.modal_result is None and form.status.focused
        assert 'Select at least one' in form.status.GetValue()
    module.wx.review_action = choose
    module.open_bibliography_import(frame)
    frame.complete_next()
    assert not frame.queue
    assert frame.library.edition_metadata(frame.active[0], 'e1') == before
    assert 'cancelled' in frame.messages[-1]


def test_changing_record_resets_every_field_confirmation_and_shows_current_values(frame, module):
    records = preview_ris(RIS + b'TY  - JOUR\nTI  - Another source\nER  -\n')
    rows = frame.library.browse(frame.active[0])
    form = module.BibliographyReviewDialog(frame, frame.library, frame.active[0], records, rows)
    form.checks['title'].SetValue(True)
    form.record.SetSelection(1)
    form.on_selection()
    assert not any(check.GetValue() for check in form.checks.values())
    assert not form.checks['authors'].enabled
    assert form.fields['title'].GetStringSelection() == 'Another source'
    assert 'Original title' in form.details.GetValue()


@pytest.mark.parametrize('stage', ['parse', 'apply'])
def test_job_cancellation_does_not_apply_metadata(frame, module, stage):
    before = frame.library.edition_metadata(frame.active[0], 'e1')
    module.wx.review_action = accept_title
    module.open_bibliography_import(frame)
    frame.complete_next(cancelled=stage == 'parse')
    if stage == 'apply':
        frame.complete_next(cancelled=True)
    assert not frame.queue
    assert frame.library.edition_metadata(frame.active[0], 'e1') == before
    assert 'cancelled' in frame.messages[-1]


def test_scope_change_during_review_prevents_publication(frame, module):
    workspace = frame.active[0]
    before = frame.library.edition_metadata(workspace, 'e1')
    other = frame.library.create_workspace('Other library')
    def choose(form):
        accept_title(form)
        frame.active = (other, None)
    module.wx.review_action = choose
    module.open_bibliography_import(frame)
    frame.complete_next()
    assert not frame.queue
    assert frame.library.edition_metadata(workspace, 'e1') == before
    assert 'changed' in frame.messages[-1]


def test_concurrent_metadata_edit_during_review_is_not_overwritten(frame, module):
    def choose(form):
        accept_title(form)
        frame.library.update_metadata(frame.active[0], 'e1', title='Concurrent title', authors=('New author',))
    module.wx.review_action = choose
    module.open_bibliography_import(frame)
    frame.complete_next()
    frame.complete_next()
    after = frame.library.edition_metadata(frame.active[0], 'e1')
    assert (after.title, after.authors) == ('Concurrent title', ('New author',))
    assert frame.refreshed == 0
    assert 'metadata changed' in frame.messages[-1]


def test_malformed_file_never_opens_review_or_applies_valid_prefix(frame, module):
    before = frame.library.edition_metadata(frame.active[0], 'e1')
    Path(module.wx.file_path).write_bytes(RIS + b'TY  - BOOK\nTI  - Broken')
    module.open_bibliography_import(frame)
    frame.complete_next()
    assert not module.wx.forms and not frame.queue
    assert frame.library.edition_metadata(frame.active[0], 'e1') == before
    assert 'incomplete' in frame.messages[-1]


def test_file_chooser_cancel_and_foreign_edition_create_no_write_jobs(frame, module):
    module.wx.file_result = module.wx.ID_CANCEL
    module.open_bibliography_import(frame)
    assert not frame.jobs
    module.wx.file_result = module.wx.ID_OK
    module.open_bibliography_import(frame, edition_id='foreign-edition')
    frame.complete_next()
    assert not module.wx.forms and not frame.queue
    assert 'Select an imported edition' in frame.messages[-1]
