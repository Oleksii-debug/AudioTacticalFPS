"""Actual assistant handlers with widget/scheduler stand-ins; no native or LLM claim."""
from concurrent.futures import Future
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import ProfileStore, ProviderProfile
from carr_library.assistant.providers import Gateway
from carr_library.ui import assistant_dialog


class Widget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.value, self.label, self.choices = value, label, list(choices)
        self.enabled, self.selection, self.name = True, -1, ''
        self.bindings, self.focused = {}, False
        self.identity = id(self)
    def GetId(self): return self.identity
    def SetName(self, value): self.name = value
    def GetName(self): return self.name
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def ChangeValue(self, value): self.value = value
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def SetLabel(self, value): self.label = value
    def Enable(self, value=True): self.enabled = value
    def Disable(self): self.enabled = False
    def IsEnabled(self): return self.enabled
    def SetFocus(self): self.focused = True
    def Bind(self, event, callback, **kwargs): self.bindings[event] = callback
    def SetSizer(self, sizer): pass
    def Wrap(self, width): pass
    def EndModal(self, code): self.modal_result = code
    def IsModal(self): return False
    def Hide(self): self.hidden = True
    def Destroy(self): self.destroyed = True


class Sizer:
    def __init__(self, *args, **kwargs): pass
    def Add(self, *args, **kwargs): pass


class ControlledExecutor:
    """Hold work and completion independently to reproduce edited-input races."""
    def __init__(self, **kwargs): self.pending, self.shutdowns = [], []
    def submit(self, callback, *args, **kwargs):
        future = Future()
        future.set_running_or_notify_cancel()
        self.pending.append((future, callback, args, kwargs))
        return future
    def complete(self):
        future, callback, args, kwargs = self.pending.pop(0)
        try:
            future.set_result(callback(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
    def shutdown(self, **kwargs): self.shutdowns.append(kwargs)


@pytest.fixture
def native_handlers(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    profile = ProviderProfile('chosen', 'Chosen profile', 'openai-compatible',
        'http://127.0.0.1:9/v1', 'synthetic-protocol-model', local=True, enabled=True)
    store = ProfileStore(app.root / 'state' / 'providers'); store.save(profile)
    calls, queue = [], []
    def synthetic_request(self, selected, operation, payload, **kwargs):
        calls.append((selected, operation, payload, kwargs))
        return {'text': json.dumps({'summary': 'Synthetic proposal; no actual model',
            'steps': [{'tool': 'library.create', 'arguments': {'name': 'Reviewed library'}}]})}
    monkeypatch.setattr(Gateway, 'request', synthetic_request)
    executor = ControlledExecutor()
    monkeypatch.setattr(assistant_dialog, 'ThreadPoolExecutor', lambda **kwargs: executor)
    constants = ('DEFAULT_DIALOG_STYLE','RESIZE_BORDER','VERTICAL','HORIZONTAL','LEFT','TOP',
        'ALL','EXPAND','TE_MULTILINE','TE_READONLY','EVT_BUTTON','EVT_TEXT','EVT_CHOICE',
        'EVT_CHECKBOX','EVT_CLOSE','EVT_WINDOW_DESTROY','ID_OK','ID_CLOSE')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(constants)})
    for name in ('Dialog','StaticText','TextCtrl','Button','Choice'):
        setattr(wx, name, Widget)
    wx.CheckBox = lambda parent, **kwargs: Widget(parent, value=False, **kwargs)
    wx.BoxSizer = Sizer
    wx.CallAfter = lambda callback, *args: queue.append((callback, args))
    frame = SimpleNamespace(library=app, active=None)
    dialog = assistant_dialog.create_assistant_dialog(frame, wx_module=wx)
    def drain():
        while queue:
            callback, args = queue.pop(0); callback(*args)
    environment = SimpleNamespace(app=app, store=store, profile=profile, frame=frame,
        dialog=dialog, controls=dialog.assistant_controls, handlers=dialog.assistant_handlers,
        executor=executor, calls=calls, drain=drain, wx=wx)
    try:
        yield environment
    finally:
        dialog.assistant_handlers['close']()


def select_model(environment):
    environment.controls['planning_source'].SetSelection(1)
    environment.controls['instruction'].ChangeValue('Please create one named library.')


def test_default_guided_preview_never_calls_a_model(native_handlers):
    e = native_handlers
    assert e.controls['planning_source'].GetSelection() == 0
    e.handlers['preview']()
    assert not e.executor.pending and not e.calls
    assert e.controls['buttons']['run'].IsEnabled()
    assert e.dialog.assistant_service.jobs() == () and e.app.workspaces() == ()


def test_model_proposal_is_background_review_then_separate_run(native_handlers):
    e = native_handlers; select_model(e)
    e.handlers['preview']()
    assert len(e.executor.pending) == 1 and not e.calls
    assert not e.controls['buttons']['run'].IsEnabled()
    assert e.controls['instruction'].IsEnabled()
    assert 'Cancel' in e.controls['buttons']['pause'].label
    e.executor.complete(); e.drain()
    assert len(e.calls) == 1 and e.controls['buttons']['run'].IsEnabled()
    assert 'requested model synthetic-protocol-model' in e.controls['review'].GetValue()
    assert e.dialog.assistant_service.jobs() == () and e.app.workspaces() == ()
    e.handlers['run']()
    assert not e.controls['planning_source'].IsEnabled()
    assert e.app.workspaces() == () and len(e.dialog.assistant_service.jobs()) == 1
    e.executor.complete(); e.drain()
    assert [row.name for row in e.app.workspaces()] == ['Reviewed library']
    assert 'complete: 1 of 1' in e.controls['review'].GetValue()
    assert len(e.calls) == 1


@pytest.mark.parametrize('field', ['instruction', 'input', 'output', 'full_backup', 'planning_source'])
def test_edited_inputs_discard_late_response_even_without_text_event(native_handlers, field):
    e = native_handlers; select_model(e); e.handlers['preview']()
    e.executor.complete()  # Response is valid, but UI completion has not applied yet.
    if field == 'planning_source':
        e.controls[field].SetSelection(0)
    else:
        e.controls[field].ChangeValue(True if field == 'full_backup' else 'Edited after response')
    e.drain()
    assert not e.controls['buttons']['run'].IsEnabled()
    assert 'discarded' in e.controls['review'].GetValue()
    assert e.dialog.assistant_service.jobs() == () and e.app.workspaces() == ()


def test_change_then_restore_original_text_still_cancels_pending_proposal(native_handlers):
    e = native_handlers; select_model(e); e.handlers['preview']()
    original = e.controls['instruction'].GetValue()
    e.controls['instruction'].ChangeValue('Changed'); e.handlers['invalidate']()
    e.controls['instruction'].ChangeValue(original); e.handlers['invalidate']()
    e.executor.complete(); e.drain()
    assert not e.calls and not e.controls['buttons']['run'].IsEnabled()
    assert 'cancelled' in e.controls['review'].GetValue()


def test_cancel_or_close_cannot_publish_a_late_proposal(native_handlers):
    e = native_handlers; select_model(e); e.handlers['preview']()
    e.handlers['cancel'](); e.executor.complete(); e.drain()
    assert not e.calls and not e.controls['buttons']['run'].IsEnabled()
    e.handlers['preview'](); e.executor.complete()
    before = e.controls['review'].GetValue()
    e.handlers['close'](); e.dialog.Destroy(); e.drain()
    assert e.controls['review'].GetValue() == before
    assert e.app.workspaces() == () and e.dialog.assistant_service.jobs() == ()


def test_silently_edited_approved_goal_cannot_run(native_handlers):
    e = native_handlers; select_model(e); e.handlers['preview']()
    e.executor.complete(); e.drain()
    e.controls['instruction'].ChangeValue('A new goal without EVT_TEXT')
    e.handlers['run']()
    assert not e.executor.pending and not e.controls['buttons']['run'].IsEnabled()
    assert 'reviewed goal or scope changed' in e.controls['review'].GetValue()
    assert e.app.workspaces() == () and e.dialog.assistant_service.jobs() == ()


def test_changed_profile_cannot_silently_contact_a_new_model(native_handlers):
    e = native_handlers; select_model(e)
    e.store.save(replace(e.profile, model='different-model'))
    e.handlers['preview'](); e.executor.complete(); e.drain()
    assert not e.calls and not e.controls['buttons']['run'].IsEnabled()
    assert 'profile changed' in e.controls['review'].GetValue()


def test_library_switch_discards_pending_response(native_handlers, tmp_path):
    e = native_handlers; select_model(e); e.handlers['preview']()
    e.executor.complete()
    e.frame.library = LibraryApp(tmp_path / 'other-library')
    e.drain(); e.handlers['run']()
    assert not e.controls['buttons']['run'].IsEnabled()
    assert e.app.workspaces() == () and e.frame.library.workspaces() == ()


@pytest.mark.parametrize('cancelled', [False, True])
def test_narration_voice_discovery_is_background_and_cancelled_before_voice_choice(native_handlers, tmp_path, monkeypatch, cancelled):
    from carr_library.speech import Voice, VoiceRegistry
    from carr_library.studio.render import ProductionService
    e = native_handlers
    output = tmp_path / 'audio-output'; output.mkdir()
    e.controls['output'].SetValue(str(output))
    e.controls['instruction'].SetValue('Produce audio: Explicit synthetic narration.')
    discoveries, inventories, choices = [], [], []
    class Provider:
        name = 'Synthetic inventory only'
        def voices(self):
            inventories.append(True)
            return (Voice('explicit-fixture', 'Fixture', 'fixture', 'uk'),)
    def existing_voices(owner):
        discoveries.append(True)
        owner._voices = VoiceRegistry([Provider()])
        return owner._voices
    class ChoiceDialog(Widget):
        def ShowModal(self):
            choices.append(True)
            self.selection = 0
            return e.wx.ID_OK
    monkeypatch.setattr(ProductionService, 'voices', property(existing_voices))
    e.wx.SingleChoiceDialog = ChoiceDialog
    e.handlers['preview']()
    assert not discoveries and not choices and len(e.executor.pending) == 1
    assert not e.controls['buttons']['run'].IsEnabled()
    if cancelled:
        e.handlers['cancel']()
    e.executor.complete()
    assert inventories == ([] if cancelled else [True])
    assert choices == []
    e.drain()
    assert discoveries == ([] if cancelled else [True])
    assert choices == ([] if cancelled else [True])
    assert e.controls['buttons']['run'].IsEnabled() is not cancelled
    assert e.dialog.assistant_service.jobs() == ()


def test_assistant_close_cancels_work_without_nonexistent_production_lifecycle(native_handlers):
    e = native_handlers
    select_model(e)
    e.handlers['preview']()
    e.handlers['close']()
    e.handlers['close']()  # Native close and wrapper cleanup remain idempotent.
    assert len(e.executor.pending) == 1
    assert e.executor.shutdowns == [{'wait': False, 'cancel_futures': True}]
    before = e.controls['review'].GetValue()
    e.executor.complete(); e.drain()
    assert not e.calls and e.controls['review'].GetValue() == before
    assert e.app.workspaces() == () and e.dialog.assistant_service.jobs() == ()

@pytest.mark.parametrize('completed', [False, True])
@pytest.mark.parametrize('operation', ['proposal', 'voices'])
def test_direct_owner_destroy_detaches_pending_callbacks_before_controls_die(
        native_handlers, tmp_path, monkeypatch, operation, completed):
    e = native_handlers
    inventories, choices = [], []
    if operation == 'proposal':
        select_model(e)
    else:
        from carr_library.speech import Voice, VoiceRegistry
        from carr_library.studio.render import ProductionService
        output = tmp_path / 'narration-output'
        output.mkdir()
        e.controls['output'].SetValue(str(output))
        e.controls['instruction'].SetValue('Produce audio: Explicit synthetic narration.')
        class Provider:
            name = 'Synthetic inventory only'
            def voices(self):
                inventories.append(True)
                return (Voice('explicit-fixture', 'Fixture', 'fixture', 'uk'),)
        monkeypatch.setattr(ProductionService, 'voices',
                            property(lambda owner: VoiceRegistry([Provider()])))
        class ChoiceDialog(Widget):
            def ShowModal(self):
                choices.append(True)
                raise AssertionError('A destroyed dialog opened a native voice choice.')
        e.wx.SingleChoiceDialog = ChoiceDialog
    e.handlers['preview']()
    assert len(e.executor.pending) == 1
    if completed:
        e.executor.complete()  # Work finished; its GUI completion is still queued.
    request_count, inventory_count = len(e.calls), len(inventories)
    owner_id = e.dialog.GetId()

    def dead_control(*arguments, **keywords):
        raise AssertionError('A late callback touched a deleted native control.')

    # A destruction event may expose another wrapper for the same native owner.
    # Its previously captured ID must suffice, with no new owner queries.
    e.dialog.GetId = dead_control
    skipped = []
    event = SimpleNamespace(GetId=lambda: owner_id, GetEventObject=dead_control,
                            Skip=lambda: skipped.append(True))
    e.dialog.bindings[e.wx.EVT_WINDOW_DESTROY](event)
    assert skipped == [True]
    assert e.executor.shutdowns == [{'wait': False, 'cancel_futures': True}]
    controls = [e.dialog, *e.controls['buttons'].values(),
                *(value for value in e.controls.values() if isinstance(value, Widget))]
    for control in controls:
        for method in ('GetValue', 'GetSelection', 'SetValue', 'ChangeValue',
                       'Enable', 'Disable', 'SetFocus', 'IsModal', 'Hide', 'EndModal'):
            setattr(control, method, dead_control)
    if not completed:
        e.executor.complete()
    e.drain()
    e.handlers['close']()  # Modal wrapper cleanup after direct destruction is inert.
    assert e.executor.shutdowns == [{'wait': False, 'cancel_futures': True}]
    assert len(e.calls) == request_count and len(inventories) == inventory_count
    assert not choices and e.app.workspaces() == ()
    assert e.dialog.assistant_service.jobs() == ()


def test_child_destroy_event_does_not_detach_the_live_assistant(native_handlers):
    e = native_handlers
    skipped = []
    event = SimpleNamespace(GetId=lambda: e.dialog.GetId() + 1,
                            Skip=lambda: skipped.append(True))
    e.dialog.bindings[e.wx.EVT_WINDOW_DESTROY](event)
    assert skipped == [True] and e.executor.shutdowns == []
    e.handlers['preview']()
    assert e.controls['buttons']['run'].IsEnabled()
    assert e.app.workspaces() == () and not e.calls

