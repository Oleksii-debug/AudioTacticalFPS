"""Real saved scan services through deferred Import Center event handlers."""
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ui.import_center import ImportCenterActions


class Control:
    def __init__(self, value='', selection=-1):
        self.value, self.selection = value, selection
        self.items, self.enabled = [], True
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def GetSelection(self): return self.selection
    def SetSelection(self, value): self.selection = value
    def SetItems(self, items): self.items, self.selection = list(items), -1
    def Enable(self): self.enabled = True
    def Disable(self): self.enabled = False
    def SetFocus(self): pass


@pytest.fixture
def center(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Native handler source scope')
    selected = tmp_path / 'selected sources'
    selected.mkdir()
    for number in range(5):
        (selected / f'{number}.txt').write_text(f'Original source {number}.', encoding='utf-8')
    view = ImportCenterActions()
    view.library, view.workspace, view.selected_root = app, workspace, selected
    view.closed = view.busy = False
    view.batch, view.summaries = None, ()
    view.collection_ids = [None]
    view.collection = Control(selection=0)
    view.languages = Control()
    view.recursive = Control(True)
    view.ocr_config = Config()
    view.scan_files, view.scan_timeout = Control(2), Control(60)
    view.batches, view.rows = Control(), Control()
    view.details, view.status = Control(), Control()
    view.cancel, view.continue_folder = Control(), Control()
    view.cancel.Disable()
    view.continue_folder.Disable()
    view.mutable = [view.collection, view.recursive, view.scan_files, view.scan_timeout,
                    view.batches, view.continue_folder]
    view.jobs = []
    view.frame = SimpleNamespace(library=app, working=False, cancel_event=Event(), book=None)
    def start(title, operation, done, *, failed):
        view.frame.working = True
        view.frame.cancel_event.clear()
        view.jobs.append(SimpleNamespace(operation=operation, done=done, failed=failed))
    view.frame.run_job = Mock(side_effect=start)
    return view


def settle(view, job=None):
    job = job or view.jobs[-1]
    try:
        result = job.operation()
    except Exception as error:
        view.frame.working = False
        job.failed(error)
        raise
    view.frame.working = False
    job.done(result)
    return result


def first_page(view):
    view.plan((view.selected_root,), paged=True)
    return settle(view)


def test_explicit_first_page_uses_captured_budgets_and_existing_deferred_worker(center):
    center.plan((center.selected_root,), paged=True)
    assert center.busy and center.frame.working
    assert not (center.library.root / 'import-batches').exists()
    assert not any(control.enabled for control in center.mutable)
    center.scan_files.SetValue(1)
    center.scan_timeout.SetValue(120)
    center.recursive.SetValue(False)
    result = settle(center)
    assert result['scan']['file_budget'] == 2 and result['scan']['timeout'] == 60
    assert result['recursive'] is True and result['total'] == 2
    assert center.batch['batch_id'] == result['batch_id']
    assert center.continue_folder.enabled and not center.busy
    assert 'More entries may remain' in center.status.GetValue()


def test_continue_uses_captured_page_and_preserves_predecessor_while_opening_successor(center):
    first = first_page(center)
    center.continue_scan()
    assert center.busy and not center.continue_folder.enabled
    child = settle(center)
    assert child['scan']['previous_batch'] == first['batch_id']
    assert center.batch['batch_id'] == child['batch_id']
    saved = center.library.import_batch(center.workspace, first['batch_id'])
    assert saved['scan']['next_batch'] == child['batch_id']
    assert saved['rows'] == first['rows']
    center.continue_scan()
    final = settle(center)
    assert final['total'] == 1 and final['scan']['next_cursor'] is None
    assert not center.continue_folder.enabled
    assert 'scan complete' in center.status.GetValue()


def test_cancelled_page_and_late_callback_leave_retryable_saved_cursor(center):
    first = first_page(center)
    path = center.library.root / 'import-batches' / (first['batch_id'] + '.json')
    before = path.read_bytes()
    center.continue_scan()
    old_job = center.jobs[-1]
    center.cancel_operation()
    with pytest.raises(InterruptedError):
        settle(center, old_job)
    assert not center.busy and center.continue_folder.enabled
    assert path.read_bytes() == before
    center.continue_scan()
    new_job = center.jobs[-1]
    child = settle(center, new_job)
    saved_status = center.status.GetValue()
    old_job.done(first)
    old_job.failed(ValueError('stale completion'))
    assert center.batch['batch_id'] == child['batch_id'] and center.status.GetValue() == saved_status


@pytest.mark.parametrize('value', [0, 5001, True], ids=['zero', 'over-bound', 'boolean'])
def test_invalid_first_page_budget_never_starts_source_work(center, value):
    center.scan_files.SetValue(value)
    with pytest.raises(ValueError, match='between 1 and 5000'):
        center.plan((center.selected_root,), paged=True)
    assert not center.jobs and not (center.library.root / 'import-batches').exists()


def test_reordered_refresh_preserves_selected_file_identity(center):
    first = first_page(center)
    center.rows.SetSelection(0)
    selected = first['rows'][0]['row_id']
    changed = dict(first, rows=list(reversed(first['rows'])))
    center._display(changed)
    assert center.batch['rows'][center.rows.GetSelection()]['row_id'] == selected


def test_late_saved_page_completion_cannot_replace_another_open_library(center):
    center.plan((center.selected_root,), paged=True)
    job = center.jobs[-1]
    result = job.operation()
    center.frame.library = object()
    center.frame.working = False
    job.done(result)
    assert center.batch is None
    assert 'library folder changed' in center.status.GetValue()
    assert not center.continue_folder.enabled
