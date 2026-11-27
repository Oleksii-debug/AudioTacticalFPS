"""Import Center event regressions with queued callbacks; no native/NVDA claim."""
from collections import Counter
from contextlib import nullcontext
from copy import deepcopy
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import sys

import pytest

from carr_library.ui.import_center import ImportCenterActions
from carr_library.extract.page_ocr import Config


class Control:
    def __init__(self, value='', selection=-1):
        self.value, self.selection = value, selection
        self.items, self.enabled, self.focused = [], True, False

    def GetSelection(self):
        return self.selection

    def SetSelection(self, value):
        self.selection = value

    def SetItems(self, values):
        self.items, self.selection = list(values), -1

    def SetValue(self, value):
        self.value = value

    def GetValue(self):
        return self.value

    def Enable(self):
        self.enabled = True

    def Disable(self):
        self.enabled = False

    def SetFocus(self):
        self.focused = True


def batch(identity='first', states=('queued',)):
    rows = [dict(row_id=f'{identity}-{number}', file=f'{identity}-{number}.txt',
                 state=state, message='', result=None, duplicate_of=None,
                 manifest={'size': 17}) for number, state in enumerate(states)]
    return dict(batch_id=identity, created='2026-11-26T17:00:00+00:00',
                rows=rows, counts=dict(Counter(states)), total=len(rows))


@pytest.fixture
def center(monkeypatch, tmp_path):
    pending = []
    monkeypatch.setitem(sys.modules, 'wx', SimpleNamespace(
        CallAfter=lambda callback: pending.append(callback), ID_CANCEL=5101))
    saved = {'first': batch()}

    def read(workspace):
        if workspace != 'library':
            raise ValueError('Unauthorized library.')
        return nullcontext(None)

    library = SimpleNamespace(root=tmp_path, read=Mock(side_effect=read),
        import_batches=Mock(side_effect=lambda workspace: tuple(deepcopy(list(saved.values())))),
        import_batch=Mock(side_effect=lambda workspace, identity: deepcopy(saved[identity])),
        plan_import_batch=Mock(), run_import_batch=Mock(), set_import_batch_row=Mock())
    jobs = []
    frame = SimpleNamespace(library=library, working=False, cancel_event=Event(), book=None)

    def run_job(title, operation, done, *, failed):
        frame.working = True
        frame.cancel_event.clear()
        jobs.append(SimpleNamespace(operation=operation, done=done, failed=failed))

    frame.run_job = Mock(side_effect=run_job)
    view = ImportCenterActions()
    view.frame, view.library, view.workspace = frame, library, 'library'
    view.busy = view.closed = False
    view.batch, view.summaries = deepcopy(saved['first']), ()
    view.collection_ids = [None, 'selected-collection']
    view.collection, view.languages = Control(selection=1), Control(selection=3)
    view.custom_ocr_config = None
    view.ocr_models_button = Control()
    view.recursive = Control(value=True)
    view.scan_timeout = Control(value=120)
    view.batches, view.rows = Control(), Control(selection=0)
    view.details, view.status, view.cancel = Control(), Control(), Control()
    view.cancel.Disable()
    view.mutable = [view.collection, view.languages, view.ocr_models_button,
                    view.recursive, view.scan_timeout, view.batches, Control()]
    view.EndModal = Mock()
    view.jobs, view.pending, view.saved = jobs, pending, saved
    return view


def complete(center, job, value):
    center.frame.working = False
    job.done(value)


def test_plan_captures_choices_and_passes_live_cancellation_to_background_work(center):
    center.plan(('/selected/folder',))
    center.library.plan_import_batch.assert_not_called()
    assert center.busy and center.cancel.enabled
    assert all(not control.enabled for control in center.mutable)
    center.collection.SetSelection(0)
    center.ocr_config = Config(languages='eng')
    center.recursive.SetValue(False)
    center.scan_timeout.SetValue(600)
    center.cancel_operation()
    center.jobs[0].operation()
    args, kwargs = center.library.plan_import_batch.call_args
    assert args == ('library', ('/selected/folder',))
    assert kwargs['collections'] == ('selected-collection',)
    assert kwargs['config'].languages == 'eng+ukr+slk'
    assert kwargs['recursive'] is True and kwargs['cancelled']()
    assert kwargs['scan_timeout'] == 120


def test_next_scan_page_uses_saved_identity_and_not_current_new_plan_options(center):
    center.batch['scan'] = dict(page=1, next_offset=5000, unscanned=[])
    center.library.next_import_scan_page = Mock()
    center.scan_timeout.SetValue(300)
    center.next_scan_page()
    assert center.busy and len(center.jobs) == 1
    center.collection.SetSelection(0)
    center.recursive.SetValue(False)
    center.languages.SetSelection(0)
    center.scan_timeout.SetValue(600)
    center.jobs[0].operation()
    args, kwargs = center.library.next_import_scan_page.call_args
    assert args == ('library', 'first') and set(kwargs) == {'cancelled', 'scan_timeout'}
    assert kwargs['scan_timeout'] == 300
    assert kwargs['cancelled']() is False
    center.cancel_operation()
    assert kwargs['cancelled']() is True


@pytest.mark.parametrize('saved,expected', [(None, 120), ({'page': 1, 'timeout_seconds': 300}, 300)])
def test_selected_scan_budget_is_displayed_without_changing_saved_options(center, saved, expected):
    selected = batch()
    if saved is not None:
        selected['scan'] = saved
    original = deepcopy(selected)
    center._display(selected)
    assert center.scan_timeout.GetValue() == expected and selected == original


@pytest.mark.parametrize('value', [True, 29, 601, '300'])
def test_invalid_native_scan_budget_never_queues_work(center, value):
    center.scan_timeout.SetValue(value)
    with pytest.raises(ValueError, match='scan time limit'):
        center.plan(('/selected/folder',))
    center.batch['scan'] = dict(page=1, next_offset=5000, unscanned=[])
    center.library.next_import_scan_page = Mock()
    center.next_scan_page()
    assert not center.jobs and 'scan time limit' in center.status.value
    center.library.plan_import_batch.assert_not_called()
    center.library.next_import_scan_page.assert_not_called()


def test_next_scan_completion_selects_saved_page_and_keeps_previous(center):
    center.batch['scan'] = dict(page=1, next_offset=5000, unscanned=[])
    following = batch('following')
    following['scan'] = dict(page=2, next_offset=None, unscanned=[])
    center.library.next_import_scan_page = Mock(return_value=following)
    center.next_scan_button = Control()
    center.next_scan_page()
    returned = center.jobs[0].operation()
    center.saved['following'] = returned
    complete(center, center.jobs[0], returned)
    assert center.batch['batch_id'] == 'following'
    assert set(center.saved) == {'first', 'following'}
    assert not center.next_scan_button.enabled
    assert 'Scan page 2' in center.status.value and 'Earlier pages were kept' in center.status.value


@pytest.mark.parametrize('change', ['closed', 'closing', 'library'])
def test_queued_next_scan_checks_original_live_host_before_call(center, change):
    center.batch['scan'] = dict(page=1, next_offset=5000, unscanned=[])
    center.library.next_import_scan_page = Mock()
    center.next_scan_page()
    if change == 'closed':
        center.closed = True
    elif change == 'closing':
        center.frame._closing = True
    else:
        center.frame.library = object()
    with pytest.raises(ValueError, match='folder changed'):
        center.jobs[0].operation()
    center.library.next_import_scan_page.assert_not_called()


def test_failed_next_keeps_previous_page_and_reports_stale_inventory(center):
    center.batch['scan'] = dict(page=1, next_offset=5000, unscanned=[])
    original = deepcopy(center.batch)
    center.library.next_import_scan_page = Mock()
    center.next_scan_page()
    center.frame.working = False
    center.jobs[0].failed(ValueError('Source inventory changed. Start a new scan.'))
    assert center.batch == original and not center.busy
    assert 'inventory changed' in center.status.value


def test_next_scan_with_no_continuation_does_not_queue_work(center):
    center.library.next_import_scan_page = Mock()
    center.next_scan_page()
    assert not center.jobs
    center.library.next_import_scan_page.assert_not_called()
    assert 'more source files' in center.status.value


def test_stale_next_completion_cannot_replace_visible_batch(center):
    center.batch['scan'] = dict(page=1, next_offset=5000, unscanned=[])
    center.library.next_import_scan_page = Mock()
    center.next_scan_page()
    center.frame.library = object()
    complete(center, center.jobs[0], batch('stale'))
    assert center.batch['batch_id'] == 'first'
    assert 'folder changed' in center.status.value


@pytest.mark.parametrize('control,selection', [('collection', -1), ('collection', 2)])
def test_invalid_plan_choices_never_start_a_job(center, control, selection):
    getattr(center, control).SetSelection(selection)
    with pytest.raises(ValueError, match='Choose a destination'):
        center.plan(('/selected/folder',))
    center.frame.run_job.assert_not_called()
    center.library.plan_import_batch.assert_not_called()


def test_missing_ocr_configuration_never_starts_a_plan(center):
    center.languages.SetSelection(4)
    center.custom_ocr_config = None
    with pytest.raises(ValueError, match='recognition languages'):
        center.plan(('/selected/folder',))
    center.frame.run_job.assert_not_called()


def test_model_choice_updates_new_plans_only_and_cancel_preserves_previous(center, monkeypatch):
    from carr_library.ui import ocr_selection
    selected = Config(languages='script/Latin+fra', dpi=200, orientation=True)
    chooser = Mock(return_value=selected)
    monkeypatch.setattr(ocr_selection, 'choose_model_config', chooser)
    original = deepcopy(center.batch)
    center.choose_ocr_models()
    chooser.assert_called_once_with(center, initial=Config(languages='eng+ukr+slk'))
    assert center.custom_ocr_config == selected and center.batch == original
    assert 'script/Latin' in center.languages.items[4] and 'new plans' in center.status.value
    chooser.return_value = None
    center.choose_ocr_models()
    assert center.custom_ocr_config == selected and center.batch == original
    center.plan(('/selected/folder',))
    center.jobs[-1].operation()
    assert center.library.plan_import_batch.call_args.kwargs['config'] == selected


def test_busy_import_cannot_launch_model_configuration(center, monkeypatch):
    from carr_library.ui import ocr_selection
    chooser = Mock()
    monkeypatch.setattr(ocr_selection, 'choose_model_config', chooser)
    center.busy = True
    center.choose_ocr_models()
    chooser.assert_not_called()
    assert 'already running' in center.status.value


def test_progress_is_marshaled_and_completion_preserves_quarantine_status(center):
    running = batch(states=('running',))
    reviewed = batch(states=('quarantined',))

    def run(workspace, identity, *, cancelled, retry_failed, progress):
        assert (workspace, identity, retry_failed) == ('library', 'first', True)
        assert not cancelled()
        progress(running)
        center.saved['first'] = reviewed
        return reviewed

    center.library.run_import_batch.side_effect = run
    center.run(retry=True)
    result = center.jobs[0].operation()
    assert center.batch['rows'][0]['state'] == 'queued'
    center.pending.pop(0)()
    assert center.batch['rows'][0]['state'] == 'running'
    complete(center, center.jobs[0], result)
    assert not center.busy and not center.cancel.enabled
    assert all(control.enabled for control in center.mutable)
    assert 'need page review' in center.status.value
    assert 'Review pending imported page' in center.details.value
    assert center.status.focused


def test_previous_job_progress_cannot_replace_a_new_batch(center):
    center.library.run_import_batch.side_effect = lambda workspace, identity, **kwargs: (
        kwargs['progress'](batch(identity, ('running',))), batch(identity, ('complete',)))[1]
    center.run()
    first = center.jobs[-1]
    first_result = first.operation()
    complete(center, first, first_result)
    center.batch = batch('second')
    center.run()
    center.pending.pop(0)()
    assert center.batch['batch_id'] == 'second'
    assert center.busy


def test_previous_job_completion_cannot_finish_the_next_job(center):
    center.run()
    first = center.jobs[-1]
    complete(center, first, batch('first', ('complete',)))
    center.batch = batch('second')
    center.run()
    first.done(batch('first', ('complete',)))
    assert center.busy and center.cancel.enabled
    assert center.batch['batch_id'] == 'second'
    assert all(not control.enabled for control in center.mutable)


def test_mismatched_progress_identity_is_ignored(center):
    center.library.run_import_batch.side_effect = lambda workspace, identity, **kwargs: (
        kwargs['progress'](batch('unrelated', ('running',))), batch(identity))[1]
    center.run()
    center.jobs[-1].operation()
    center.pending.pop(0)()
    assert center.batch['batch_id'] == 'first'


def test_planning_cancel_and_close_do_not_claim_unsaved_queue_is_saved(center):
    center.batch = None
    center.plan(('/selected/folder',))
    center.cancel_operation()
    assert center.frame.cancel_event.is_set()
    assert 'scan' in center.status.value.lower() and 'not saved' in center.status.value.lower()
    event = SimpleNamespace(CanVeto=lambda: True, Veto=Mock())
    center.close(event)
    event.Veto.assert_called_once()
    center.EndModal.assert_not_called()
    assert not center.closed
    assert 'your queue is saved' not in center.status.value.lower()


def test_failure_restores_controls_then_close_ends_the_modal(center):
    center.run()
    center.frame.working = False
    center.jobs[-1].failed(RuntimeError('Retained source changed.'))
    assert not center.busy and not center.cancel.enabled
    assert all(control.enabled for control in center.mutable)
    assert 'Retained source changed.' in center.status.value and center.status.focused
    center.close()
    assert center.closed
    center.EndModal.assert_called_once_with(5101)


def test_closed_view_drops_progress_and_completion(center):
    center.library.run_import_batch.side_effect = lambda workspace, identity, **kwargs: (
        kwargs['progress'](batch(identity, ('running',))), batch(identity))[1]
    center.run()
    job = center.jobs[-1]
    job.operation()
    center.closed = True
    before = (deepcopy(center.batch), center.status.value)
    center.pending.pop(0)()
    job.done(batch('different'))
    assert (center.batch, center.status.value) == before
    center.library.import_batches.assert_not_called()


def test_replaced_data_directory_cannot_receive_old_completion(center):
    center.run()
    center.frame.library = SimpleNamespace()
    complete(center, center.jobs[-1], batch('different'))
    center.library.import_batches.assert_not_called()
    assert center.batch['batch_id'] == 'first'
    assert 'library folder changed' in center.status.value


@pytest.mark.parametrize('index', [-1, 1])
def test_out_of_bounds_file_selection_cannot_mutate_a_row(center, index):
    center.rows.SetSelection(index)
    center.select_row()
    assert center.details.value == ''
    center.toggle_row()
    center.library.set_import_batch_row.assert_not_called()
    assert 'Select a file first' in center.status.value


@pytest.mark.parametrize('state,included', [('queued', False), ('excluded', True),
                                           ('duplicate', True)])
def test_explicit_file_selection_uses_stable_row_identity(center, state, included):
    center.batch = batch(states=(state,))
    center.library.set_import_batch_row.return_value = batch(
        states=('queued' if included else 'excluded',))
    center.toggle_row()
    center.library.set_import_batch_row.assert_called_once_with(
        'library', 'first', 'first-0', included=included)
    assert center.batch['rows'][0]['state'] == ('queued' if included else 'excluded')


def test_lost_authorization_clears_visible_batch_data(center):
    center._display(batch())
    center.library.read.side_effect = ValueError('Library unavailable.')
    center.refresh()
    assert center.batch is None and center.rows.items == [] and center.details.value == ''
    assert center.status.value == 'Library unavailable.'


def test_custom_fast_script_selection_is_captured_only_for_new_plans(center, monkeypatch):
    from carr_library.extract.page_ocr import Config
    chosen = Config(languages='ukr+script/Latin', model_profile='fast',
                    orientation=True, dpi=150, timeout=60)
    chooser = Mock(return_value=chosen)
    monkeypatch.setattr('carr_library.ui.ocr_selection.choose_model_config', chooser)
    center.choose_ocr_models()
    chooser.assert_called_once_with(center, initial=Config(languages='eng+ukr+slk'))
    assert center.custom_ocr_config is chosen
    assert center.languages.selection == 4 and len(center.languages.items) == 5
    assert 'Fast' in center.languages.items[4] and 'script/Latin' in center.languages.items[4]
    assert 'orientation detection: on' in center.status.value
    center.plan(('/selected/scanned.pdf',))
    assert not center.ocr_models_button.enabled
    center.custom_ocr_config = Config(languages='eng', model_profile='best')
    center.jobs[-1].operation()
    assert center.library.plan_import_batch.call_args.kwargs['config'] is chosen


@pytest.mark.parametrize('with_custom', [False, True])
def test_cancel_model_chooser_preserves_previous_selection_and_settings(center, monkeypatch, with_custom):
    from carr_library.extract.page_ocr import Config
    from carr_library.ui.ocr_options import LANGUAGES
    center.languages.SetItems([text for text, _ in LANGUAGES])
    center.languages.SetSelection(3)
    if with_custom:
        center.custom_ocr_config = Config(languages='script/Latin', model_profile='best')
        center.languages.SetItems(center.languages.items + ['Previous custom configuration'])
        center.languages.SetSelection(4)
    before = (center.languages.selection, center.languages.items[:], center.custom_ocr_config)
    chooser = Mock(return_value=None)
    monkeypatch.setattr('carr_library.ui.ocr_selection.choose_model_config', chooser)
    center.choose_ocr_models()
    assert (center.languages.selection, center.languages.items, center.custom_ocr_config) == before
    center.frame.run_job.assert_not_called()


def test_reconfiguring_models_replaces_the_single_custom_choice(center, monkeypatch):
    from carr_library.extract.page_ocr import Config
    first = Config(languages='ukr', model_profile='fast')
    second = Config(languages='script/Latin', model_profile='best')
    chooser = Mock(side_effect=(first, second))
    monkeypatch.setattr('carr_library.ui.ocr_selection.choose_model_config', chooser)
    center.choose_ocr_models()
    center.choose_ocr_models()
    assert len(center.languages.items) == 5 and center.languages.selection == 4
    assert center.custom_ocr_config is second
    assert chooser.call_args.kwargs['initial'] is first
    assert 'Best' in center.languages.items[4]


def test_text_folder_plan_never_opens_model_selection_automatically(center, monkeypatch):
    from carr_library.extract.page_ocr import Config
    chooser = Mock(side_effect=AssertionError('Text plan must not open OCR selection'))
    monkeypatch.setattr('carr_library.ui.ocr_selection.choose_model_config', chooser)
    center.plan(('/selected/text-folder',))
    center.jobs[-1].operation()
    chooser.assert_not_called()
    assert center.library.plan_import_batch.call_args.kwargs['config'] == Config(languages='eng+ukr+slk')


def test_saved_batch_resume_does_not_replace_recorded_config_with_current_custom_choice(center):
    from carr_library.extract.page_ocr import Config
    center.custom_ocr_config = Config(languages='script/Latin', model_profile='fast')
    center.languages.SetSelection(4)
    center.batch['config'] = vars(Config(languages='ukr', model_profile='best')).copy()
    before = deepcopy(center.batch['config'])
    center.run(retry=True)
    center.jobs[-1].operation()
    args, kwargs = center.library.run_import_batch.call_args
    assert args == ('library', 'first')
    assert set(kwargs) == {'cancelled', 'retry_failed', 'progress'}
    assert center.batch['config'] == before


def test_unconfigured_or_invalid_custom_choice_cannot_start_plan(center):
    center.languages.SetSelection(4)
    center.custom_ocr_config = SimpleNamespace(languages='eng', model_profile='fast')
    with pytest.raises(ValueError, match='Choose a destination'):
        center.plan(('/selected/folder',))
    center.frame.run_job.assert_not_called()


def test_model_chooser_does_not_open_during_an_import_job(center, monkeypatch):
    chooser = Mock()
    monkeypatch.setattr('carr_library.ui.ocr_selection.choose_model_config', chooser)
    center.run()
    center.choose_ocr_models()
    chooser.assert_not_called()
    assert not center.ocr_models_button.enabled
    assert 'already running' in center.status.value


def test_model_choice_is_not_applied_after_library_folder_changes(center, monkeypatch):
    from carr_library.extract.page_ocr import Config
    def choose(*args, **kwargs):
        center.frame.library = SimpleNamespace()
        return Config(languages='ukr', model_profile='fast')
    monkeypatch.setattr('carr_library.ui.ocr_selection.choose_model_config', choose)
    center.choose_ocr_models()
    assert center.custom_ocr_config is None and center.languages.selection == 3
    assert 'library folder changed' in center.status.value



def recovery_ready(center, monkeypatch):
    from carr_library.ui import import_center
    center.library.import_recovery_items = Mock(return_value=(
        dict(kind='batch', identity='first', label='Saved batch first'),))
    value = dict(kind='batch', identity='first', workspace='library', checksum='a' * 64)
    center.library.review_import_recovery = Mock(return_value=value)
    center.library.approve_import_recovery = Mock(return_value=dict(
        approved=True, checksum=value['checksum'], batch_id=None, message='Captured sources approved.'))
    confirm = Mock(return_value=True)
    monkeypatch.setattr(import_center, 'confirm_restored_import', confirm)
    return value, confirm


def test_restored_import_requires_displayed_review_then_separate_start(center, monkeypatch):
    value, confirm = recovery_ready(center, monkeypatch)
    center.review_restored_imports()
    assert center.busy and len(center.jobs) == 1
    center.library.approve_import_recovery.assert_not_called()
    returned = center.jobs[0].operation()
    assert returned == value
    complete(center, center.jobs[0], returned)
    confirm.assert_called_once_with(center, value)
    assert center.busy and len(center.jobs) == 2
    receipt = center.jobs[1].operation()
    args, options = center.library.approve_import_recovery.call_args
    assert args == ('library', 'batch', 'first')
    assert options['expected_checksum'] == value['checksum'] and options['confirmed'] is True
    assert options['cancelled']() is False
    complete(center, center.jobs[1], receipt)
    assert not center.busy and center.batch['batch_id'] == 'first'
    assert 'Start queued files' in center.status.value
    center.library.run_import_batch.assert_not_called()


def test_cancelled_review_keeps_restored_import_inert(center, monkeypatch):
    value, confirm = recovery_ready(center, monkeypatch)
    confirm.return_value = False
    center.review_restored_imports()
    complete(center, center.jobs[0], center.jobs[0].operation())
    assert not center.busy and len(center.jobs) == 1
    center.library.approve_import_recovery.assert_not_called()
    assert 'remains paused' in center.status.value


@pytest.mark.parametrize('change', ['closed', 'closing', 'library'])
def test_recovery_review_rechecks_live_host_after_modal_before_approval(center, monkeypatch, change):
    from carr_library.ui import import_center
    value, _ = recovery_ready(center, monkeypatch)
    def changed(_parent, _value):
        if change == 'closed':
            center.closed = True
        elif change == 'closing':
            center.frame._closing = True
        else:
            center.frame.library = object()
        return True
    monkeypatch.setattr(import_center, 'confirm_restored_import', changed)
    center.review_restored_imports()
    complete(center, center.jobs[0], center.jobs[0].operation())
    center.library.approve_import_recovery.assert_not_called()
    assert len(center.jobs) == 1 and 'folder changed' in center.status.value


def test_stale_recovery_result_does_not_open_or_approve_another_selection(center, monkeypatch):
    value, confirm = recovery_ready(center, monkeypatch)
    center.review_restored_imports()
    changed = dict(value, identity='other')
    complete(center, center.jobs[0], changed)
    confirm.assert_not_called()
    center.library.approve_import_recovery.assert_not_called()
    assert 'another selection' in center.status.value


def test_queued_recovery_approval_checks_scope_before_worker_call(center, monkeypatch):
    value, _ = recovery_ready(center, monkeypatch)
    center.review_restored_imports()
    complete(center, center.jobs[0], center.jobs[0].operation())
    center.frame.library = object()
    with pytest.raises(ValueError, match='folder changed'):
        center.jobs[1].operation()
    center.library.approve_import_recovery.assert_not_called()


def test_recovery_approval_uses_live_cancel_and_requires_exact_receipt(center, monkeypatch):
    value, _ = recovery_ready(center, monkeypatch)
    center.review_restored_imports()
    complete(center, center.jobs[0], center.jobs[0].operation())
    center.cancel_operation()
    center.jobs[1].operation()
    callback = center.library.approve_import_recovery.call_args.kwargs['cancelled']
    assert callback() is True
    complete(center, center.jobs[1], dict(approved=True, checksum='b' * 64, message='Wrong receipt.'))
    assert 'reviewed approval' in center.status.value


def test_recovery_button_reports_empty_scope_without_scheduling(center):
    center.library.import_recovery_items = Mock(return_value=())
    center.review_restored_imports()
    assert not center.jobs
    assert 'No restored imports' in center.status.value
