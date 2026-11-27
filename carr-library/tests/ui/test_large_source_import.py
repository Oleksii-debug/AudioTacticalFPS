"""Deferred Import Center handlers with real source/worker service acceptance."""
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import load_request
from carr_library.ui.import_center import ImportCenterActions


class Control:
    def __init__(self, value=None): self.value, self.enabled = value, True
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value
    def GetSelection(self): return self.value
    def SetFocus(self): pass
    def Enable(self): self.enabled = True
    def Disable(self): self.enabled = False


@pytest.fixture
def center(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    view = ImportCenterActions()
    view.library, view.workspace = app, app.create_workspace('Selected library')
    view.source = tmp_path / 'Original.txt'
    view.source.write_bytes(b'Exact source\r\n\fSecond segment')
    view.closed = view.busy = False
    view.collection_ids, view.collection = [None], Control(0)
    view.ocr_config = Config()
    view.large_kind, view.large_mib, view.large_timeout = Control(0), Control(2), Control(90)
    view.status, view.cancel = Control(''), Control()
    view.mutable = [view.large_kind, view.large_mib, view.large_timeout, view.collection]
    view.jobs = []
    view.frame = SimpleNamespace(library=app, working=False, book=None, cancel_event=Event())
    def queue(title, operation, done, *, failed):
        view.frame.working = True
        view.jobs.append(SimpleNamespace(operation=operation, done=done, failed=failed))
    view.frame.run_job = queue
    return view


def settle(center):
    job = center.jobs[-1]
    result = job.operation()
    center.frame.working = False
    job.done(result)
    return result


def test_native_handler_captures_profile_and_waits_for_real_worker_completion(center):
    center.import_large_source(center.source)
    assert center.busy and not center.large_mib.enabled
    assert not (center.library.root / 'import-requests').exists()
    center.large_mib.value = 4096
    result = settle(center)
    assert result['state'] == 'complete'
    assert load_request(center.library.root, result['request_id'])['source_profile']['maximum_bytes'] == 2 * 1024**2
    assert not center.busy and center.large_mib.enabled
    assert '2 exact unpaginated logical fragments' in center.status.value
    assert center.source.read_bytes() == b'Exact source\r\n\fSecond segment'


@pytest.mark.parametrize('limit', [0, 4097, True])
def test_invalid_native_allowance_starts_no_operation(center, limit):
    center.large_mib.value = limit
    with pytest.raises(ValueError, match='allowance'):
        center.import_large_source(center.source)
    assert center.jobs == []


def test_queued_operation_refuses_a_changed_library_before_reading_source(center):
    center.import_large_source(center.source)
    center.frame.library = object()
    with pytest.raises(ValueError, match='library folder changed'):
        center.jobs[-1].operation()
    assert not (center.library.root / 'import-requests').exists()


def test_cancelled_operation_keeps_resumable_request_and_reports_no_completion(center):
    center.import_large_source(center.source)
    center.frame.cancel_event.set()
    result = settle(center)
    assert result['state'] == 'cancelled'
    assert 'Resume interrupted import' in center.status.value
    assert 'Imported 2' not in center.status.value
    assert center.library.browse(center.workspace) == ()


def test_late_completion_after_closed_dialog_does_not_touch_controls(center):
    center.import_large_source(center.source)
    job = center.jobs[-1]
    result = job.operation()
    before = center.status.value
    center.closed = True
    job.done(result)
    job.failed(ValueError('late error'))
    assert center.status.value == before


def test_native_profile_rejects_a_different_source_suffix_before_queueing(center):
    center.large_kind.value = 1
    with pytest.raises(ValueError, match='match'):
        center.import_large_source(center.source)
    assert center.jobs == []


def test_native_timeout_is_captured_with_the_source_profile(center):
    center.import_large_source(center.source)
    center.large_timeout.value = 0
    assert settle(center)['state'] == 'complete'
