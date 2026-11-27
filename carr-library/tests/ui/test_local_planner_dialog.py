"""Exercise the real assistant dialog handlers with explicit native-control doubles.

The host, profile store, gateway, ledger, typed plan and execution services are
real. No local model, native event injection, device or NVDA claim is made.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from queue import Queue
import sys
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService
from carr_library.assistant.local_planner import LocalPlanner
from carr_library.progress import ProgressHub
from carr_library.providers import ProfileStore, ProviderProfile
from carr_library.ui import assistant_dialog as module
from test_portable_sync import DeferredExecutor


class Transport:
    def __init__(self):
        self.calls = []
        self.after = lambda: None
        self.proposal = {'schema':'carr-assistant-plan/1', 'summary':'List current libraries',
                        'steps':[{'tool':'library.list','arguments':{}}]}
    def post(self, profile, route, body, headers, **kwargs):
        if kwargs['cancelled']():
            raise InterruptedError('Cancelled before synthetic local-model transmission')
        self.calls.append(json.loads(body))
        self.after()
        return json.dumps({'choices':[{'message':{'content':json.dumps(self.proposal)}}]}).encode()


class NativeDoubles:
    def __init__(self):
        self.queue = Queue()
        self.windows = []
        self.choice_hook = lambda: None
        self.choice_result = 1
        self.choice_index = 0
        owner = self
        class Control:
            def __init__(self, parent=None, *, value='', label='', choices=(), **kwargs):
                self.parent, self.value, self.label = parent, value, label
                self.choices, self.selection = list(choices), -1
                self.name, self.enabled, self.dead = label, True, False
                self.events, self.focused, self.thread = {}, False, get_ident()
                owner.windows.append(self)
            def touch(self):
                assert not self.dead, 'Late callback touched a deleted native control'
                assert self.thread == get_ident(), 'A worker touched a native control'
            def Bind(self, event, callback): self.touch(); self.events[event] = callback
            def Enable(self, value=True): self.touch(); self.enabled = bool(value)
            def Disable(self): self.Enable(False)
            def SetName(self, value): self.touch(); self.name = value
            def SetLabel(self, value): self.touch(); self.label = value
            def GetValue(self): self.touch(); return self.value
            def ChangeValue(self, value): self.touch(); self.value = value
            def SetValue(self, value):
                self.ChangeValue(value)
                if 'text' in self.events: self.events['text'](SimpleNamespace(Skip=lambda:None))
            def GetSelection(self): self.touch(); return self.selection
            def SetSelection(self, value): self.touch(); self.selection = value
            def SetFocus(self): self.touch(); self.focused = True
            def fire(self):
                self.touch()
                assert self.enabled, 'Test attempted to activate a disabled control'
                self.events['button'](None)
            def IsBeingDeleted(self): return self.dead
            def Destroy(self):
                if self.dead: return
                callback = self.events.get('destroy')
                if callback: callback(SimpleNamespace(GetEventObject=lambda:self, Skip=lambda:None))
                self.dead = True
                for item in list(owner.windows):
                    if item.parent is self: item.Destroy()
        class Dialog(Control):
            def __init__(self, parent, *, title, **kwargs):
                super().__init__(parent, **kwargs)
                self.title, self.modal = title, False
            def SetSizer(self, value): self.touch()
            def Layout(self): self.touch()
            def IsModal(self): self.touch(); return self.modal
            def EndModal(self, value): self.touch(); self.modal = False
            def ShowModal(self):
                self.touch(); self.modal = True
                owner.drain()
                return 1
        class ChoiceDialog(Dialog):
            def __init__(self, parent, message, title, choices):
                super().__init__(parent, title=title)
                self.choices = list(choices)
            def ShowModal(self):
                owner.choice_hook()
                self.selection = owner.choice_index
                return owner.choice_result
        class CheckBox(Control):
            def __init__(self, *args, **kwargs): super().__init__(*args, value=False, **kwargs)
        self.module = SimpleNamespace(Dialog=Dialog, SingleChoiceDialog=ChoiceDialog,
            DirDialog=Dialog, TextCtrl=Control, Choice=Control, StaticText=Control,
            Button=Control, CheckBox=CheckBox, BoxSizer=lambda *args:SimpleNamespace(Add=lambda *a,**k:None),
            DEFAULT_DIALOG_STYLE=1, RESIZE_BORDER=2, VERTICAL=4, HORIZONTAL=8,
            LEFT=16, TOP=32, ALL=64, EXPAND=128, TE_MULTILINE=256, TE_READONLY=512,
            ID_OK=1, ID_CANCEL=2, ID_CLOSE=3, EVT_BUTTON='button', EVT_TEXT='text',
            EVT_CHOICE='choice', EVT_CHECKBOX='checkbox', EVT_CLOSE='close', EVT_WINDOW_DESTROY='destroy',
            CallAfter=lambda callback,*args:self.queue.put((callback,args)))
    def drain(self):
        while not self.queue.empty():
            callback,args = self.queue.get_nowait()
            callback(*args)


@pytest.fixture
def host(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path/'library')
    workspace = app.create_workspace('Local planner scope')
    frame = SimpleNamespace(library=app, active=(workspace,None), _closing=False,
        progress_actions=SimpleNamespace(panel=ProgressHub()), unsaved_note='Preserve notebook changes')
    profile = ProfileStore(app.root/'api-connections.json').save(ProviderProfile(
        'local', 'Selected local fixture', 'openai-compatible', 'http://127.0.0.1:8080/v1',
        'synthetic-not-a-real-model', ('llm',), local=True))
    transport = Transport()
    production = SimpleNamespace(voices=lambda:(), plan=lambda *a,**k:None)
    service = AssistantService(app, production=production)
    planner = LocalPlanner(app, assistant=service, transport=transport)
    native = NativeDoubles()
    monkeypatch.setitem(sys.modules, 'wx', native.module)
    executor = DeferredExecutor()
    monkeypatch.setattr(module, 'ThreadPoolExecutor', lambda **kwargs:executor)
    return SimpleNamespace(app=app, frame=frame, workspace=workspace, profile=profile,
        transport=transport, planner=planner, service=service, production=production,
        native=native, executor=executor)


def show(host, exercise):
    module.show_assistant(host.frame, production=host.production, local_planner=host.planner,
                          on_ready=lambda dialog, controls:exercise(dialog,controls))


def prepare(host, controls):
    controls['instruction'].SetValue('List my current libraries.')
    controls['model'].fire()
    assert controls['model_actions'].review is not None
    assert 'nothing has been sent' in controls['review'].value
    assert not controls['run'].enabled and not host.transport.calls
    assert controls['review'].focused


def complete_model(host, controls):
    prepare(host,controls)
    controls['model'].fire()
    assert controls['model_actions'].busy
    assert not controls['instruction'].enabled and not controls['run'].enabled
    host.executor.run()
    host.native.drain()


def test_real_model_dialog_requires_two_separate_approvals_before_tools(host):
    def exercise(dialog,controls):
        complete_model(host,controls)
        assert len(host.transport.calls)==1
        assert controls['actions'].plan is not None and controls['run'].enabled
        assert 'UNTRUSTED LOCAL MODEL PROPOSAL' in controls['review'].value
        assert 'no tools executed' in controls['review'].value
        assert not host.service.jobs()
        assert host.frame.progress_actions.panel.snapshot()[-1].state=='completed'
        controls['run'].fire()
        host.executor.run(); host.native.drain()
        assert host.service.jobs()[0].status=='complete'
        assert len(host.transport.calls)==1 and host.frame.unsaved_note=='Preserve notebook changes'
    show(host,exercise)


@pytest.mark.parametrize('change',['goal','scope','budget','folder'])
def test_edit_after_model_review_removes_permission_to_send(host,change,tmp_path):
    def exercise(dialog,controls):
        prepare(host,controls)
        if change=='scope':
            controls['scope'].SetSelection(0)
            controls['scope'].events['choice'](SimpleNamespace(Skip=lambda:None))
        else:
            key={'goal':'instruction','budget':'budget','folder':'input'}[change]
            value={'goal':'A different goal','budget':'2','folder':str(tmp_path)}[change]
            controls[key].SetValue(value)
        assert controls['model_actions'].review is None and not controls['run'].enabled
        controls['model'].fire()  # This only prepares another exact review.
        assert not host.transport.calls and not host.executor.pending
    show(host,exercise)


def test_silent_native_value_change_is_compared_again_at_send_boundary(host):
    def exercise(dialog,controls):
        prepare(host,controls)
        controls['instruction'].ChangeValue('Different text without a native text event')
        controls['model'].fire()
        assert not host.transport.calls and not host.executor.pending
        assert controls['model_actions'].review is None
        assert 'changed' in controls['review'].value
    show(host,exercise)


def test_changed_parent_inside_profile_chooser_cannot_send_or_accept(host):
    def exercise(dialog,controls):
        host.native.choice_hook = lambda:setattr(host.frame,'active',None)
        controls['model'].fire()
        assert controls['model_actions'].review is None and not host.transport.calls
    show(host,exercise)


def test_late_cancel_discards_model_proposal_but_keeps_completed_ledger(host):
    def exercise(dialog,controls):
        prepare(host,controls); controls['model'].fire()
        host.executor.run()  # The actual response exists; UI delivery is still queued.
        controls['pause'].fire()
        host.native.drain()
        assert controls['actions'].plan is None and not controls['run'].enabled
        assert not controls['model_actions'].busy and not host.service.jobs()
        assert host.frame.progress_actions.panel.snapshot()[-1].state=='cancelled'
        assert len(host.transport.calls)==1
    show(host,exercise)


@pytest.mark.parametrize('change',['close','destroy','scope'])
def test_owned_completion_cannot_touch_closed_or_rebound_native_controls(host,change):
    def exercise(dialog,controls):
        prepare(host,controls); controls['model'].fire()
        host.executor.run()
        if change=='close': controls['close'].fire()
        elif change=='destroy': dialog.Destroy()
        else:
            host.frame.active=None
            for control in controls.values():
                if hasattr(control,'dead'): control.dead=True
        host.native.drain()
        assert not controls['model_actions'].busy
        assert controls['actions'].plan is None and not host.service.jobs()
        assert host.frame.progress_actions.panel.snapshot()[-1].state=='cancelled'
        if change=='scope':
            # The dialog still owns its native controls and can be closed.
            for control in controls.values():
                if hasattr(control,'dead'): control.dead=False
    show(host,exercise)


def test_forced_destroy_waits_for_actual_model_worker_before_terminal_progress(host,monkeypatch):
    entered,release=Event(),Event()
    def held():
        entered.set()
        assert release.wait(5)
    host.transport.after=held
    with ThreadPoolExecutor(max_workers=1) as real:
        monkeypatch.setattr(module,'ThreadPoolExecutor',lambda **kwargs:real)
        def exercise(dialog,controls):
            prepare(host,controls); controls['model'].fire()
            assert entered.wait(5)
            dialog.Destroy()
            assert controls['model_actions'].busy
            try:
                event=host.frame.progress_actions.panel.snapshot()[-1]
                assert event.state=='running' and 'Cancellation requested' in event.phase
            finally:
                release.set()
        show(host,exercise)
    host.native.drain()  # Must only settle withdrawn state, not deleted controls.
    assert host.frame.progress_actions.panel.snapshot()[-1].state=='cancelled'
    assert not host.service.jobs()


def test_existing_deterministic_preview_and_run_never_calls_model(host):
    def exercise(dialog,controls):
        controls['instruction'].SetValue('List libraries')
        controls['preview'].fire()
        assert controls['actions'].plan and controls['run'].enabled
        controls['run'].fire()
        host.executor.run(); host.native.drain()
        assert controls['actions'].service.jobs()[0].status=='complete'
        assert not host.transport.calls
    show(host,exercise)


def test_model_response_cannot_enable_run_for_outside_scope_tool(host):
    host.transport.proposal['steps']=[{'tool':'library.create','arguments':{'name':'Not permitted'}}]
    def exercise(dialog,controls):
        complete_model(host,controls)
        assert controls['actions'].plan is None and not controls['run'].enabled
        assert not host.service.jobs()
        assert [row.name for row in host.app.workspaces()]==['Local planner scope']
        assert host.frame.progress_actions.panel.snapshot()[-1].state=='failed'
    show(host,exercise)


def test_rejected_dialog_executor_consumes_review_without_sending(host):
    executor=ThreadPoolExecutor(max_workers=1); executor.shutdown(wait=True)
    host.executor=executor
    # The fixture constructor captures host.executor through this replacement.
    original=module.ThreadPoolExecutor
    module.ThreadPoolExecutor=lambda **kwargs:executor
    try:
        def exercise(dialog,controls):
            prepare(host,controls); controls['model'].fire()
            assert not controls['model_actions'].busy and controls['instruction'].enabled
            assert controls['model_actions'].review is None and not controls['run'].enabled
            assert not host.transport.calls
        show(host,exercise)
    finally:
        module.ThreadPoolExecutor=original


def test_existing_job_close_keeps_progress_owned_until_real_worker_finishes(host,monkeypatch):
    entered,release=Event(),Event()
    with ThreadPoolExecutor(max_workers=1) as real:
        monkeypatch.setattr(module,'ThreadPoolExecutor',lambda **kwargs:real)
        def exercise(dialog,controls):
            controls['scope'].SetSelection(0)
            controls['instruction'].SetValue('Create library "Do not create after closing"')
            controls['preview'].fire()
            execute=controls['actions'].service._execute
            def held(*args,**kwargs):
                entered.set()
                assert release.wait(5)
                return execute(*args,**kwargs)
            controls['actions'].service._execute=held
            controls['run'].fire()
            assert entered.wait(5)
            controls['close'].fire()
            try:
                assert controls['actions'].busy
                event=host.frame.progress_actions.panel.snapshot()[-1]
                assert event.state=='running' and 'Cancellation requested' in event.phase
            finally:
                release.set()
        show(host,exercise)
    host.native.drain()
    assert host.frame.progress_actions.panel.snapshot()[-1].state=='cancelled'
    assert [row.name for row in host.app.workspaces()]==['Local planner scope']
    assert host.service.jobs()[0].status=='paused'


def test_new_review_inside_voice_chooser_is_not_overwritten_by_stale_model_error(host,tmp_path):
    host.transport.proposal['steps']=[{'tool':'audio.produce','arguments':{'text':'Proposed narration'}}]
    host.production.voices=lambda:(SimpleNamespace(available=True,name='Synthetic voice',languages=('en',),id='fixture'),)
    def exercise(dialog,controls):
        controls['output'].SetValue(str(tmp_path))
        prepare(host,controls)
        controls['model'].fire()
        def replace_review():
            controls['instruction'].SetValue('List libraries')
            controls['preview'].fire()
        host.native.choice_hook=replace_review
        host.executor.run(); host.native.drain()
        assert controls['actions'].plan.steps[0].tool=='library.list'
        assert controls['run'].enabled and 'library.list' in controls['review'].value
        assert 'permissions changed' not in controls['review'].value
        assert not host.service.jobs()
    show(host,exercise)
