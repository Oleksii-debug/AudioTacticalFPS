"""Real executor/cancellation/source lifetimes; controlled native wx and Word."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Queue, Empty
import sys
import threading
import time
from types import ModuleType, SimpleNamespace as N

import pytest

from carr_library.application import LibraryApp
from carr_library.academic_integration.formatting import CitationCluster, CitationRequest
from carr_library.academic_integration.word_com import WordCopy, WordResult
from carr_library.ui.academic_library_dialog import AcademicController
from carr_library.ui.citation_formatting import CitationFormattingController
from carr_library.ui.word_citations_dialog import create_word_citations_dialog, _host


class Control:
    counter = 100
    gui_thread = None

    def __init__(self, *args, **kwargs):
        Control.counter += 1
        self.identity = Control.counter
        self.parent = args[0] if args and isinstance(args[0], Control) else None
        self.children = []
        if self.parent is not None:
            self.parent.children.append(self)
        self.value = kwargs.get('value', '')
        self.choices = list(kwargs.get('choices', ()))
        self.selection, self.enabled, self.result = -1, True, None
        self.deleted = False
        self.handlers = {}

    def native(self):
        assert threading.get_ident() == Control.gui_thread, 'Worker touched a native widget.'
        assert not self.deleted, 'A late callback touched a deleted native widget.'

    def Bind(self, event, callback, **kwargs):
        self.native()
        self.handlers[event] = callback

    def GetId(self):
        self.native()
        return self.identity

    def IsBeingDeleted(self):
        assert threading.get_ident() == Control.gui_thread
        return self.deleted

    def Destroy(self):
        self.native()
        self.deleted = True
        for child in self.children:
            if not child.deleted:
                child.Destroy()
        if hasattr(self, 'on_destroy'):
            self.on_destroy(N(GetId=lambda: self.identity, Skip=lambda: None))
        return True

    def SetName(self, value): self.native(); self.name = value
    def SetValue(self, value): self.native(); self.value = value
    def GetValue(self): self.native(); return self.value
    def SetSelection(self, value): self.native(); self.selection = value
    def GetSelection(self): self.native(); return self.selection
    def GetCount(self): self.native(); return len(self.choices)
    def GetString(self, index): self.native(); return self.choices[index]
    def Append(self, value): self.native(); self.choices.append(value)
    def Clear(self): self.native(); self.choices.clear()
    def Enable(self, value): self.native(); self.enabled = value
    def SetFocus(self): self.native()
    def Add(self, *args, **kwargs): self.native()
    def Wrap(self, *args): self.native()
    def SetSizer(self, *args): self.native()
    def SetEscapeId(self, *args): self.native()
    def SetInsertionPoint(self, *args): self.native()
    def IsModal(self): self.native(); return True
    def EndModal(self, value): self.native(); self.result = value
    def Hide(self): self.native(); self.result = 'hidden'


@pytest.fixture
def form(tmp_path, monkeypatch):
    Control.gui_thread = threading.get_ident()
    wx = ModuleType('wx')
    for name in ('Dialog', 'BoxSizer', 'StaticText', 'Choice', 'Button', 'TextCtrl', 'CheckBox'):
        setattr(wx, name, Control)
    for index, name in enumerate(('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL',
            'HORIZONTAL', 'EXPAND', 'ALL', 'TE_MULTILINE', 'TE_READONLY', 'ALIGN_RIGHT',
            'ID_CANCEL', 'ID_OK', 'EVT_BUTTON', 'EVT_CLOSE', 'EVT_WINDOW_DESTROY',
            'FD_SAVE', 'EVT_TEXT', 'EVT_CHOICE')):
        setattr(wx, name, 1 << index)
    queued = Queue()
    wx.CallAfter = lambda callback, *args: queued.put((callback, args))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    library = LibraryApp(tmp_path / 'library')
    workspace = library.create_workspace('Word UI source scope')
    academic = AcademicController(library, workspace)
    first = academic.create(dict(title='Real source metadata', year='2024', authors=('Jane Reader',)))
    second = academic.create(dict(title='Another source', year='2025'))
    controller = CitationFormattingController(library, workspace)
    snapshot = controller.load()
    result = controller.render(snapshot, citations=(
        CitationCluster((CitationRequest(first.id),), 'initial-citation'),))
    executor = ThreadPoolExecutor(max_workers=1)
    host = Control()
    host.library, host.cancel_event = library, threading.Event()
    host.active = (workspace, 'Word UI source scope')
    host.working = host.busy = host._closing = False
    host.calls, host.future = 0, None
    parent = Control(host)

    def run_job(title, operation, done=None, failed=None, **kwargs):
        if host._closing or host.working:
            return
        host.working = host.busy = True
        host.cancel_event.clear()
        host.calls += 1
        host.future = executor.submit(operation)
        def complete(future):
            if host._closing:
                return
            def deliver():
                if host._closing:
                    return
                host.working = host.busy = False
                try:
                    value = future.result()
                except Exception as error:
                    if failed:
                        failed(error)
                else:
                    if done:
                        done(value)
            wx.CallAfter(deliver)
        host.future.add_done_callback(complete)
        return None  # Actual Desktop admission contract.

    host.run_job = run_job
    copy = WordCopy(tmp_path / 'copy.docx', tmp_path / 'original.docx', '0' * 64,
                    ('initial-citation',), (first.id,))
    outcome = WordResult(copy.path, ('initial-citation',), (first.id,), 'in-text', True)
    calls = []
    bridge = N(connected_copy=copy, recovery_required=False,
               change=lambda *args, **kwargs: calls.append(kwargs) or outcome,
               inspect=lambda *args, **kwargs: outcome,
               completed_result=lambda selected: outcome)
    dialog = create_word_citations_dialog(parent, controller, snapshot, result, host=host, bridge=bridge)
    dialog.copy = copy
    selected = next(index for index, row in enumerate(snapshot.references) if row['id'] == first.id)
    dialog.reference.SetSelection(selected)
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

    yield N(dialog=dialog, host=host, parent=parent, controller=controller, snapshot=snapshot,
        first=first, second=second, copy=copy, result=outcome, bridge=bridge, calls=calls,
        drain=drain, settled=settled, wx=wx, executor=executor, queued=queued, tmp_path=tmp_path)
    dialog.cancelled.set()
    host.cancel_event.set()
    executor.shutdown(wait=True, cancel_futures=True)
    drain()
    if not dialog.deleted:
        dialog.Destroy()
    assert library.close_search()


def test_existing_desktop_executor_owns_typed_action_and_none_is_successful_admission(form):
    form.dialog.change(insert=True)
    form.settled()
    assert form.host.calls == 1 and len(form.calls) == 1
    assert form.calls[0]['items'] == (CitationRequest(form.first.id),)
    assert form.calls[0]['insert'] is True
    assert form.dialog.copy is form.copy and form.dialog.insert_button.enabled
    assert 'Original preserved' in form.dialog.status.value
    assert form.dialog.occurrence.choices == ['initial-citation']


def test_locator_requires_explicit_review_and_captured_typed_request_is_immutable(form):
    ui = form.dialog
    ui.locator.SetValue('12')
    ui.change(insert=True)
    assert not form.calls and form.host.calls == 0
    assert 'Check this exact locator' in ui.status.value
    ui.locator_reviewed.SetValue(True)
    entered, release = threading.Event(), threading.Event()
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        form.calls.append(kwargs)
        return form.result
    form.bridge.change = held
    ui.change(insert=True)
    assert entered.wait(3)
    ui.locator.SetValue('99')
    ui.reference.SetSelection(next(index for index, row in enumerate(form.snapshot.references)
                                  if row['id'] == form.second.id))
    release.set()
    form.settled()
    assert form.calls[0]['items'] == (CitationRequest(form.first.id, '12', locator_verified=True),)


@pytest.mark.parametrize('which,event_name', [
    ('locator', 'EVT_TEXT'), ('reference', 'EVT_CHOICE'), ('label', 'EVT_CHOICE'),
])
def test_source_locator_or_kind_change_revokes_previous_checkbox_authority(form, which, event_name):
    ui = form.dialog
    ui.locator.SetValue('12')
    ui.locator_reviewed.SetValue(True)
    getattr(ui, which).handlers[getattr(form.wx, event_name)](N(Skip=lambda: None))
    assert ui.locator_reviewed.GetValue() is False
    ui.change(insert=True)
    assert not form.calls and form.host.calls == 0


@pytest.mark.parametrize('blocked', ['working', 'closing', 'library'])
def test_busy_or_changed_desktop_never_submits(form, blocked):
    if blocked == 'working':
        form.host.working = True
    elif blocked == 'closing':
        form.host._closing = True
    else:
        form.host.library = object()
    form.dialog.change(insert=True)
    assert form.host.calls == 0 and form.dialog.pending is None and not form.calls


def test_close_retains_actual_call_until_worker_cancellation_even_without_host_callback(form):
    entered, release = threading.Event(), threading.Event()
    def operation(*args, cancelled, **kwargs):
        entered.set()
        assert release.wait(3)
        assert cancelled()
        raise InterruptedError('Actual worker observed close')
    form.bridge.change = operation
    form.dialog.change(insert=True)
    assert entered.wait(3)
    form.host._closing = True
    vetoed = []
    form.dialog.on_close(N(Veto=lambda: vetoed.append(True)))
    assert form.dialog.pending is not None and form.dialog.result is None and vetoed == [True]
    release.set()
    form.settled()
    assert form.dialog.result == form.wx.ID_CANCEL and form.host.calls == 1


def test_late_close_keeps_completed_result_and_never_starts_another_operation(form):
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


@pytest.mark.parametrize('kind', ['direct', 'parent', 'rewrapped-owner'])
def test_actual_pending_worker_retains_python_owner_after_native_death_and_replayed_delivery(form, kind):
    entered, release = threading.Event(), threading.Event()
    def held(*args, cancelled, **kwargs):
        entered.set()
        assert release.wait(3)
        assert cancelled()
        return form.result
    form.bridge.change = held
    form.dialog.change(insert=True)
    token = form.dialog.pending
    assert entered.wait(3)
    unrelated = threading.Event()
    form.host.cancel_event = unrelated
    if kind == 'direct':
        form.dialog.Destroy()
    elif kind == 'parent':
        form.parent.Destroy()
    else:
        form.dialog.on_destroy(N(GetId=lambda: form.dialog._native_id,
            GetEventObject=lambda: pytest.fail('Rewrapped identity must use cached native ID.'),
            Skip=lambda: None))
        form.dialog.deleted = True
    assert form.dialog.pending is token and not unrelated.is_set()
    release.set()
    form.settled()
    form.dialog.settle(token, form.result, None)
    assert not form.dialog.alive and form.host.calls == 1


def test_child_destroy_event_does_not_cancel_owned_action_or_foreign_host_job(form):
    form.dialog.on_destroy(N(GetId=lambda: form.dialog._native_id + 999, Skip=lambda: None))
    assert form.dialog.alive and not form.dialog.cancelled.is_set()
    assert not form.host.cancel_event.is_set()


@pytest.mark.parametrize('mode', ['raises', 'declines', 'synchronous-failure'])
def test_real_admission_contract_restores_controls_and_allows_explicit_retry(form, mode):
    actual = form.host.run_job
    if mode == 'raises':
        def decline(*args, **kwargs):
            raise RuntimeError('Executor refused')
    elif mode == 'declines':
        def decline(*args, **kwargs):
            return None
    else:
        def decline(*args, failed=None, **kwargs):
            failed(RuntimeError('Executor refused'))
            return None
    form.host.run_job = decline
    form.dialog.change(insert=True)
    assert form.dialog.pending is None and form.dialog.insert_button.enabled and not form.calls
    form.host.run_job = actual
    form.dialog.change(insert=True)
    form.settled()
    assert form.host.calls == 1 and len(form.calls) == 1


def test_accepted_worker_then_admission_exception_keeps_owner_until_actual_return(form):
    actual = form.host.run_job
    entered, release = threading.Event(), threading.Event()
    def held(*args, cancelled, **kwargs):
        entered.set()
        assert release.wait(3)
        assert cancelled()
        return form.result
    form.bridge.change = held
    def admitted(*args, **kwargs):
        actual(*args, **kwargs)
        assert entered.wait(3)
        raise RuntimeError('Late observer failure')
    form.host.run_job = admitted
    form.dialog.change(insert=True)
    assert form.dialog.pending is not None and form.host.calls == 1
    form.dialog.change(insert=True)
    assert form.host.calls == 1
    release.set()
    form.settled()
    assert form.dialog.copy is form.copy


def test_uncertain_result_disables_mutations_but_explicit_inspect_can_restore_them(form):
    def uncertain(*args, **kwargs):
        form.bridge.recovery_required = True
        raise RuntimeError('Unknown external result; inspect')
    form.bridge.change = uncertain
    form.dialog.change(insert=True)
    form.settled()
    assert not form.dialog.insert_button.enabled and form.dialog.inspect_button.enabled
    def inspect(*args, **kwargs):
        form.bridge.recovery_required = False
        return form.result
    form.bridge.inspect = inspect
    form.dialog.inspect()
    form.settled()
    assert form.dialog.insert_button.enabled and form.host.calls == 2


def test_dead_file_picker_never_reads_path_or_destroys_deleted_peer(form):
    form.dialog.copy = None
    form.bridge.connected_copy = None
    class Picker(Control):
        def ShowModal(self):
            form.dialog.Destroy()
            return form.wx.ID_OK
        def GetPath(self):
            pytest.fail('A dead picker cannot provide export authority.')
    form.wx.FileDialog = Picker
    form.dialog.on_connect()
    assert form.dialog.deleted and form.host.calls == 0


def test_owned_ancestor_host_is_reused_and_cycles_are_refused():
    host = N(run_job=lambda *args, **kwargs: None)
    child = N(parent=N(parent=host))
    assert _host(child) is host
    child.parent = child
    with pytest.raises(RuntimeError, match='current library'):
        _host(child)


@pytest.mark.parametrize('scope', [None, ('foreign-workspace', 'Other workspace')])
def test_rebound_workspace_blocks_admission_with_same_library_object(form, scope):
    form.host.active = scope
    form.dialog.change(insert=True)
    assert form.host.calls == 0 and form.dialog.pending is None and not form.calls


def test_actual_pending_worker_observes_workspace_rebound_and_drops_late_native_delivery(form):
    entered, release = threading.Event(), threading.Event()
    def held(*args, cancelled, **kwargs):
        entered.set()
        assert release.wait(3)
        assert cancelled()
        raise InterruptedError('The selected workspace changed.')
    form.bridge.change = held
    form.dialog.change(insert=True)
    assert entered.wait(3)
    status = form.dialog.status.value
    form.host.active = ('foreign-workspace', 'Other workspace')
    release.set()
    form.settled()
    assert form.dialog.status.value == status and form.dialog.result == form.wx.ID_CANCEL
    assert form.host.calls == 1 and not form.calls


@pytest.mark.parametrize('first', ['direct', 'native-event'])
def test_repeated_destroy_uses_cached_native_identity_without_dead_peer_access(form, first):
    ui = form.dialog
    if first == 'direct':
        assert ui.Destroy() is True
    else:
        ui.on_destroy(N(GetId=lambda: ui._native_id, Skip=lambda: None))
        ui.deleted = True
    assert ui.Destroy() is False
    assert ui.Destroy() is False
    assert not ui.alive and ui.deleted


@pytest.mark.parametrize('exception', [KeyboardInterrupt, SystemExit])
def test_base_exception_before_admission_settles_controls_then_propagates(form, exception):
    def interrupted(*args, **kwargs):
        raise exception('Before admission')
    form.host.run_job = interrupted
    with pytest.raises(exception, match='Before admission'):
        form.dialog.change(insert=True)
    assert form.dialog.pending is None and form.dialog.insert_button.enabled
    assert form.host.calls == 0 and not form.calls


@pytest.mark.parametrize('exception', [KeyboardInterrupt, SystemExit])
def test_base_exception_after_real_admission_retains_worker_before_propagating(form, exception):
    actual = form.host.run_job
    entered, release = threading.Event(), threading.Event()
    def held(*args, cancelled, **kwargs):
        entered.set()
        assert release.wait(3)
        assert cancelled()
        return form.result
    form.bridge.change = held
    def interrupted(*args, **kwargs):
        actual(*args, **kwargs)
        assert entered.wait(3)
        raise exception('After admission')
    form.host.run_job = interrupted
    try:
        with pytest.raises(exception, match='After admission'):
            form.dialog.change(insert=True)
        assert form.dialog.pending is not None and form.host.calls == 1
        form.dialog.change(insert=True)
        assert form.host.calls == 1
    finally:
        release.set()
    form.settled()
    assert form.dialog.copy is form.copy and form.dialog.pending is None
