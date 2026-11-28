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
    def __init__(self, **kwargs): self.pending = []
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
    def shutdown(self, **kwargs): pass


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
        'EVT_CHECKBOX','EVT_CLOSE','ID_OK','ID_CLOSE')
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
    discoveries, choices = [], []
    class Provider:
        name = 'Synthetic inventory only'
        def voices(self): return (Voice('explicit-fixture', 'Fixture', 'fixture', 'uk'),)
    def discover(owner, *, cancelled):
        discoveries.append(True)
        if cancelled(): raise InterruptedError('Voice inventory cancelled')
        owner._voices = VoiceRegistry([Provider()])
        return owner._voices
    class ChoiceDialog(Widget):
        def ShowModal(self):
            choices.append(True)
            self.selection = 0
            return e.wx.ID_OK
    monkeypatch.setattr(ProductionService, 'discover_voices', discover)
    e.wx.SingleChoiceDialog = ChoiceDialog
    e.handlers['preview']()
    assert not discoveries and not choices and len(e.executor.pending) == 1
    assert not e.controls['buttons']['run'].IsEnabled()
    if cancelled:
        e.handlers['cancel']()
    e.executor.complete(); e.drain()
    assert discoveries == [True]
    assert choices == ([] if cancelled else [True])
    assert e.controls['buttons']['run'].IsEnabled() is not cancelled
    assert e.dialog.assistant_service.jobs() == ()


def test_assistant_close_queues_service_cleanup_after_current_worker(native_handlers, monkeypatch):
    from carr_library.studio.render import ProductionService
    e = native_handlers
    closed = []
    original = ProductionService.close
    def close(owner):
        closed.append(True)
        original(owner)
    monkeypatch.setattr(ProductionService, 'close', close)
    select_model(e)
    e.handlers['preview']()
    e.handlers['close']()
    assert len(e.executor.pending) == 2 and closed == []
    e.executor.complete(); e.drain()
    assert closed == []
    e.executor.complete()
    assert closed == [True]
