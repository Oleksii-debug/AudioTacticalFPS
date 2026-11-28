"""Actual batch/dialog closures on the shared worker; stand-ins are not native evidence."""
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from carr_library.studio.book_audio import SplitOptions, capture_plan
from carr_library.studio.book_batch import BookBatchService
from carr_library.studio.project import Role
from carr_library.ui import book_audio_dialog, book_batch_dialog

_spec = importlib.util.spec_from_file_location('book_batch_ui_handlers', Path(__file__).with_name('test_book_audio_dialog.py'))
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
handlers = _fixture.handlers


@pytest.fixture
def batch_ui(handlers, monkeypatch):
    e = handlers
    second = e.app.add_text(e.workspace, 'Second selected source', 'Second exact literal book.')
    choices = {'indices': (0, 1), 'single': 0}
    class Picker:
        def __init__(self, *args, **kwargs): pass
        def ShowModal(self): return e.wx.ID_OK
        def GetSelections(self): return choices['indices']
        def GetSelection(self): return choices['single']
        def Destroy(self): pass
    e.wx.MultiChoiceDialog = e.wx.SingleChoiceDialog = Picker
    available = tuple(SimpleNamespace(id=identity, name=identity, language='en', capabilities=('rate', 'volume'))
                      for identity in ('synthetic-listed-voice', 'second-listed-voice'))
    e.production._voices.voices = lambda: available
    settings = {
        e.imported['edition']: ('synthetic-listed-voice', 1, 72, 'wav'),
        second['edition']: ('second-listed-voice', -1, 52, 'flac'),
    }
    outputs = {}
    for edition in settings:
        outputs[edition] = e.output.parent / ('book-output-' + edition)
        outputs[edition].mkdir()
    details, reviewed = [], []
    def detail(frame, *, service, edition_id, on_reviewed, initial_plan, initial_output, **kwargs):
        details.append((edition_id, initial_plan, initial_output))
        class Child:
            _book_destroyed = False
            def ShowModal(self):
                def work():
                    assert threading.get_ident() != e.ui_thread
                    voice, rate, volume, format = settings[edition_id]
                    plan = initial_plan or capture_plan(e.app, e.workspace, edition_id,
                        options=SplitOptions('document'), role=Role('narrator', 'Narrator', voice, 'en',
                        rate=rate, volume=volume), format=format)
                    value = service.review(plan, outputs[edition_id])
                    return plan, value
                def ready(value):
                    reviewed.append(value[0])
                    on_reviewed(*value)
                e.frame.run_job('Existing per-book review dialog fixture', work, done=ready)
                e.complete()
            def Destroy(self): pass
        return Child(), lambda: None
    monkeypatch.setattr(book_audio_dialog, 'create_book_audio_dialog', detail)
    start = len(e.state['widgets'])
    dialog, close = book_batch_dialog.create_book_batch_dialog(e.frame, service=e.service, wx_module=e.wx)
    widgets = e.state['widgets'][start:]
    def find(name):
        return next(widget for widget in widgets if widget.name == name)
    def press(name):
        control = find(name)
        assert control.enabled, name
        control.bindings[e.wx.EVT_BUTTON](None)
    yield SimpleNamespace(e=e, find=find, press=press, close=close, settings=settings, outputs=outputs,
                          details=details, reviewed=reviewed, choices=choices)
    close()


def choose_and_review(e):
    e.press('Choose library books')
    assert e.e.state['queue'] and not e.e.production.jobs()
    e.e.complete()
    select = e.find('Ordered books; review each book before Preview')
    assert len(select.choices) == 2
    for index in (0, 1):
        select.SetSelection(index)
        e.press('Review selected book')
    assert len(e.reviewed) == 2 and not e.e.production.jobs()


def preview(e):
    choose_and_review(e)
    e.press('Preview entire batch')
    assert e.e.state['queue']
    e.e.complete()
    assert 'Batch review SHA-256:' in e.find('Multiple book review and existing Studio checkpoints').value


def test_batch_real_review_prepare_order_and_per_item_settings_use_existing_worker(batch_ui):
    e = batch_ui
    choose_and_review(e)
    select = e.find('Ordered books; review each book before Preview')
    select.SetSelection(1)
    e.press('Move selected book up')
    e.press('Preview entire batch')
    e.e.complete()
    assert not e.e.production.jobs()
    e.press('Prepare batch items')
    assert len(e.e.state['approvals']) == 1 and not e.e.production.jobs()
    e.e.complete()
    batch, = BookBatchService(e.e.service).saved()
    assert [item.plan_id for item in batch.items] == [plan.id for plan in reversed(e.reviewed)]
    plans = BookBatchService(e.e.service).plans(batch)
    for plan in plans:
        expected = e.settings[plan.source['source_anchor']['edition']]
        assert (plan.role.voice_id, plan.role.rate, plan.role.volume, plan.format) == expected
    assert len(e.e.production.jobs()) == 2
    assert all(not list(path.iterdir()) for path in e.outputs.values())
    assert not e.find('Move selected book up').enabled and not e.find('Choose library books').enabled
    assert e.find('Run or resume batch').enabled
    assert 'PREPARED' in e.find('Multiple book review and existing Studio checkpoints').value


def test_batch_run_requires_explicit_frozen_review_and_keeps_borrowed_owner(batch_ui, monkeypatch):
    e = batch_ui
    preview(e)
    e.press('Prepare batch items')
    e.e.complete()
    calls = []
    def run(groups, identity, outputs, **options):
        assert threading.get_ident() != e.e.ui_thread
        options['guard']()
        assert not options['cancelled']()
        current = groups.review(groups.load(identity), outputs)
        assert options['approval'] == current.fingerprint
        calls.append((identity, tuple(outputs)))
        return {'status': 'paused', 'stored': groups.status(identity)}
    monkeypatch.setattr(BookBatchService, 'run', run)
    e.press('Run or resume batch')
    assert calls == [] and len(e.e.state['approvals']) == 2
    e.e.complete()
    assert len(calls) == 1 and 'Batch invocation: paused' in e.find(
        'Multiple book review and existing Studio checkpoints').value
    e.close()
    assert not e.e.service._closed.is_set() and not e.e.production._closed


def test_mutated_title_cannot_reuse_batch_approval(batch_ui):
    e = batch_ui
    preview(e)
    e.find('Batch title').ChangeValue('Changed after exact preview')
    e.press('Prepare batch items')
    assert not e.e.state['approvals'] and not e.e.state['queue'] and not e.e.production.jobs()
    assert 'Preview this exact' in e.find('Multiple book review and existing Studio checkpoints').value


def test_queued_preview_freezes_inputs_and_rejects_later_ui_changes(batch_ui):
    e = batch_ui
    choose_and_review(e)
    e.press('Preview entire batch')
    e.find('Batch title').ChangeValue('Changed while source worker queued')
    e.e.complete()
    assert 'changed. Preview again' in e.find('Multiple book review and existing Studio checkpoints').value
    assert not e.find('Prepare batch items').enabled and not e.e.production.jobs()


def test_close_cancels_queued_source_review_without_closing_parent_service(batch_ui, monkeypatch):
    e = batch_ui
    choose_and_review(e)
    e.press('Preview entire batch')
    entries = []
    def forbidden_plan(*args, **kwargs):
        entries.append(True)
        raise AssertionError('A closed batch must cancel before building a plan.')
    monkeypatch.setattr(book_batch_dialog, 'select_books', forbidden_plan)
    e.close()
    result = e.e.complete()
    assert isinstance(result, InterruptedError)
    assert entries == []
    assert not e.e.production.jobs() and not e.e.service._closed.is_set()


def test_source_scope_change_blocks_queued_preparation(batch_ui):
    e = batch_ui
    preview(e)
    e.press('Prepare batch items')
    original = e.e.frame.active
    e.e.frame.active = (e.e.workspace, 'different-topic')
    try:
        result = e.e.complete()
        assert isinstance(result, ValueError) and not e.e.production.jobs()
    finally:
        e.e.frame.active = original


def test_cancel_before_queued_render_does_not_enter_production(batch_ui, monkeypatch):
    e = batch_ui
    preview(e)
    e.press('Prepare batch items')
    e.e.complete()
    observed = []
    def run(groups, identity, outputs, **options):
        observed.append((options['cancelled'](), options['cancel_action']()))
        return {'status': 'cancelled', 'stored': groups.status(identity)}
    monkeypatch.setattr(BookBatchService, 'run', run)
    e.press('Run or resume batch')
    e.press('Cancel batch')
    result = e.e.complete()
    assert isinstance(result, InterruptedError) and not observed
    assert len(e.e.production.jobs()) == 2
    assert all(not list(path.iterdir()) for path in e.outputs.values())


@pytest.mark.parametrize('requested', ['Pause batch', 'Cancel batch', 'close'])
def test_active_batch_stop_uses_existing_worker_and_retains_borrowed_service(batch_ui, monkeypatch, requested):
    e = batch_ui
    preview(e)
    e.press('Prepare batch items')
    e.e.complete()
    entered, release = threading.Event(), threading.Event()
    observed = []
    def run(groups, identity, outputs, **options):
        assert threading.get_ident() != e.e.ui_thread
        entered.set()
        assert release.wait(10)
        observed.append((options['cancelled'](), options['cancel_action']()))
        return {'status': 'cancelled' if options['cancel_action']() == 'cancel' else 'paused',
                'stored': groups.status(identity)}
    monkeypatch.setattr(BookBatchService, 'run', run)
    e.press('Run or resume batch')
    label, operation, done, failed = e.e.state['queue'].pop(0)
    values = []
    worker = threading.Thread(target=lambda: values.append(operation()))
    worker.start()
    try:
        assert entered.wait(10)
        e.close() if requested == 'close' else e.press(requested)
    finally:
        release.set()
        worker.join(10)
    assert not worker.is_alive() and len(values) == 1
    e.e.frame.working = False
    done(values[0])
    assert observed == [(True, 'cancel' if requested == 'Cancel batch' else 'pause')]
    assert not e.e.service._closed.is_set() and not e.e.production._closed
    assert all(not list(path.iterdir()) for path in e.outputs.values())


def test_saved_batch_reopens_references_and_does_not_resume(batch_ui):
    e = batch_ui
    preview(e)
    e.press('Prepare batch items')
    e.e.complete()
    batch, = BookBatchService(e.e.service).saved()
    e.press('New batch')
    assert not e.find('Ordered books; review each book before Preview').choices
    e.press('Saved batches')
    e.e.complete()  # Choose the immutable group; only this choice queues its jobs.
    assert e.e.state['queue'] and not e.find('Run or resume batch').enabled
    e.e.complete()
    assert len(e.find('Ordered books; review each book before Preview').choices) == 2
    assert not e.find('Run or resume batch').enabled
    assert 'No saved batch is automatically resumed' in e.find(
        'Multiple book review and existing Studio checkpoints').value
    e.find('Ordered books; review each book before Preview').SetSelection(0)
    e.press('Review selected book')
    assert e.details[-1][1].id == batch.items[0].plan_id


def test_existing_per_book_review_mode_captures_explicit_controls_without_jobs(handlers):
    e = handlers
    voice = SimpleNamespace(id='synthetic-listed-voice', name='Declared local fixture',
                            language='en', capabilities=('rate', 'volume'))
    e.production._voices.voices = lambda: (voice,)
    start = len(e.state['widgets'])
    captured = []
    dialog, close = book_audio_dialog.create_book_audio_dialog(e.frame, service=e.service, wx_module=e.wx,
        edition_id=e.imported['edition'], on_reviewed=lambda *value: captured.append(value),
        initial_output=e.output)
    widgets = e.state['widgets'][start:]
    find = lambda name: next(widget for widget in widgets if widget.name == name)
    def press(name):
        control = find(name)
        assert control.enabled
        control.bindings[e.wx.EVT_BUTTON](None)
    try:
        e.complete()
        press('Discover local voices')
        e.complete()
        find('Voice rate, -10 to 10').ChangeValue('2')
        find('Voice volume, 0 to 100').ChangeValue('45')
        assert find('Voice rate, -10 to 10').value == '2'
        assert find('Voice volume, 0 to 100').value == '45'
        press('Preview split and names')
        e.complete()
        press('Use reviewed book')
        assert not captured and not e.production.jobs()
        e.complete()
        plan, reviewed = captured[0]
        assert (plan.role.rate, plan.role.volume) == (2, 45) and not reviewed.prepared
        assert not e.production.jobs() and not e.service.saved()
        assert not find('Run or resume saved items').enabled
        assert not e.service._closed.is_set()
    finally:
        close()

