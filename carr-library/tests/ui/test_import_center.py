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
    view.batches, view.rows = Control(), Control(selection=0)
    view.details, view.status, view.cancel = Control(), Control(), Control()
    view.cancel.Disable()
    view.mutable = [view.collection, view.languages, view.ocr_models_button,
                    view.recursive, view.batches, Control()]
    view.EndModal = Mock()
    view.jobs, view.pending, view.saved = jobs, pending, saved
    return view


def complete(center, job, value):
    center.frame.working = False
    job.done(value)


@pytest.mark.parametrize('existing', [False, True])
def test_import_report_capture_precedes_render_and_protects_competing_file(center, tmp_path, monkeypatch, existing):
    from carr_library.ui import import_center as ui
    from carr_library.ui.research_actions import NativeDialogs
    from carr_library.research.export_file import capture_export_destination
    target = tmp_path / 'report.txt'
    center.library.root = tmp_path / 'private'
    center.library.root.mkdir()
    if existing: target.write_bytes(b'Previously approved report')
    wx = sys.modules['wx']
    wx.ID_OK, wx.FD_SAVE = 1, 2
    chooser = SimpleNamespace(ShowModal=lambda: wx.ID_OK, GetPath=lambda: str(target), Destroy=Mock())
    wx.FileDialog = Mock(return_value=chooser)
    monkeypatch.setattr(NativeDialogs, 'approve_export', lambda self, parent, path, **options:
        capture_export_destination(path, private_root=options['private_root']))
    original = ui.batch_report
    def compete(value):
        result = original(value)
        target.write_bytes(b'Newer independent import report')
        return result
    monkeypatch.setattr(ui, 'batch_report', compete)
    center.save_report()
    assert target.read_bytes() == b'Newer independent import report'
    assert 'destination changed' in center.status.value and center.status.focused
    assert wx.FileDialog.call_args.kwargs['style'] == wx.FD_SAVE
    chooser.Destroy.assert_called_once()
    assert not list(tmp_path.glob('.research-export-*'))


def test_import_report_decline_does_not_render_or_write(center, tmp_path, monkeypatch):
    from carr_library.ui import import_center as ui
    from carr_library.ui.research_actions import NativeDialogs
    target = tmp_path / 'report.txt'
    target.write_bytes(b'Keep original report')
    wx = sys.modules['wx']
    wx.ID_OK, wx.FD_SAVE = 1, 2
    wx.FileDialog = Mock(return_value=SimpleNamespace(ShowModal=lambda: wx.ID_OK,
        GetPath=lambda: str(target), Destroy=Mock()))
    monkeypatch.setattr(NativeDialogs, 'approve_export', lambda *args, **kwargs: None)
    monkeypatch.setattr(ui, 'batch_report', lambda *args: pytest.fail('Declined report rendered content'))
    center.save_report()
    assert target.read_bytes() == b'Keep original report'
    assert 'saved' not in center.status.value.lower()


def test_plan_captures_choices_and_passes_live_cancellation_to_background_work(center):
    center.plan(('/selected/folder',))
    center.library.plan_import_batch.assert_not_called()
    assert center.busy and center.cancel.enabled
    assert all(not control.enabled for control in center.mutable)
    center.collection.SetSelection(0)
    center.languages.SetSelection(0)
    center.recursive.SetValue(False)
    center.cancel_operation()
    center.jobs[0].operation()
    args, kwargs = center.library.plan_import_batch.call_args
    assert args == ('library', ('/selected/folder',))
    assert kwargs['collections'] == ('selected-collection',)
    assert kwargs['config'].languages == 'eng+ukr+slk'
    assert kwargs['recursive'] is True and kwargs['cancelled']()


@pytest.mark.parametrize('control,selection', [('collection', -1), ('collection', 2),
                                             ('languages', -1), ('languages', 4)])
def test_invalid_plan_choices_never_start_a_job(center, control, selection):
    getattr(center, control).SetSelection(selection)
    with pytest.raises(ValueError, match='Choose a destination'):
        center.plan(('/selected/folder',))
    center.frame.run_job.assert_not_called()
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
