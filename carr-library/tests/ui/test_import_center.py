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
    view.ocr_config = Config(languages='eng+ukr+slk')
    view.recursive = Control(value=True)
    view.batches, view.rows = Control(), Control(selection=0)
    view.details, view.status, view.cancel = Control(), Control(), Control()
    view.cancel.Disable()
    view.mutable = [view.collection, view.languages, view.recursive, view.batches, Control()]
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
    center.cancel_operation()
    center.jobs[0].operation()
    args, kwargs = center.library.plan_import_batch.call_args
    assert args == ('library', ('/selected/folder',))
    assert kwargs['collections'] == ('selected-collection',)
    assert kwargs['config'].languages == 'eng+ukr+slk'
    assert kwargs['recursive'] is True and kwargs['cancelled']()


@pytest.mark.parametrize('control,selection', [('collection', -1), ('collection', 2)])
def test_invalid_plan_choices_never_start_a_job(center, control, selection):
    getattr(center, control).SetSelection(selection)
    with pytest.raises(ValueError, match='Choose a destination'):
        center.plan(('/selected/folder',))
    center.frame.run_job.assert_not_called()
    center.library.plan_import_batch.assert_not_called()


def test_missing_ocr_configuration_never_starts_a_plan(center):
    center.ocr_config = None
    with pytest.raises(ValueError, match='recognition languages'):
        center.plan(('/selected/folder',))
    center.frame.run_job.assert_not_called()


def test_model_choice_updates_new_plans_only_and_cancel_preserves_previous(center, monkeypatch):
    from carr_library.ui import ocr_options
    selected = Config(languages='script/Latin+fra', dpi=200, orientation=True)
    chooser = Mock(return_value=selected)
    monkeypatch.setattr(ocr_options, 'choose_model_config', chooser)
    original = deepcopy(center.batch)
    center.choose_languages()
    chooser.assert_called_once_with(center, Config(languages='eng+ukr+slk'), preset_root=center.library.root)
    assert center.ocr_config == selected and center.batch == original
    assert 'script/Latin' in center.languages.value and 'previous settings' in center.status.value
    chooser.return_value = None
    center.choose_languages()
    assert center.ocr_config == selected and center.batch == original
    center.plan(('/selected/folder',))
    center.jobs[-1].operation()
    assert center.library.plan_import_batch.call_args.kwargs['config'] == selected


def test_busy_import_cannot_launch_model_configuration(center, monkeypatch):
    from carr_library.ui import ocr_options
    chooser = Mock()
    monkeypatch.setattr(ocr_options, 'choose_model_config', chooser)
    center.busy = True
    center.choose_languages()
    chooser.assert_not_called()
    assert 'Wait for the current operation' in center.status.value


@pytest.mark.parametrize('profile', ['fast', 'best'])
def test_managed_model_choice_is_frozen_into_new_plan_and_never_rewrites_saved_batch(center, monkeypatch, profile):
    from carr_library.ui import ocr_options
    chosen = Config(languages='ukr+script/Latin', model_profile=profile, orientation=True, dpi=200)
    monkeypatch.setattr(ocr_options, 'choose_model_config', Mock(return_value=chosen))
    saved = deepcopy(center.batch)
    center.choose_languages()
    assert center.ocr_config is chosen and profile.title() in center.languages.value
    center.plan(('/selected/scanned.pdf',))
    center.ocr_config = Config()
    center.jobs[-1].operation()
    assert center.library.plan_import_batch.call_args.kwargs['config'] == chosen
    assert center.batch == saved


@pytest.mark.parametrize('problem', ['invalid', 'changed_root', 'became_busy'])
def test_unusable_model_dialog_result_does_not_replace_current_choices(center, monkeypatch, problem):
    from carr_library.ui import ocr_options
    before = center.ocr_config
    def choose(*args, **kwargs):
        if problem == 'changed_root':
            center.frame.library = SimpleNamespace()
        if problem == 'became_busy':
            center.frame.working = True
        return SimpleNamespace(languages='ukr') if problem == 'invalid' else Config(languages='ukr', model_profile='best')
    monkeypatch.setattr(ocr_options, 'choose_model_config', choose)
    center.choose_languages()
    assert center.ocr_config is before
    center.library.plan_import_batch.assert_not_called()


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


def _report_destination(center, tmp_path, monkeypatch):
    wx = sys.modules['wx']
    center.library.root = tmp_path / 'private-library'
    center.library.root.mkdir()
    target = tmp_path / 'report.txt'
    target.write_bytes(b'Existing target')
    picker = Mock()
    picker.ShowModal.return_value = 1
    picker.GetPath.return_value = str(target)
    wx.ID_OK, wx.FD_SAVE = 1, 2
    wx.YES, wx.NO = 4, 8
    wx.YES_NO, wx.NO_DEFAULT, wx.ICON_QUESTION = 16, 32, 64
    wx.FileDialog = Mock(return_value=picker)
    wx.MessageBox = Mock(return_value=wx.YES)
    return target, picker, wx


def _report_worker(job):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(job.operation).result()


def test_import_report_hashes_on_worker_then_confirms_before_guarded_publication(
        center, tmp_path, monkeypatch):
    from threading import get_ident
    from carr_library.research import export_file
    from carr_library.ui.import_center import batch_report
    target, picker, wx = _report_destination(center, tmp_path, monkeypatch)
    main_thread, calls = get_ident(), []
    capture = export_file.capture_export_destination

    def observe(*args, **kwargs):
        calls.append(get_ident())
        assert get_ident() != main_thread
        return capture(*args, **kwargs)

    monkeypatch.setattr(export_file, 'capture_export_destination', observe)
    wx.MessageBox.side_effect = lambda *args: wx.YES if get_ident() == main_thread else pytest.fail('Not UI')
    center.save_report()
    assert calls == [] and len(center.jobs) == 1
    approval = _report_worker(center.jobs[0])
    wx.MessageBox.assert_not_called()
    complete(center, center.jobs[0], approval)
    assert len(center.jobs) == 2 and target.read_bytes() == b'Existing target'
    result = _report_worker(center.jobs[1])
    complete(center, center.jobs[1], result)
    assert target.read_text(encoding='utf-8') == batch_report(center.saved['first'])
    assert center.status.value == 'Import report saved.'
    picker.Destroy.assert_called_once()


@pytest.mark.parametrize('stage', ['before_capture', 'after_capture', 'in_confirmation'])
def test_import_report_cancellation_cannot_be_cleared_by_second_job(
        center, tmp_path, monkeypatch, stage):
    target, _, wx = _report_destination(center, tmp_path, monkeypatch)
    if stage == 'in_confirmation':
        def confirm(*args):
            center.frame.cancel_event.set()
            return wx.YES
        wx.MessageBox.side_effect = confirm
    center.save_report()
    if stage == 'before_capture':
        center.frame.cancel_event.set()
        with pytest.raises(InterruptedError):
            _report_worker(center.jobs[0])
    else:
        approval = _report_worker(center.jobs[0])
        if stage == 'after_capture':
            center.frame.cancel_event.set()
        complete(center, center.jobs[0], approval)
    assert len(center.jobs) == 1 and target.read_bytes() == b'Existing target'


@pytest.mark.parametrize('change', ['closed', 'library', 'batch', 'later_job'])
def test_import_report_stale_snapshot_never_publishes_to_a_changed_owner(
        center, tmp_path, monkeypatch, change):
    target, _, wx = _report_destination(center, tmp_path, monkeypatch)
    center.save_report()
    approval = _report_worker(center.jobs[0])
    if change == 'closed':
        center.closed = True
    elif change == 'library':
        center.frame.library = object()
    elif change == 'batch':
        center.batch = batch('other')
    else:
        center._job_token = object()
    complete(center, center.jobs[0], approval)
    assert len(center.jobs) == 1 and target.read_bytes() == b'Existing target'
    wx.MessageBox.assert_not_called()


def test_import_report_destination_change_after_confirmation_is_not_overwritten(
        center, tmp_path, monkeypatch):
    import os
    target, _, _ = _report_destination(center, tmp_path, monkeypatch)
    center.save_report()
    complete(center, center.jobs[0], _report_worker(center.jobs[0]))
    stamp = target.stat()
    target.write_bytes(b'Switched target')
    os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    with pytest.raises(ValueError, match='changed'):
        _report_worker(center.jobs[1])
    assert target.read_bytes() == b'Switched target'


def test_import_report_declined_overwrite_never_starts_publication(center, tmp_path, monkeypatch):
    target, _, wx = _report_destination(center, tmp_path, monkeypatch)
    wx.MessageBox.return_value = wx.NO
    center.save_report()
    complete(center, center.jobs[0], _report_worker(center.jobs[0]))
    assert len(center.jobs) == 1 and target.read_bytes() == b'Existing target'
