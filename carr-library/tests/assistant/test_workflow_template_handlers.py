"""Actual workflow controllers with deterministic native widget scheduling seams."""
from concurrent.futures import Future
import json
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService, Plan, Scope, Step
from carr_library.ui import assistant_templates


class Widget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.value, self.label, self.choices = value, label, list(choices)
        self.enabled, self.name, self.selections = True, '', ()
        self.bindings, self.focused = {}, False
    def SetName(self, value): self.name = value
    def GetName(self): return self.name
    def GetValue(self): return self.value
    def ChangeValue(self, value): self.value = value
    def SetValue(self, value): self.value = value
    def Set(self, values): self.choices, self.selections = list(values), ()
    def GetSelections(self): return self.selections
    def Enable(self, value=True): self.enabled = value
    def Disable(self): self.enabled = False
    def IsEnabled(self): return self.enabled
    def SetFocus(self): self.focused = True
    def Bind(self, event, callback, **kwargs): self.bindings[event] = callback
    def SetSizer(self, sizer): pass
    def Wrap(self, width): pass
    def IsModal(self): return False
    def Hide(self): self.hidden = True
    def Destroy(self): self.destroyed = True


class Sizer:
    def __init__(self, *args, **kwargs): pass
    def Add(self, *args, **kwargs): pass


class Executor:
    def __init__(self):
        self.pending, self.shutdowns = [], []
    def submit(self, callback, *args, **kwargs):
        future = Future()
        self.pending.append((future, callback, args, kwargs))
        return future
    def complete(self):
        future, callback, args, kwargs = self.pending.pop(0)
        if future.set_running_or_notify_cancel():
            try: future.set_result(callback(*args, **kwargs))
            except BaseException as exc: future.set_exception(exc)
    def shutdown(self, **kwargs): self.shutdowns.append(kwargs)


@pytest.fixture
def ui(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service = AssistantService(app)
    plan = Plan(Scope(), (Step('library.list'),))
    executor, deliveries, active = Executor(), [], {'current': True}
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
        'ALL', 'EXPAND', 'LB_EXTENDED', 'TE_MULTILINE', 'TE_READONLY', 'EVT_BUTTON',
        'EVT_CLOSE', 'ID_OK', 'ID_CLOSE', 'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION',
        'YES', 'FD_OPEN', 'FD_FILE_MUST_EXIST', 'FD_SAVE')
    wx = SimpleNamespace(**{key: index + 1 for index, key in enumerate(constants)})
    for key in ('Dialog', 'StaticText', 'TextCtrl', 'Button', 'ListBox'):
        setattr(wx, key, Widget)
    wx.BoxSizer = Sizer
    wx.CallAfter = lambda callback, *args: deliveries.append((callback, args))
    wx.before_modal_return = lambda: None
    wx.next_name, wx.next_path = 'Named workflow', str(tmp_path / 'template.json')
    class Picker(Widget):
        def ShowModal(self):
            wx.before_modal_return()
            return wx.ID_OK
        def GetValue(self): return wx.next_name
        def GetPath(self): return wx.next_path
    wx.TextEntryDialog = wx.FileDialog = Picker
    wx.MessageBox = lambda *args, **kwargs: wx.YES
    dialog = assistant_templates.create_templates_dialog(None, service, executor=executor,
        is_current=lambda: active['current'], current_plan=plan, wx_module=wx)
    def drain():
        while deliveries:
            callback, args = deliveries.pop(0)
            callback(*args)
    def finish():
        executor.complete()
        drain()
    e = SimpleNamespace(app=app, service=service, store=service.workflow_templates(),
        plan=plan, executor=executor, deliveries=deliveries, active=active, wx=wx,
        dialog=dialog, controls=dialog.template_controls, handlers=dialog.template_handlers,
        finish=finish, drain=drain)
    try:
        yield e
    finally:
        dialog.template_handlers['close']()


def test_inventory_uses_existing_worker_and_load_never_submits_a_job(ui):
    e = ui
    assert len(e.executor.pending) == 1 and not e.store.root.exists()
    assert e.controls['buttons']['close'].IsEnabled()
    assert not e.controls['buttons']['load'].IsEnabled()
    saved = e.store.save('Saved batch', e.plan)
    e.finish()
    assert e.controls['rows'].choices == ['Saved batch; revision 1; 1 steps']
    e.controls['rows'].selections = (0,)
    e.handlers['load']()
    assert e.dialog.template_result is None and e.service.jobs() == ()
    e.finish()
    assert json.loads(e.dialog.template_result) == saved.draft
    assert e.service.jobs() == () and not e.executor.shutdowns


def test_save_is_background_and_preserves_only_reviewed_steps(ui):
    e = ui; e.finish()
    e.handlers['save']()
    assert not e.store.root.exists() and e.service.jobs() == ()
    e.finish()  # Atomic save; refresh now borrows the same executor.
    assert len(e.store.list()) == 1 and len(e.executor.pending) == 1
    e.finish()
    saved = e.store.list()[0]
    assert saved.name == 'Named workflow' and 'scope' not in saved.to_dict()['draft']
    assert len(e.controls['rows'].choices) == 1 and e.service.jobs() == ()


def test_selected_native_list_order_is_the_explicit_batch_order(ui):
    e = ui
    e.store.save('A first', e.plan)
    e.store.save('B second', Plan(Scope(), (Step('library.list'), Step('library.list'))))
    e.finish()
    e.controls['rows'].selections = (1, 0)
    e.handlers['load'](); e.finish()
    draft = json.loads(e.dialog.template_result)
    assert draft['summary'] == 'A first\nB second'
    assert len(draft['steps']) == 3 and 'scope' not in draft
    assert e.service.jobs() == ()


def test_changed_saved_record_is_rejected_when_queued_load_executes(ui):
    e = ui
    old = e.store.save('Before', e.plan)
    e.finish()
    e.controls['rows'].selections = (0,)
    e.handlers['load']()
    e.store.save('Changed elsewhere', old.draft, previous=old)
    e.finish()
    assert e.dialog.template_result is None
    assert 'changed' in e.controls['status'].GetValue()
    assert e.service.jobs() == ()


@pytest.mark.parametrize('change', ['scope', 'same_root_app', 'root', 'close'])
def test_late_queued_save_cannot_cross_changed_or_closed_owner(ui, change, tmp_path):
    e = ui; e.finish()
    e.handlers['save']()
    before = e.controls['status'].GetValue()
    if change == 'scope': e.active['current'] = False
    elif change == 'same_root_app': e.service.app = LibraryApp(e.app.root)
    elif change == 'root': e.service.app.root = tmp_path / 'other-root'
    else: e.handlers['close']()
    e.finish()
    assert not e.store.root.exists()
    assert e.controls['status'].GetValue() == before
    assert not e.executor.shutdowns


def test_close_after_actual_save_before_ui_delivery_preserves_saved_record_silently(ui):
    e = ui; e.finish()
    e.handlers['save']()
    e.executor.complete()
    saved = e.store.list()
    assert len(saved) == 1 and e.deliveries
    before = e.controls['status'].GetValue()
    e.handlers['close']()
    e.drain()
    assert e.store.list() == saved and e.controls['status'].GetValue() == before
    assert not e.executor.pending and not e.executor.shutdowns


@pytest.mark.parametrize('operation', ['save', 'rename', 'import'])
def test_modal_return_rechecks_owner_before_any_queued_mutation(ui, operation):
    e = ui
    saved = e.store.save('Existing', e.plan)
    e.finish()
    e.controls['rows'].selections = (0,)
    before = e.store.get(saved.id)
    e.wx.before_modal_return = lambda: e.active.update(current=False)
    e.handlers[operation]()
    assert not e.executor.pending
    assert e.store.get(saved.id) == before and e.service.jobs() == ()


def test_remove_confirmation_rechecks_owner_and_keeps_original(ui):
    e = ui
    saved = e.store.save('Existing', e.plan)
    e.finish()
    e.controls['rows'].selections = (0,)
    def confirm(*args, **kwargs):
        e.active['current'] = False
        return e.wx.YES
    e.wx.MessageBox = confirm
    e.handlers['remove']()
    assert not e.executor.pending and e.store.get(saved.id) == saved


def test_export_confirmation_rechecks_owner_before_rendering_or_writing(ui, monkeypatch):
    e = ui
    saved = e.store.save('Existing', e.plan)
    e.finish()
    e.controls['rows'].selections = (0,)
    def confirm(*args, **kwargs):
        e.active['current'] = False
        return object()
    monkeypatch.setattr(assistant_templates, 'confirm_export_destination', confirm)
    monkeypatch.setattr(assistant_templates, 'write_export',
                        lambda *a, **k: pytest.fail('wrote after scope changed'))
    e.handlers['export']()
    assert not e.executor.pending and e.store.get(saved.id) == saved


def test_submit_failure_reenables_controls_and_reports_actual_error(ui, monkeypatch):
    e = ui; e.finish()
    def failed(*args, **kwargs):
        raise RuntimeError('worker admission failed')
    monkeypatch.setattr(e.executor, 'submit', failed)
    e.handlers['refresh']()
    assert 'worker admission failed' in e.controls['status'].GetValue()
    assert e.controls['buttons']['refresh'].IsEnabled()
    assert e.controls['buttons']['close'].IsEnabled()


def test_existing_saved_job_can_be_saved_as_inert_workflow_on_the_same_worker(ui):
    e = ui; e.finish(); e.handlers['close']()
    identity = e.service.submit(e.plan)
    record = e.service.store.load(identity)
    record['data_dir'] = str(e.app.root.parent / 'old-owner-directory')
    e.service.store.save(record)
    original = e.service.store.path(identity).read_bytes()
    picker = assistant_templates.create_templates_dialog(None, e.service, executor=e.executor,
        is_current=lambda: True, current_job_id=identity, wx_module=e.wx)
    try:
        e.finish()
        assert picker.template_controls['buttons']['save'].IsEnabled()
        picker.template_handlers['save']()
        assert e.store.list() == ()
        e.finish(); e.finish()
        saved = e.store.list()
        assert len(saved) == 1 and 'scope' not in saved[0].draft
        assert e.service.store.path(identity).read_bytes() == original
        with pytest.raises(PermissionError):
            e.service.get(identity)
    finally:
        picker.template_handlers['close']()


def test_import_chooses_a_new_local_name_after_bounded_read_before_save(ui):
    e = ui
    original = e.store.save('Existing exported name', e.plan)
    from pathlib import Path
    Path(e.wx.next_path).write_text(e.store.export_json(original), encoding='utf-8')
    e.finish()
    e.wx.next_name = 'Chosen import name'
    e.handlers['import']()
    assert e.store.list() == (original,)
    e.finish()  # Read/validate, native name dialog, then queued save.
    assert e.store.list() == (original,) and len(e.executor.pending) == 1
    e.finish(); e.finish()
    values = e.store.list()
    assert {value.name for value in values} == {'Existing exported name', 'Chosen import name'}
    assert len({value.id for value in values}) == 2 and e.service.jobs() == ()


def test_real_thread_start_failure_does_not_leave_a_queued_save_executable(ui, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    e = ui; e.finish()
    original_start, failures = threading.Thread.start, []
    def fail_first(thread, *args, **kwargs):
        if thread.name.startswith('workflow-submit-test') and not failures:
            failures.append(thread)
            raise RuntimeError('actual worker start rejected once')
        return original_start(thread, *args, **kwargs)
    monkeypatch.setattr(threading.Thread, 'start', fail_first)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix='workflow-submit-test') as real:
        monkeypatch.setattr(e.executor, 'submit', real.submit)
        e.handlers['save']()
        assert 'actual worker start rejected once' in e.controls['status'].GetValue()
        assert not e.store.root.exists()
        # Starting a later real worker drains the previously enqueued WorkItem.
        # The failed attempt's admission token must still reject that mutation.
        e.handlers['refresh']()
    e.drain()
    assert len(failures) == 1 and not e.store.root.exists()
    assert e.service.jobs() == () and e.controls['buttons']['refresh'].IsEnabled()
