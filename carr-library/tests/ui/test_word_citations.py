"""Actual Word dialog callbacks and one borrowed worker with synthetic transport."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import importlib.util
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace as N
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.ui.academic_library_dialog import AcademicController
from carr_library.citations.bibliography_workbench import BibliographyWorkbench, StylePreview
from carr_library.academic_integration.word_bridge import WordBridge, WordPreview, WordOutcome, _intent
from carr_library.academic_integration.word_records import canonical, fingerprint
from carr_library.ui import word_citations_dialog as ui
from carr_library.ui.citation_styles_dialog import create_citation_styles_dialog

_spec = importlib.util.spec_from_file_location('word_ui_fixtures',
    Path(__file__).parents[1] / 'academic_integration' / 'word_fixtures.py')
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


class Control:
    def __init__(self, *args, **kwargs):
        self.parent = args[0] if args else None
        self.value, self.label = kwargs.get('value', ''), kwargs.get('label', '')
        self.choices = list(kwargs.get('choices', ()))
        self.selection, self.checked = -1, []
        self.enabled, self.focused, self.destroyed, self.hidden = True, False, False, False
        self.bindings, self.modal_result = {}, None
    def Bind(self, event, callback): self.bindings[event] = callback
    def SetName(self, name): self.name = name
    def GetName(self): return getattr(self, 'name', '')
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def GetCheckedItems(self): return tuple(self.checked)
    def Append(self, value): self.choices.append(value)
    def Clear(self): self.choices.clear(); self.selection = -1
    def Enable(self, value): self.enabled = value
    def SetFocus(self): self.focused = True
    def SetInsertionPoint(self, *args): pass
    def SetMinSize(self, *args): pass
    def SetSizer(self, *args): pass
    def SetEscapeId(self, *args): pass
    def Add(self, *args, **kwargs): pass
    def AddGrowableCol(self, *args): pass
    def Wrap(self, *args): pass
    def IsModal(self): return True
    def EndModal(self, result): self.modal_result = result
    def SetReturnCode(self, result): self.modal_result = result
    def Hide(self): self.hidden = True
    def Destroy(self): self.destroyed = True
    def GetParent(self): return self.parent


@pytest.fixture
def form(tmp_path, monkeypatch):
    wx = ModuleType('wx')
    for name in ('Dialog', 'BoxSizer', 'FlexGridSizer', 'StaticText', 'Choice', 'CheckListBox',
                 'CheckBox', 'Button', 'TextCtrl'):
        setattr(wx, name, Control)
    for index, name in enumerate(('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
        'EXPAND', 'ALL', 'ALIGN_CENTER_VERTICAL', 'TE_MULTILINE', 'TE_READONLY', 'ALIGN_RIGHT',
        'ID_CANCEL', 'ID_OK', 'ID_YES', 'ID_NO', 'EVT_BUTTON', 'EVT_CHOICE', 'EVT_CLOSE',
        'EVT_WINDOW_DESTROY', 'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION', 'LEFT', 'RIGHT', 'BOTTOM')):
        setattr(wx, name, 1 << index)
    queued = []
    wx.CallAfter = lambda callback, *args: queued.append((callback, args))
    answer = N(value=wx.ID_NO, during=lambda: None)
    questions = []
    def question(*args, **kwargs):
        questions.append((args, kwargs))
        def modal():
            answer.during()
            return answer.value
        return N(ShowModal=modal, Destroy=Mock())
    wx.MessageDialog = question
    wx.TheClipboard = N(Open=Mock(return_value=True), SetData=Mock(return_value=True), Close=Mock())
    wx.TextDataObject = lambda value: value
    monkeypatch.setitem(sys.modules, 'wx', wx)
    library = LibraryApp(tmp_path / 'private')
    workspace = library.create_workspace('Word sources')
    controller = AcademicController(library, workspace)
    row = controller.create(dict(title='Reviewed source', authors=('Smith, Jane',), year='2020'))
    workbench = BibliographyWorkbench(controller)
    snapshot = controller.export(row.id, format='csl-json', include_notes=False)
    preview = StylePreview(workspace, row.id, sha256(snapshot.encode('utf-8')).hexdigest(),
                           'apa', 'en-US', 'literal', ('Reviewed source',), None)
    owner = N(current=True)
    def guard():
        if not owner.current:
            raise ValueError('The original owner is closed or changed.')
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='actual-word-ui-test-worker')
    shutdown = executor.shutdown
    executor.shutdown = Mock(wraps=shutdown)
    calls, approved = [], []
    held = threading.Event(); held.set()
    instances = []
    class SyntheticBridge(WordBridge):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            instances.append(self)
        def run(self, intent=None, *, decision=None, on_preview=None, cancelled=lambda: False, **kwargs):
            if intent is not None:
                _intent(intent)
            self._source()
            calls.append((threading.get_ident(), intent))
            if intent is None:
                return WordOutcome('INSPECTED', 'Synthetic read-only protocol inspection.', fixture.SESSION,
                    'Synthetic.docx', capture=fixture.installed_capture(fixture.proposal()))
            proposal = fixture.proposal()
            token = fingerprint(proposal)
            view = WordPreview(token, 'Synthetic.docx', intent['action'], 'Synthetic finite preview.',
                canonical(proposal['interchange']['references']).decode('ascii'), 60, canonical(proposal))
            decision.offer(token)
            on_preview(view)
            deadline = time.monotonic() + 5
            while not decision.state(token):
                if decision.closed or cancelled():
                    raise InterruptedError('Synthetic stop before a native grant.')
                self._source()
                if time.monotonic() >= deadline:
                    raise TimeoutError('Synthetic approval expired.')
                time.sleep(.005)
            approved.append(token)
            if not held.wait(5):
                raise TimeoutError('Synthetic completion barrier expired.')
            result = WordOutcome('COMPLETED', 'Verified synthetic completion; not actual Word.',
                fixture.SESSION, 'Synthetic.docx', receipt=fixture.receipt(proposal))
            self.last_outcome = result
            return result
    monkeypatch.setattr(ui, 'WordBridge', SyntheticBridge)
    dialog = ui.create_word_citation_dialog(None, workbench, preview, executor=executor, guard=guard)

    def drain():
        while queued:
            callback, args = queued.pop(0)
            callback(*args)

    def until(predicate, seconds=6):
        deadline = time.monotonic() + seconds
        while not predicate():
            drain()
            if time.monotonic() >= deadline:
                raise AssertionError('The actual borrowed-worker callback did not reach its bounded state.')
            time.sleep(.005)
        drain()

    yield N(dialog=dialog, executor=executor, wx=wx, drain=drain, until=until, answer=answer,
        questions=questions, calls=calls, approved=approved, owner=owner, held=held,
        library=library, controller=controller, row=row, workbench=workbench, preview=preview,
        instances=instances, guard=guard, queued=queued)
    held.set()
    dialog.on_destroy(N(GetEventObject=lambda: dialog, Skip=Mock()))
    shutdown(wait=True, cancel_futures=True)
    drain()


def start_preview(form):
    form.dialog.references.checked = [0]
    form.dialog.on_preview()
    form.until(lambda: form.dialog.view is not None)
    assert form.dialog.pending is not None and form.dialog.apply_button.enabled


def test_opening_and_closing_do_not_attach_or_mutate_word_or_shutdown_borrowed_worker(form):
    assert form.calls == [] and not form.dialog.apply_button.enabled
    form.dialog.on_close()
    assert not form.dialog.alive and form.dialog.modal_result == form.wx.ID_CANCEL
    form.executor.shutdown.assert_not_called()
    assert form.calls == []


def test_real_borrowed_worker_inspection_exposes_stable_ids_and_inert_references(form):
    gui_thread = threading.get_ident()
    form.dialog.on_inspect()
    form.until(lambda: form.dialog.pending is None)
    assert form.calls[0][0] != gui_thread and form.calls[0][1] is None
    assert form.dialog.target_ids == [fixture.CITATION]
    assert 'Protocol fixture' not in form.dialog.status.value
    assert 'ref' in form.dialog.references_output.value
    assert form.dialog.copy_button.enabled
    form.dialog.on_copy_references()
    assert form.wx.TheClipboard.SetData.call_count == 1
    assert 'no source or edit authority' in form.dialog.status.value
    form.executor.shutdown.assert_not_called()


def test_preview_never_applies_without_default_no_confirmation(form):
    start_preview(form)
    assert not form.approved
    form.dialog.on_apply()
    assert not form.approved
    assert form.questions[-1][1]['style'] & form.wx.NO_DEFAULT
    assert form.dialog.view is not None
    form.dialog.on_stop()
    form.until(lambda: form.dialog.pending is None)


def test_explicit_yes_keeps_exact_preview_and_actual_completion_visible(form):
    start_preview(form)
    token = form.dialog.view.token
    form.answer.value = form.wx.ID_YES
    form.dialog.on_apply()
    form.until(lambda: form.dialog.pending is None)
    assert form.approved == [token]
    assert 'Verified synthetic completion' in form.dialog.status.value
    assert form.dialog.status.focused and not form.dialog.apply_button.enabled
    assert form.dialog.target_ids == []
    assert form.instances[-1].last_outcome.status == 'COMPLETED'


@pytest.mark.parametrize('change', ['owner', 'root', 'decision', 'preview'])
def test_confirmation_return_rechecks_exact_owner_directory_and_preview(form, tmp_path, change):
    start_preview(form)
    form.answer.value = form.wx.ID_YES
    if change == 'owner':
        form.answer.during = lambda: setattr(form.owner, 'current', False)
    elif change == 'root':
        def replace():
            form.library.root.rename(tmp_path / 'old-root')
            form.library.root.mkdir()
        form.answer.during = replace
    elif change == 'decision':
        form.answer.during = form.dialog.decision.close
    else:
        form.answer.during = lambda: setattr(form.dialog, 'view', None)
    form.dialog.on_apply()
    assert not form.approved
    form.dialog.cancelled.set()
    if form.dialog.decision is not None:
        form.dialog.decision.close()
    form.until(lambda: form.dialog.pending is None)


def test_stop_after_approval_does_not_erase_a_verified_late_completion(form):
    form.held.clear()
    start_preview(form)
    form.answer.value = form.wx.ID_YES
    form.dialog.on_apply()
    form.until(lambda: bool(form.approved))
    form.dialog.on_stop()
    assert form.dialog.pending is not None
    form.held.set()
    form.until(lambda: form.dialog.pending is None)
    assert 'Verified synthetic completion' in form.dialog.status.value
    assert 'cancelled' not in form.dialog.status.value.lower()


def test_busy_close_requests_stop_and_waits_for_outcome_before_native_destruction(form):
    start_preview(form)
    form.dialog.on_close()
    assert form.dialog.alive and form.dialog.modal_result is None
    form.until(lambda: form.dialog.pending is None)
    assert form.dialog.alive
    form.dialog.on_close()
    assert not form.dialog.alive and form.dialog.modal_result == form.wx.ID_CANCEL
    form.executor.shutdown.assert_not_called()


def test_destroyed_dialog_ignores_all_queued_callbacks_and_retains_worker_cleanup(form):
    start_preview(form)
    old_status, old_output = form.dialog.status.value, form.dialog.output.value
    form.dialog.on_destroy(N(GetEventObject=lambda: form.dialog, Skip=Mock()))
    form.until(lambda: form.dialog.pending is None)
    assert form.dialog.status.value == old_status and form.dialog.output.value == old_output
    assert not form.approved
    form.executor.shutdown.assert_not_called()


def test_queued_work_rechecks_original_scope_before_attaching(form):
    blocker, release = threading.Event(), threading.Event()
    def block():
        blocker.set()
        assert release.wait(5)
    first = form.executor.submit(block)
    assert blocker.wait(2)
    try:
        form.dialog.on_inspect()
        form.owner.current = False
    finally:
        release.set()
    first.result(timeout=5)
    form.until(lambda: form.dialog.pending is None)
    assert form.calls == []


@pytest.mark.parametrize('kind', ['none', 'unverified-pinpoint', 'multiple-pinpoints', 'unknown-edit'])
def test_invalid_selection_is_refused_without_attaching_or_mutating(form, kind):
    if kind != 'none':
        form.dialog.references.checked = [0]
    if kind == 'unverified-pinpoint':
        form.dialog.locator.SetValue('12')
    elif kind == 'multiple-pinpoints':
        # Out-of-range IDs never become a fallback to a different reference.
        form.dialog.references.checked = [0, 999]
        form.dialog.locator.SetValue('12')
    elif kind == 'unknown-edit':
        form.dialog.action.SetSelection(1)
    form.dialog.on_preview()
    if kind == 'unverified-pinpoint':
        # The service validates the explicit pinpoint envelope before native launch.
        form.until(lambda: form.dialog.pending is None)
    else:
        assert form.dialog.pending is None
    assert not form.approved and form.calls == []


def test_csl_workbench_hook_borrows_same_worker_and_preserves_exact_host_scope(form, monkeypatch):
    host = N(library=form.library, active=(form.controller.workspace, None), _closing=False, GetParent=lambda: None)
    parent = Control(host)
    style = create_citation_styles_dialog(parent, form.controller, executor=form.executor)
    style.preview = form.preview
    style.workbench = form.workbench
    captured = {}
    child = N(Destroy=Mock())
    def create(parent, workbench, preview, **kwargs):
        captured.update(parent=parent, workbench=workbench, preview=preview, **kwargs)
        def modal():
            kwargs['guard']()
            host.library = LibraryApp(form.library.root.parent / 'other-library')
            with pytest.raises(ValueError):
                kwargs['guard']()
        child.ShowModal = modal
        return child
    monkeypatch.setattr(ui, 'create_word_citation_dialog', create)
    style.on_word()
    assert captured['executor'] is form.executor and captured['preview'] is form.preview
    assert captured['workbench'] is form.workbench
    child.Destroy.assert_called_once()
    form.executor.shutdown.assert_not_called()
    assert 'unavailable' in style.status.value
