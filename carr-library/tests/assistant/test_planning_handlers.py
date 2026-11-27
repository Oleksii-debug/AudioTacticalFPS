"""Actual native handler logic with controlled scheduling; no wx/LLM qualification."""
from concurrent.futures import Future
from dataclasses import replace
import json
import sys
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant import AssistantService
from carr_library.providers import ProfileStore, ProviderGateway, ProviderProfile
from carr_library.ui import assistant_dialog


class Widget:
    def __init__(self, parent=None, *args, value='', label='', choices=(), **kwargs):
        self.parent, self.value, self.label, self.choices = parent, value, label, list(choices)
        self.enabled, self.selection, self.name = True, -1, ''
        self.bindings, self.destroyed, self.modal = {}, False, False
    def SetName(self, value): self.name = value
    def GetValue(self): return self.value
    def SetValue(self, value):
        if self.destroyed:
            raise AssertionError('A late callback touched a destroyed control.')
        self.value = value
    ChangeValue = SetValue
    def SetSelection(self, value): self.selection = value
    def GetSelection(self): return self.selection
    def SetLabel(self, value): self.label = value
    def Enable(self, value=True): self.enabled = value
    def Disable(self): self.enabled = False
    def SetFocus(self):
        if self.destroyed:
            raise AssertionError('A late callback focused a destroyed control.')
    def Bind(self, event, callback, **kwargs): self.bindings[event] = callback
    def SetSizer(self, sizer): pass
    def IsModal(self): return self.modal
    def EndModal(self, code): self.modal = False
    def Hide(self): self.hidden = True
    def Destroy(self): self.destroyed = True


class Sizer:
    def __init__(self, *args, **kwargs): pass
    def Add(self, *args, **kwargs): pass


class ControlledExecutor:
    def __init__(self):
        self.pending, self.shutdowns = [], []
        self.in_worker = False
    def submit(self, callback, *args, **kwargs):
        future = Future()
        future.set_running_or_notify_cancel()
        self.pending.append((future, callback, args, kwargs))
        return future
    def complete(self):
        future, callback, args, kwargs = self.pending.pop(0)
        self.in_worker = True
        try:
            future.set_result(callback(*args, **kwargs))
        except BaseException as exc:
            future.set_exception(exc)
        finally:
            self.in_worker = False
    def shutdown(self, **kwargs): self.shutdowns.append(kwargs)


@pytest.fixture
def native_handlers(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    profile = ProviderProfile('chosen-local', 'Chosen local profile', 'openai-compatible',
        'http://127.0.0.1:9/v1', 'synthetic-model', ('llm',), local=True)
    store = ProfileStore(app.root / 'api-connections.json')
    store.save(profile)
    e = SimpleNamespace(app=app, profile=profile, store=store, calls=[], queue=[], choices=[],
                        before_response=lambda: None, on_confirmation=lambda: None,
                        approve=True, executor=ControlledExecutor())
    e.frame = SimpleNamespace(library=app, active=None)
    constants = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'LEFT', 'TOP',
        'ALL', 'EXPAND', 'TE_MULTILINE', 'TE_READONLY', 'EVT_BUTTON', 'EVT_TEXT', 'EVT_CHOICE',
        'EVT_CHECKBOX', 'EVT_CLOSE', 'ID_OK', 'ID_CLOSE', 'ID_YES', 'ID_NO', 'YES_NO',
        'NO_DEFAULT', 'ICON_QUESTION')
    wx = SimpleNamespace(**{name: index + 1 for index, name in enumerate(constants)})
    e.wx = wx
    def drain():
        while e.queue:
            callback, args = e.queue.pop(0)
            callback(*args)
    e.drain = drain
    class Dialog(Widget):
        def ShowModal(self):
            self.modal = True
            e.dialog = self
            e.drain()
            e.scenario(e)
            self.modal = False
            return wx.ID_CLOSE
        def Destroy(self):
            super().Destroy()
            for value in e.controls.values():
                if isinstance(value, Widget):
                    value.destroyed = True
    class Choice(Widget):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.selection = 0
        def ShowModal(self):
            e.choices.append(self)
            return wx.ID_OK
    class Confirmation(Widget):
        def ShowModal(self):
            e.on_confirmation()
            return wx.ID_YES if e.approve else wx.ID_NO
    wx.Dialog, wx.SingleChoiceDialog, wx.MessageDialog = Dialog, Choice, Confirmation
    for name in ('StaticText', 'TextCtrl', 'Button', 'Choice'):
        setattr(wx, name, Widget)
    wx.CheckBox = lambda parent, **kwargs: Widget(parent, value=False, **kwargs)
    wx.BoxSizer = Sizer
    wx.CallAfter = lambda callback, *args: e.queue.append((callback, args))
    monkeypatch.setitem(sys.modules, 'wx', wx)
    monkeypatch.setattr(assistant_dialog, 'ThreadPoolExecutor', lambda **kwargs: e.executor)
    original_validate = AssistantService.validate
    def validate_off_ui(self, plan):
        assert e.executor.in_worker, 'Local runtime/plan validation ran on the event thread.'
        return original_validate(self, plan)
    monkeypatch.setattr(AssistantService, 'validate', validate_off_ui)
    def synthetic_request(self, selected, request, **kwargs):
        if kwargs['cancelled']():
            raise InterruptedError('Synthetic request was cancelled before transmission.')
        assert request.fingerprint(selected) in kwargs['approval'].request_hashes
        assert kwargs['approval'].max_calls == 1 and kwargs['approval'].budget_micros == 0
        e.calls.append((selected, request))
        e.before_response()
        return json.dumps({'choices': [{'message': {'content': json.dumps({
            'schema': 'carr-assistant-plan/1', 'summary': 'Synthetic proposal, no model quality claim',
            'steps': [{'tool': 'library.create', 'arguments': {'name': 'Reviewed library'}}]})}}]}).encode('utf-8')
    monkeypatch.setattr(ProviderGateway, 'perform', synthetic_request)
    def ready(dialog, controls):
        e.controls = controls
    def show(scenario):
        e.scenario = scenario
        assistant_dialog.show_assistant(e.frame, on_ready=ready)
        return e
    e.show = show
    e.press = lambda name: e.controls[name].bindings[wx.EVT_BUTTON](None)
    e.invalidate = lambda field='instruction': e.controls[field].bindings[
        wx.EVT_CHOICE if field == 'scope' else wx.EVT_CHECKBOX if field == 'full_backup' else wx.EVT_TEXT](
            SimpleNamespace(Skip=lambda: None))
    e.finish = lambda: (e.executor.complete(), e.drain())
    return e


def test_guided_preview_is_background_and_never_requests_a_model(native_handlers):
    def scenario(e):
        e.press('preview')
        assert len(e.executor.pending) == 1 and not e.calls
        assert not e.controls['run'].enabled
        e.finish()
        assert e.controls['run'].enabled and not e.calls
        assert AssistantService(e.app).jobs() == () and e.app.workspaces() == ()
    native_handlers.show(scenario)


def test_model_proposal_requires_separate_preview_and_run(native_handlers):
    def scenario(e):
        e.controls['instruction'].ChangeValue('Create one named library.')
        e.press('model')
        assert len(e.executor.pending) == 1 and not e.calls
        assert not any(button.enabled for button in e.controls['folder_buttons'])
        e.finish()
        assert len(e.calls) == 1 and not e.controls['run'].enabled
        assert 'Nothing has run' in e.controls['review'].GetValue()
        e.press('run')
        assert not e.executor.pending and AssistantService(e.app).jobs() == ()
        e.press('preview')
        e.finish()
        assert e.controls['run'].enabled and AssistantService(e.app).jobs() == ()
        e.press('run')
        assert AssistantService(e.app).jobs() == () and e.app.workspaces() == ()
        e.finish()
        assert len(AssistantService(e.app).jobs()) == 1
        assert [row.name for row in e.app.workspaces()] == ['Reviewed library']
        assert len(e.calls) == 1
    native_handlers.show(scenario)


@pytest.mark.parametrize('field', ['instruction', 'input', 'output', 'full_backup',
                                   'budget', 'file_budget', 'deadline', 'scope'])
def test_changed_inputs_discard_late_response_without_relying_on_events(native_handlers, field):
    def scenario(e):
        e.press('model')
        e.executor.complete()
        if field == 'scope':
            e.controls[field].SetSelection(-1)
        else:
            e.controls[field].ChangeValue(True if field == 'full_backup' else 'changed')
        e.drain()
        assert not e.controls['run'].enabled
        assert 'discarded' in e.controls['review'].GetValue()
        assert AssistantService(e.app).jobs() == () and e.app.workspaces() == ()
    native_handlers.show(scenario)


def test_edit_then_restore_original_still_cancels_the_pending_request(native_handlers):
    def scenario(e):
        e.press('model')
        old = e.controls['instruction'].GetValue()
        e.controls['instruction'].ChangeValue('Edited')
        e.invalidate()
        e.controls['instruction'].ChangeValue(old)
        e.invalidate()
        e.finish()
        assert not e.calls and not e.controls['run'].enabled
        assert 'cancelled' in e.controls['review'].GetValue()
    native_handlers.show(scenario)


def test_changed_goal_during_native_confirmation_never_reaches_worker(native_handlers):
    def scenario(e):
        e.on_confirmation = lambda: e.controls['instruction'].ChangeValue('Changed while the question was open')
        e.press('model')
        assert not e.executor.pending and not e.calls
        assert 'changed before' in e.controls['review'].GetValue()
    native_handlers.show(scenario)


def test_saved_profile_change_before_dispatch_never_contacts_stale_model(native_handlers):
    def scenario(e):
        e.press('model')
        e.store.save(replace(e.profile, model='changed-model'))
        e.finish()
        assert not e.calls and not e.controls['run'].enabled
        assert 'profile changed' in e.controls['review'].GetValue()
    native_handlers.show(scenario)


def test_changed_library_discards_a_completed_proposal(native_handlers, tmp_path):
    def scenario(e):
        e.press('model')
        e.executor.complete()
        other = LibraryApp(tmp_path / 'other-library')
        e.frame.library = other
        e.drain()
        e.press('run')
        assert 'discarded' in e.controls['review'].GetValue()
        assert e.app.workspaces() == other.workspaces() == ()
        assert not e.controls['run'].enabled
    native_handlers.show(scenario)


def test_silent_change_of_reviewed_budget_cannot_submit_a_job(native_handlers):
    def scenario(e):
        e.controls['instruction'].ChangeValue('Create library "Reviewed library"')
        e.press('preview')
        e.finish()
        assert e.controls['run'].enabled
        e.controls['budget'].ChangeValue('2048')
        e.press('run')
        assert not e.executor.pending and not e.controls['run'].enabled
        assert 'reviewed goal or scope changed' in e.controls['review'].GetValue()
        assert AssistantService(e.app).jobs() == () and e.app.workspaces() == ()
    native_handlers.show(scenario)


@pytest.mark.parametrize('cancelled', [False, True])
def test_voice_inventory_stays_off_event_thread_and_cannot_publish_after_cancel(
        native_handlers, tmp_path, monkeypatch, cancelled):
    from carr_library import runtime_settings
    from carr_library.audio.engines import Voice, VoiceRegistry
    e = native_handlers
    discoveries = []
    class Engine:
        id = 'fixture'
        def voices(self, *, cancelled):
            discoveries.append(True)
            return (Voice('fixture:voice', 'Synthetic voice', ('uk',), 'fixture'),)
    monkeypatch.setattr(runtime_settings, 'configured_voice_registry',
                        lambda *args, **kwargs: VoiceRegistry([Engine()]))
    output = tmp_path / 'audio-output'
    output.mkdir()
    def scenario(e):
        e.controls['output'].ChangeValue(str(output))
        e.controls['instruction'].ChangeValue('Produce audio: Explicit synthetic narration.')
        e.press('preview')
        assert not discoveries and not e.choices
        if cancelled:
            e.press('pause')
        e.finish()
        if not cancelled:
            assert not e.controls['run'].enabled and len(e.executor.pending) == 1
            e.finish()
        assert discoveries == ([] if cancelled else [True])
        assert bool(e.choices) is (not cancelled)
        assert e.controls['run'].enabled is (not cancelled)
        assert AssistantService(e.app).jobs() == ()
    e.show(scenario)


def test_close_discards_queued_model_result_and_remains_idempotent(native_handlers):
    before = []
    def scenario(e):
        e.press('model')
        e.executor.complete()
        before.append(e.controls['review'].GetValue())
        e.press('close')
        e.press('close')
    e = native_handlers.show(scenario)
    e.drain()
    assert e.controls['review'].value == before[0]
    assert e.executor.shutdowns == [{'wait': False, 'cancel_futures': True}]
    assert AssistantService(e.app).jobs() == () and e.app.workspaces() == ()


def test_cancel_after_run_click_before_dispatch_never_submits(native_handlers):
    def scenario(e):
        e.controls['instruction'].ChangeValue('Create library "Not submitted"')
        e.press('preview')
        e.finish()
        e.press('run')
        assert len(e.executor.pending) == 1 and AssistantService(e.app).jobs() == ()
        e.press('pause')
        e.finish()
        assert AssistantService(e.app).jobs() == () and e.app.workspaces() == ()
        assert 'before submitting' in e.controls['review'].GetValue()
    native_handlers.show(scenario)


@pytest.mark.parametrize('failure', ['update', 'finish_result'])
def test_committed_service_result_survives_advisory_progress_failure(native_handlers, failure):
    e = native_handlers
    notifications, finished, unexpected_failures = [], [], []

    class Observer:
        def start(self, *args, **kwargs):
            return SimpleNamespace(token='actual-service-observer-test')
        def update(self, completed, total, message, **kwargs):
            notifications.append((completed, total, message))
            # Exercise the final notification after the real service has
            # persisted its completed job, not a fake successful return.
            if failure == 'update' and completed == total:
                raise RuntimeError('Synthetic final progress observer failure')
        def finish_result(self, result, **kwargs):
            finished.append(result)
            if failure == 'finish_result':
                raise RuntimeError('Synthetic completion observer failure')
        def fail(self, error, **kwargs):
            unexpected_failures.append(error)
        def cancel(self, *args, **kwargs):
            unexpected_failures.append('unexpected cancellation')
        def request_cancel(self):
            pass

    e.frame.operation_progress = Observer()
    def scenario(e):
        e.controls['instruction'].ChangeValue('Create library "Committed despite notification"')
        e.press('preview')
        e.finish()
        assert e.controls['run'].enabled
        e.press('run')
        assert AssistantService(e.app).jobs() == () and e.app.workspaces() == ()
        e.finish()
        service = AssistantService(e.app)
        jobs = service.jobs()
        assert len(jobs) == 1 and jobs[0].status == 'complete'
        saved = service.store.load(jobs[0].id)
        assert saved['status'] == 'complete'
        assert len(saved['steps']) == 1 and saved['steps'][0]['status'] == 'complete'
        assert [row.name for row in e.app.workspaces()] == ['Committed despite notification']
        assert notifications[-1] == (1, 1, 'complete')
        assert len(finished) == 1 and finished[0].status == 'complete'
        assert not unexpected_failures and not e.executor.pending
        assert e.controls['review'].GetValue().startswith('complete: 1 of 1')
        assert e.controls['run'].enabled and e.controls['preview'].enabled
        assert not e.controls['pause'].enabled
    e.show(scenario)
