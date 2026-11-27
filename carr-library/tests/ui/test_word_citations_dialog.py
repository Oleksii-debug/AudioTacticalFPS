"""Real worker/Event and database lifetimes; controlled wx/Word boundaries only."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue, Empty
import sys
import threading
import time
from types import ModuleType, SimpleNamespace as N

import pytest

from carr_library.application import LibraryApp
from carr_library.citations.bibliography_workbench import BibliographyWorkbench
from carr_library.citations.word_com import WordCopy, WordResult
from carr_library.ui.academic_library_dialog import AcademicController
from carr_library.ui.word_citations_dialog import create_word_citations_dialog, _host


class Control:
    def __init__(self, *args, **kwargs):
        self.value = kwargs.get('value', '')
        self.choices = list(kwargs.get('choices', ()))
        self.selection, self.enabled, self.result = -1, True, None
    def Bind(self, *args, **kwargs): pass
    def SetName(self, value): self.name = value
    def SetValue(self, value): self.value = value
    def GetValue(self): return self.value
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def GetCount(self): return len(self.choices)
    def GetString(self, index): return self.choices[index]
    def Append(self, value): self.choices.append(value)
    def Clear(self): self.choices.clear()
    def Enable(self, value): self.enabled = value
    def SetFocus(self): pass
    def Add(self, *args, **kwargs): pass
    def Wrap(self, *args): pass
    def SetSizer(self, *args): pass
    def SetEscapeId(self, *args): pass
    def SetInsertionPoint(self, *args): pass
    def IsModal(self): return True
    def EndModal(self, value): self.result = value
    def Hide(self): self.result = 'hidden'


@pytest.fixture
def form(tmp_path, monkeypatch):
    wx = ModuleType('wx')
    for name in ('Dialog', 'BoxSizer', 'StaticText', 'Choice', 'Button', 'TextCtrl'):
        setattr(wx, name, Control)
    for index, name in enumerate(('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL',
            'HORIZONTAL', 'EXPAND', 'ALL', 'TE_MULTILINE', 'TE_READONLY', 'ALIGN_RIGHT',
            'ID_CANCEL', 'ID_OK', 'EVT_BUTTON', 'EVT_CLOSE', 'EVT_WINDOW_DESTROY', 'FD_SAVE')):
        setattr(wx, name, 1 << index)
    queued = Queue()
    wx.CallAfter = lambda callback, *args: queued.put((callback, args))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Word UI scope')
    controller = AcademicController(library, workspace)
    row = controller.create(dict(title='Real metadata', year='2024', authors=('Jane Reader',)))
    workbench = BibliographyWorkbench(controller)
    preview = workbench.preview()
    executor = ThreadPoolExecutor(max_workers=1)
    host = N(library=library, cancel_event=threading.Event(), working=False, busy=False,
             _closing=False, calls=0, future=None)

    def run_job(title, operation, done=None, failed=None, **kwargs):
        host.working = True
        host.calls += 1
        host.future = executor.submit(operation)
        def complete(future):
            host.working = False
            if host._closing:
                return
            try:
                result = future.result()
            except Exception as exc:
                if failed:
                    wx.CallAfter(failed, exc)
            else:
                if done:
                    wx.CallAfter(done, result)
        host.future.add_done_callback(complete)
        return None  # The actual DesktopFrame contract.

    host.run_job = run_job
    copy = WordCopy(tmp_path / 'copy.docx', tmp_path / 'original.docx', '0' * 64)
    result = WordResult(copy.path, ('occurrence-one',), (row.id,), 'in-text', True)
    calls = []
    bridge = N(change=lambda *args, **kwargs: calls.append(kwargs) or result)
    dialog = create_word_citations_dialog(None, workbench, preview, host=host, bridge=bridge)
    dialog.copy = copy
    dialog.controls()

    def drain():
        while True:
            try:
                callback, args = queued.get_nowait()
            except Empty:
                break
            callback(*args)

    def settled():
        deadline = time.monotonic() + 5
        while dialog.pending is not None and time.monotonic() < deadline:
            drain()
            if dialog.pending is not None:
                time.sleep(.005)
        drain()
        assert dialog.pending is None

    yield N(dialog=dialog, host=host, workbench=workbench, preview=preview, row=row,
            copy=copy, result=result, bridge=bridge, calls=calls, drain=drain, settled=settled, wx=wx)
    dialog.cancelled.set()
    host.cancel_event.set()
    executor.shutdown(wait=True, cancel_futures=True)


def test_existing_host_owns_worker_and_none_return_is_successful_admission(form):
    ui = form.dialog
    ui.change(insert=True)
    form.settled()
    assert form.host.calls == 1 and len(form.calls) == 1
    assert form.calls[0]['items'] == [{'id': form.row.id}]
    assert form.calls[0]['preview'] is form.preview and form.calls[0]['insert'] is True
    assert ui.insert_button.enabled and ui.copy == form.copy
    assert 'Original preserved' in ui.status.value
    assert ui.occurrence.choices == ['occurrence-one']


@pytest.mark.parametrize('blocked', ['working', 'closing', 'library'])
def test_busy_or_stale_desktop_scope_never_submits_a_second_operation(form, blocked):
    if blocked == 'working':
        form.host.working = True
    elif blocked == 'closing':
        form.host._closing = True
    else:
        form.host.library = object()
    form.dialog.change(insert=True)
    assert form.host.calls == 0 and form.dialog.pending is None and not form.calls
    assert 'busy or its library changed' in form.dialog.status.value


def test_close_waits_for_actual_worker_cancellation_even_if_host_drops_callbacks(form):
    entered, release = threading.Event(), threading.Event()
    def operation(*args, cancelled, **kwargs):
        entered.set()
        assert release.wait(3)
        if cancelled():
            raise InterruptedError('Actual worker observed the close request')
        pytest.fail('Closing must reach the actual worker cancellation boundary.')
    form.bridge.change = operation
    form.dialog.change(insert=True)
    assert entered.wait(3)
    form.host._closing = True
    form.dialog.on_close()
    assert form.dialog.pending is not None and form.dialog.result is None
    release.set()
    form.settled()
    assert form.dialog.result == form.wx.ID_CANCEL
    assert 'Actual worker observed' in form.dialog.status.value
    assert form.host.calls == 1


def test_cancel_after_success_preserves_the_real_result_and_closes_once(form):
    entered, release = threading.Event(), threading.Event()
    def committed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return form.result
    form.bridge.change = committed
    form.dialog.change(insert=True)
    assert entered.wait(3)
    form.dialog.on_close()
    release.set()
    form.settled()
    assert form.dialog.copy is form.copy and 'Original preserved' in form.dialog.status.value
    assert form.dialog.result == form.wx.ID_CANCEL and form.host.calls == 1


def test_executor_admission_failure_restores_controls_without_word_retry(form):
    def failed(*args, **kwargs):
        raise RuntimeError('Executor refused submission')
    form.host.run_job = failed
    form.dialog.change(insert=True)
    assert form.dialog.pending is None and form.dialog.insert_button.enabled
    assert 'Executor refused submission' in form.dialog.status.value and not form.calls


def test_host_cancel_and_scope_change_are_visible_to_the_worker(form):
    entered, release = threading.Event(), threading.Event()
    def operation(*args, cancelled, **kwargs):
        entered.set()
        assert release.wait(3)
        assert cancelled()
        raise InterruptedError('Worker refused changed host scope')
    form.bridge.change = operation
    form.dialog.change(insert=True)
    assert entered.wait(3)
    form.host.library = object()
    form.host.cancel_event.set()
    release.set()
    form.settled()
    assert 'changed host scope' in form.dialog.status.value and form.host.calls == 1


def test_existing_ancestor_host_is_reused_and_cycles_are_refused():
    host = N(run_job=lambda *args, **kwargs: None)
    child = N(parent=N(parent=host))
    assert _host(child) is host
    child.parent = child
    with pytest.raises(RuntimeError, match='current research-library'):
        _host(child)
