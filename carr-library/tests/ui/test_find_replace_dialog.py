"""Real queued reader work and actual find-dialog handlers, with thread-owned controls."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from threading import Event, get_ident
from types import SimpleNamespace
import time

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui import find_replace_dialog as ui

# Reuse the existing pronunciation test's native-ownership/queued-worker harness.

class Widget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.owner = get_ident()
        self.value, self.label, self.choices = value, label, list(choices)
        self.selection, self.enabled, self.name = -1, True, ''
        self.destroyed, self.children, self.handlers = False, [], {}
        self.parent = parent if isinstance(parent, Widget) else None
        if isinstance(parent, Widget):
            parent.children.append(self)

    def native(self):
        assert get_ident() == self.owner, 'Native control accessed from the worker.'
        assert not self.destroyed, 'Native control accessed after destruction.'

    def SetName(self, name): self.native(); self.name = name
    def GetName(self): self.native(); return self.name
    def GetValue(self): self.native(); return self.value
    def ChangeValue(self, value): self.native(); self.value = value
    def SetValue(self, value): self.native(); self.value = value
    def Set(self, values): self.native(); self.choices = list(values); self.selection = -1
    def SetSelection(self, index): self.native(); self.selection = index
    def GetSelection(self): self.native(); return self.selection
    def Enable(self, enabled=True): self.native(); self.enabled = enabled
    def IsEnabled(self): self.native(); return self.enabled
    def Bind(self, event, handler, *args): self.native(); self.handlers[event] = handler
    def SetSizer(self, *args): self.native()
    def SetScrollRate(self, *args): self.native()
    def FitInside(self): self.native()
    def Wrap(self, *args): self.native()
    def SetFocus(self): self.native(); self.focused = True
    def IsModal(self): self.native(); return False
    def Hide(self): self.native(); self.hidden = True
    def Destroy(self):
        self.native()
        for child in tuple(self.children):
            child.Destroy()
        if self.parent is not None:
            self.parent.children.remove(self)
        self.destroyed = True


class Sizer:
    def __init__(self, *args): pass
    def Add(self, *args): pass


class ReaderWorker:
    """The reader's admission/cancel/queued-completion contract with real threads."""
    def __init__(self, cancel):
        self.cancel, self.owner = cancel, get_ident()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pronunciation-handler')
        self.callbacks, self.gate = Queue(), Event()
        self.gate.set()
        self.busy, self.future, self.worker_threads = False, None, []
        self.reject = False

    def __call__(self, operation, done, *, on_failed):
        assert get_ident() == self.owner
        if self.busy or self.reject:
            return False
        self.busy = True
        self.cancel.clear()
        def work():
            self.worker_threads.append(get_ident())
            if not self.gate.wait(5):
                raise TimeoutError('Synthetic worker admission barrier timed out.')
            return operation()
        self.future = self.executor.submit(work)
        self.future.add_done_callback(lambda future: self.callbacks.put((future, done, on_failed)))
        return True

    def ready(self):
        assert self.future is not None
        try:
            self.future.result(timeout=5)
        except Exception:
            pass

    def drain(self):
        assert get_ident() == self.owner
        future, done, failed = self.callbacks.get(timeout=5)
        self.busy = False
        try:
            done(future.result(timeout=5))
        except Exception as exc:
            failed(exc)

    def close(self):
        self.cancel.set(); self.gate.set()
        self.executor.shutdown(wait=True, cancel_futures=False)
        while not self.callbacks.empty():
            self.drain()


@pytest.fixture
def view(tmp_path):
    owner = get_ident()
    original = '😀 Cat first.\r\ncat second. cat final.'
    state = SimpleNamespace(document=ReadingDocument(original), selection=(0, 0),
        authorized=True, closed=False, applied=0, undone=0, guard_threads=[])
    store = ReaderDraftStore(tmp_path)
    def guard():
        state.guard_threads.append(get_ident())
        if state.closed or not state.authorized:
            raise ValueError('The source scope is no longer available.')
    def capture():
        assert get_ident() == owner
        guard()
        return ui.ReadingSnapshot.capture(state.document, state.selection)
    def select(preview, index):
        assert get_ident() == owner
        guard(); ui.check_find_preview(state.document, preview, capture())
        item = preview.matches[index]
        state.selection = (item.start, item.end)
        return capture()
    def apply(preview, index):
        assert get_ident() == owner
        guard()
        ui.apply_find_preview(state.document, preview, capture(), index=index)
        state.selection = (0, 0); state.applied += 1
        store.save(ReaderResume(state.document))
        return capture(), ''
    def undo(expected):
        assert get_ident() == owner
        guard()
        if capture() != expected:
            raise ValueError('The reading copy changed after replacement.')
        assert state.document.undo()
        state.selection = (0, 0); state.undone += 1
        store.save(ReaderResume(state.document))
        return ''
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VSCROLL', 'VERTICAL', 'HORIZONTAL',
        'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE', 'EVT_BUTTON', 'EVT_CLOSE', 'EVT_TEXT',
        'EVT_LISTBOX', 'EVT_CHECKBOX', 'EVT_CHOICE', 'EVT_CHAR_HOOK', 'WXK_ESCAPE', 'ID_CANCEL')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(constants)})
    for name in ('Dialog', 'ScrolledWindow', 'StaticText', 'TextCtrl', 'ListBox', 'CheckBox', 'Choice', 'Button'):
        setattr(wx, name, Widget)
    wx.BoxSizer = Sizer
    cancel = Event(); worker = ReaderWorker(cancel)
    dialog = ui.create_find_dialog(None, capture=capture, select=select, apply=apply,
        undo=undo, guard=guard, run=worker, cancel=cancel, wx_module=wx)
    value = SimpleNamespace(dialog=dialog, state=state, worker=worker, cancel=cancel, wx=wx,
                            owner=owner, capture=capture, original=original, store=store)
    try:
        yield value
    finally:
        if dialog.alive:
            dialog.on_close()
        worker.close()
        if not dialog.destroyed:
            dialog.Destroy()


def form(view, pattern='cat', replacement='dog', *, regex=False, ignore_case=False, selected=False):
    dialog = view.dialog
    dialog.pattern.ChangeValue(pattern); dialog.replacement.ChangeValue(replacement)
    dialog.regex.SetValue(regex); dialog.ignore_case.SetValue(ignore_case)
    dialog.scope.SetSelection(int(selected))
    dialog.on_change()


def preview(view):
    view.dialog.on_preview()
    view.worker.drain()
    assert view.dialog.preview is not None, view.dialog.status.GetValue()


def test_real_preview_select_replace_one_and_undo_run_on_the_existing_worker(view):
    form(view, ignore_case=True)
    preview(view)
    assert view.state.document.text == view.original and view.state.applied == 0
    assert len(view.dialog.preview.matches) == 3
    assert view.state.selection == (view.original.index('Cat'), view.original.index('Cat') + 3)
    view.dialog.move(1)
    assert view.state.selection == (view.original.index('cat'), view.original.index('cat') + 3)
    view.dialog.on_one()
    assert view.state.document.text == view.original.replace('cat', 'dog', 1)
    assert view.state.applied == 1 and view.state.document.revision == 1
    assert view.store.load().document.source_text == view.original
    view.dialog.on_undo()
    assert view.state.document.text == view.original and view.state.undone == 1
    assert view.worker.worker_threads and all(value != view.owner for value in view.worker.worker_threads)


def test_replace_all_keeps_the_original_selected_scope_after_highlighting_a_match(view):
    first = view.original.index('cat')
    view.state.selection = (first, len(view.original))
    form(view, selected=True)
    preview(view)
    assert view.state.selection == (first, first + 3)
    assert view.dialog.preview.start == first and len(view.dialog.preview.matches) == 2
    view.dialog.on_all()
    assert view.state.document.text == view.original[:first] + view.original[first:].replace('cat', 'dog')
    assert view.state.document.source_text == view.original


@pytest.mark.parametrize('changed', ['text', 'selection', 'form', 'authority'])
def test_stale_queued_result_is_discarded_before_review_or_apply(view, changed):
    form(view)
    view.worker.gate.clear()
    view.dialog.on_preview()
    if changed == 'text':
        view.state.document.set_text('Changed while the search was pending.')
    elif changed == 'selection':
        view.state.selection = (0, 2)
    elif changed == 'form':
        view.dialog.pattern.ChangeValue('Different')
    else:
        view.state.authorized = False
    view.worker.gate.set(); view.worker.drain()
    assert view.dialog.preview is None and view.state.applied == 0
    assert not view.dialog.buttons['all'].IsEnabled()


def test_form_changes_invalidate_old_matches_and_cannot_apply_them(view):
    form(view); preview(view)
    view.dialog.replacement.ChangeValue('unreviewed')
    view.dialog.on_all()
    assert view.state.document.text == view.original and view.state.applied == 0
    assert view.dialog.preview is None


def test_previous_next_are_bounded_and_never_wrap(view):
    form(view); preview(view)
    before = view.state.selection
    view.dialog.move(-1)
    assert view.state.selection == before and view.dialog.index == 0
    view.dialog.move(1)
    last = view.state.selection
    view.dialog.move(1)
    assert view.state.selection == last and view.dialog.index == 1
    assert view.state.document.text == view.original


def test_worker_admission_refusal_restores_idle_controls(view):
    view.worker.reject = True
    form(view); view.dialog.on_preview()
    assert not view.dialog.busy and view.dialog.preview is None
    assert view.dialog.buttons['preview'].IsEnabled()
    assert 'running' in view.dialog.status.GetValue()


def test_cancelled_preview_cannot_apply_and_worker_cleanup_is_owned(view):
    form(view); view.worker.gate.clear()
    view.dialog.on_preview(); view.dialog.on_cancel()
    view.worker.gate.set(); view.worker.drain()
    assert view.dialog.preview is None and not view.dialog.busy
    assert view.state.document.text == view.original and view.state.applied == 0


def test_close_after_computation_discards_queued_completion_without_native_access(view):
    form(view); view.dialog.on_preview()
    view.worker.ready()
    view.dialog.on_close(); view.dialog.Destroy()
    view.worker.drain()
    assert view.dialog.destroyed and not view.dialog.alive
    assert view.state.document.text == view.original and view.state.applied == 0


def test_closed_dialog_cannot_undo_or_cancel_a_new_reader_operation(view):
    form(view); preview(view); view.dialog.on_all()
    edited = view.state.document.to_dict()
    view.dialog.on_close(); view.dialog.Destroy()
    view.cancel.clear()
    view.dialog.on_undo(); view.dialog.on_cancel(); view.dialog.on_close()
    assert view.state.document.to_dict() == edited and not view.cancel.is_set()


def test_escape_cancels_search_and_closes_only_the_review_dialog(view):
    event = SimpleNamespace(GetKeyCode=lambda: view.wx.WXK_ESCAPE, Skip=lambda: None)
    view.dialog.key(event)
    assert not view.dialog.alive and view.cancel.is_set()
    assert view.state.document.text == view.original
