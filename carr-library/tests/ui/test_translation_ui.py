"""Real translation checkpoints and gateway; native controls are explicit doubles.

No test contacts Google or claims native keyboard/NVDA acceptance.
"""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from queue import Queue
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.assistant.translation import TranslationService, source_locations
from carr_library.progress import ProgressHub
from carr_library.providers import GOOGLE_TRANSLATE_ENDPOINT, ProfileStore, ProviderProfile
from carr_library.reader.narration import NarrationStore, new_project
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui import translation as module
from test_keyboard_composition import composed
from test_portable_sync import DeferredExecutor


class Vault:
    def __init__(self): self.values = {}
    def put(self, ref, binding, value): self.values[ref, binding] = value
    def get(self, ref, binding): return self.values[ref, binding]
    def delete(self, ref, binding): self.values.pop((ref, binding), None)


class Transport:
    def __init__(self): self.calls = []; self.after = lambda: None
    def get(self, *args, **kwargs):
        return b'{"data":{"languages":[{"language":"en","name":"English"},{"language":"uk","name":"Ukrainian"}]}}'
    def post(self, profile, route, body, headers, **kwargs):
        self.calls.append(json.loads(body))
        self.after()
        return json.dumps({'data':{'translations':[{'translatedText':'Derived '+self.calls[-1]['q']}]}}).encode()


class Control:
    def __init__(self, value=''):
        self.value, self.selection, self.items = value, -1, []
        self.enabled, self.focused, self.dead = True, False, False
        self.owner = get_ident()
    def touch(self):
        assert not self.dead, 'A callback touched a destroyed control'
        assert self.owner == get_ident(), 'A worker touched native UI'
    def Enable(self, value): self.touch(); self.enabled = value
    def ChangeValue(self, value): self.touch(); self.value = value
    def GetValue(self): self.touch(); return self.value
    def SetSelection(self, value): self.touch(); self.selection = value
    def GetSelection(self): self.touch(); return self.selection
    def SetItems(self, value): self.touch(); self.items = list(value)
    def SetFocus(self): self.touch(); self.focused = True
    def SetInsertionPoint(self, value): self.touch()


class Dialogs:
    def __init__(self): self.responses = []; self.before = lambda: None
    def choose(self, *args):
        self.before()
        return self.responses.pop(0) if self.responses else None


@pytest.fixture
def native(tmp_path):
    app = LibraryApp(tmp_path/'library')
    workspace = app.create_workspace('Translation origin')
    vault, transport = Vault(), Transport()
    profile = ProfileStore(app.root/'api-connections.json', vault).save(ProviderProfile(
        'google', 'Explicit synthetic account', 'google-translate', GOOGLE_TRANSLATE_ENDPOINT,
        'nmt', ('translation','translation_languages')), secret='synthetic-not-a-real-key')
    service = TranslationService(app, vault=vault, transport=transport)
    service.refresh_languages(profile.id, approved_profile_fingerprint=profile.fingerprint)
    frame = SimpleNamespace(library=app, active=(workspace,None), _closing=False,
        progress_actions=SimpleNamespace(panel=ProgressHub()), unsaved_note='Untouched research note')
    value = module.TranslationDialog.__new__(module.TranslationDialog)
    value.frame, value.actions = frame, module.TranslationActions(frame, service=service)
    value.queue = Queue()
    value.wx = SimpleNamespace(CallAfter=lambda callback,*args: value.queue.put((callback,args)), ID_CANCEL=2)
    value.executor = DeferredExecutor()
    value.alive = value.window_alive = True
    value.close_pending = False
    value.measured = value.origin = value.result_state = None
    value.profile_rows = value.language_rows = ()
    value.dialogs = Dialogs()
    value.dialog = SimpleNamespace(IsModal=lambda: True, EndModal=lambda result: value.ends.append(result),
                                   Destroy=lambda: value.ends.append('destroyed'))
    value.ends = []
    for name in ('profile','title','source','from_language','to_language','budget','per_call','review','status'):
        setattr(value, name, Control())
    value.title.value = 'Explicit derivative'
    value.source.value = 'Exact selected source 😀\r\nSecond line.'
    value.budget.value = '0.000100'
    value.per_call.value = '0.000010'
    value.inputs = [getattr(value,name) for name in
        ('profile','title','source','from_language','to_language','budget','per_call')]
    value.buttons = {name:Control() for name in
        ('connections','languages','reading','prepare','run','jobs','save','export','bilingual','cancel','close')}
    value.reload_profiles()
    value.to_language.SetSelection(1)
    value.transport, value.profile_record = transport, profile
    yield value
    value.actions.close()
    value.executor.shutdown(wait=True, cancel_futures=True)


def drain(native):
    while not native.queue.empty():
        callback, args = native.queue.get_nowait()
        callback(*args)


def run(native):
    future = native.executor.run()
    drain(native)
    return future


def review(native):
    native.prepare()
    run(native)
    assert native.actions.review is not None


def complete(native):
    review(native)
    native.dialogs.responses = [0]
    native.approve_run()
    run(native)
    assert native.result_state == 'complete'


def test_exact_review_is_local_and_chunk_boundaries_are_readable(native):
    native.source.value = 'Sentence 😀\r\n' * 700
    native.prepare()
    assert native.actions.busy and not native.source.enabled and not native.buttons['run'].enabled
    assert native.transport.calls == []
    run(native)
    data = native.actions.review.data
    assert data['source_text'] in native.review.value
    assert sum(data['chunk_lengths']) == len(native.source.value)
    assert 'characters 1–' in native.review.value and 'caller-entered estimates' in native.review.value
    assert native.review.focused and native.buttons['run'].enabled
    assert native.transport.calls == []


@pytest.mark.parametrize('source_changed', [False, True])
def test_editing_source_or_estimates_withdraws_old_approval(native, source_changed):
    review(native)
    previous = native.actions.job_id
    native.changed(source_changed=source_changed)
    native.approve_run()
    assert native.actions.review is None and not native.buttons['run'].enabled
    assert native.actions.job_id == (None if source_changed else previous)
    assert native.transport.calls == [] and not native.executor.pending


def test_late_cancel_discards_local_review_before_ui_delivery(native):
    native.prepare()
    native.executor.run()
    native.cancel()
    drain(native)
    assert native.actions.review is None and native.transport.calls == []
    assert native.frame.progress_actions.panel.snapshot()[-1].state == 'cancelled'
    assert not native.buttons['run'].enabled and not native.actions.busy


def test_late_cancel_preserves_actual_durable_translation_receipt(native):
    review(native)
    native.dialogs.responses = [0]
    native.approve_run()
    native.executor.run()
    native.cancel()
    drain(native)
    assert native.result_state == 'complete' and native.actions.review is None
    assert native.buttons['save'].enabled and 'Derived Exact selected source' in native.review.value
    assert len(native.transport.calls) == 1
    assert native.frame.progress_actions.panel.snapshot()[-1].state == 'completed'


def test_changed_scope_inside_confirmation_never_sends(native):
    review(native)
    native.dialogs.responses = [0]
    native.dialogs.before = lambda: setattr(native.frame, 'active', None)
    native.approve_run()
    assert native.transport.calls == [] and not native.executor.pending


@pytest.mark.parametrize('change', ['destroy','scope','database'])
def test_queued_completion_cannot_touch_destroyed_or_rebound_controls(native, tmp_path, change):
    native.prepare()
    native.executor.run()
    if change == 'destroy':
        event = SimpleNamespace(GetEventObject=lambda:native.dialog, Skip=lambda:None)
        native.destroyed(event)
    elif change == 'scope':
        native.frame.active = None
    else:
        native.frame.library.database = tmp_path/'other.sqlite'
    for control in native.inputs + [native.status,native.review] + list(native.buttons.values()):
        control.dead = True
    drain(native)
    assert not native.actions.busy and native.actions.review is None
    assert native.frame.progress_actions.panel.snapshot()[-1].state == 'cancelled'


def test_close_veto_waits_for_worker_then_settles_cancelled(native):
    native.prepare()
    vetoes = []
    native.close(SimpleNamespace(CanVeto=lambda:True, Veto=lambda:vetoes.append(True)))
    assert vetoes == [True] and not native.ends and native.actions.busy
    run(native)
    assert native.ends == [native.wx.ID_CANCEL] and not native.alive
    assert native.frame.progress_actions.panel.snapshot()[-1].state == 'cancelled'
    assert native.transport.calls == []


def test_real_worker_destroy_keeps_progress_live_until_owned_future_finishes(native):
    native.executor = ThreadPoolExecutor(max_workers=1)
    entered, release = Event(), Event()
    def operation(stopped, progress):
        entered.set()
        assert release.wait(5)
        assert stopped()
        raise InterruptedError('Owned work stopped')
    assert native._start('Held actual worker', operation, lambda value:pytest.fail('Destroyed result delivery'))
    assert entered.wait(5)
    native.destroyed(SimpleNamespace(GetEventObject=lambda:native.dialog, Skip=lambda:None))
    assert native.frame.progress_actions.panel.snapshot()[-1].state == 'running'
    release.set()
    native.executor.shutdown(wait=True)
    drain(native)
    assert native.frame.progress_actions.panel.snapshot()[-1].state == 'cancelled'
    assert not native.actions.busy


def test_rejected_executor_restores_controls_without_hidden_request(native):
    native.executor = ThreadPoolExecutor(max_workers=1)
    native.executor.shutdown(wait=True)
    native.prepare()
    assert not native.actions.busy and native.source.enabled
    assert not native.buttons['run'].enabled and native.transport.calls == []
    native.executor = DeferredExecutor()
    review(native)


def test_saved_job_reopens_with_blank_budget_and_fresh_approval_only(native):
    complete(native)
    identity = native.actions.job_id
    native.changed()
    native.dialogs.responses = [0]
    native.jobs()
    run(native)  # The chooser schedules a fresh file read on the worker.
    assert native.actions.busy and len(native.executor.pending) == 1
    run(native)
    assert native.actions.job_id == identity and native.actions.review is None
    assert native.budget.value == native.per_call.value == ''
    assert native.result_state == 'complete' and len(native.transport.calls) == 1


def test_saved_reading_selection_loads_on_worker_and_revalidates_before_translation(native, monkeypatch):
    store = NarrationStore(native.frame.library.root)
    imported = native.frame.library.add_text(native.frame.active[0], 'Saved input', 'Saved source')
    page = ReadingDesk(native.frame.library, native.frame.active[0], imported['edition']).page(1)
    project = new_project(page.raw_text, title='Saved input', source_location=page.location)
    digest = store.save(project)
    native.dialogs.responses = [0]
    native.reading_copy()
    run(native)
    assert native.actions.busy and len(native.executor.pending) == 1
    run(native)
    assert native.origin == (project.id,digest) and native.source.value == project.current_text
    native.title.value = 'A new title for the same retained source'
    native.changed(text_changed=False)
    assert native.origin == (project.id,digest)
    native.prepare()
    run(native)
    assert native.actions.review.data['source_text'] == 'Saved source' and native.transport.calls == []
    assert source_locations(native.actions.service.details(native.actions.job_id)) == (page.location,)


def test_saved_job_choice_after_scope_change_schedules_no_read(native):
    complete(native)
    native.dialogs.responses = [0]
    native.dialogs.before = lambda:setattr(native.frame, 'active', None)
    native.jobs()
    run(native)
    assert not native.executor.pending and len(native.transport.calls) == 1


def test_derivative_save_keeps_original_and_current_unsaved_notebook(native):
    complete(native)
    workspace = native.frame.active
    native.save_copy()
    run(native)
    record = native.actions.service.details(native.actions.job_id)
    project,_ = NarrationStore(native.frame.library.root).load(record['reading_project']['id'])
    assert project.original_text == native.source.value and project.current_text == 'Derived '+native.source.value
    assert native.frame.active == workspace and native.frame.unsaved_note == 'Untouched research note'


def test_actual_bilingual_export_handler_uses_new_destination(native, tmp_path):
    complete(native)
    target = tmp_path/'selected-bilingual.txt'
    native.wx.FD_SAVE, native.wx.ID_OK = 1, 1
    native.wx.FileDialog = lambda *args,**kwargs:SimpleNamespace(
        ShowModal=lambda:1, GetPath=lambda:str(target), IsBeingDeleted=lambda:False, Destroy=lambda:None)
    native.export_bilingual()
    run(native)
    content = target.read_text(encoding='utf-8')
    assert 'not quotation evidence' in content and 'ORIGINAL\nExact selected source' in content
    assert 'TRANSLATION\nDerived Exact selected source' in content
    prior = target.read_bytes()
    native.export_bilingual()
    run(native)
    assert target.read_bytes() == prior and len(native.transport.calls) == 1


def test_cancelled_multichunk_run_exposes_retained_partial_text_without_final_save(native):
    native.source.value = 'Exact source. ' * 1000
    review(native)
    native.transport.after = native.actions.cancelled.set
    native.dialogs.responses = [0]
    native.approve_run()
    run(native)
    assert native.result_state == 'paused' and len(native.transport.calls) == 1
    assert 'Accepted partial machine translation' in native.review.value
    assert 'Derived Exact source.' in native.review.value
    assert not native.buttons['save'].enabled and not native.buttons['export'].enabled
    assert native.actions.review is None
    native.approve_run()
    assert not native.executor.pending and len(native.transport.calls) == 1


def test_uncertain_history_exposes_local_receipt_without_authorizing_retry(native):
    review(native)
    def failed():
        raise RuntimeError('Synthetic uncertain response')
    native.transport.after = failed
    native.dialogs.responses = [0]
    native.approve_run()
    run(native)
    identity = native.actions.job_id
    record = native.actions.service.details(identity)
    assert record['status'] == 'attention'
    native.dialogs.responses = [0]
    native.jobs()
    run(native)
    run(native)
    assert record['pending']['operation_id'] in native.review.value
    assert record['pending']['request_sha256'] in native.review.value
    assert 'not a provider receipt' in native.review.value
    assert not native.buttons['run'].enabled and not native.buttons['save'].enabled
    assert len(native.transport.calls) == 1


def test_translation_menu_joins_existing_shared_keyboard_manager(composed, monkeypatch):
    frame, manager, handlers = composed
    opened = []
    monkeypatch.setattr(module, 'show_translation', opened.append)
    count = frame.menu_bar.GetMenuCount()
    item = module.install_translation_actions(frame)
    assert module.install_translation_actions(frame) is item
    assert manager.available('translation.open') and manager.shortcut('translation.open') == ''
    assert frame.menu_bar.GetMenuCount() == count
    handlers[item.GetId()](None)
    assert opened == [frame]
