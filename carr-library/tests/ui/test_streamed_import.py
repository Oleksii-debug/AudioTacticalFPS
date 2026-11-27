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
    view.stream_mib, view.status, view.cancel = Control(70), Control(''), Control()
    view.mutable = [view.stream_mib, view.collection]
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
    center.import_streamed_text(center.source)
    assert center.busy and not center.stream_mib.enabled
    assert not (center.library.root / 'import-requests').exists()
    center.stream_mib.value = 128
    result = settle(center)
    assert result['state'] == 'complete'
    assert load_request(center.library.root, result['request_id'])['source_profile']['maximum_bytes'] == 70 * 1024**2
    assert not center.busy and center.stream_mib.enabled
    assert '2 exact logical fragments' in center.status.value
    assert center.source.read_bytes() == b'Exact source\r\n\fSecond segment'


@pytest.mark.parametrize('limit', [0, 129, True])
def test_invalid_native_allowance_starts_no_operation(center, limit):
    center.stream_mib.value = limit
    with pytest.raises(ValueError, match='allowance'):
        center.import_streamed_text(center.source)
    assert center.jobs == []


def test_queued_operation_refuses_a_changed_library_before_reading_source(center):
    center.import_streamed_text(center.source)
    center.frame.library = object()
    with pytest.raises(ValueError, match='library folder changed'):
        center.jobs[-1].operation()
    assert not (center.library.root / 'import-requests').exists()


def test_cancelled_operation_keeps_resumable_request_and_reports_no_completion(center):
    center.import_streamed_text(center.source)
    center.frame.cancel_event.set()
    result = settle(center)
    assert result['state'] == 'cancelled'
    assert 'Resume interrupted import' in center.status.value
    assert 'Streamed text imported' not in center.status.value
    assert center.library.browse(center.workspace) == ()


def test_late_completion_after_closed_dialog_does_not_touch_controls(center):
    center.import_streamed_text(center.source)
    job = center.jobs[-1]
    result = job.operation()
    before = center.status.value
    center.closed = True
    job.done(result)
    job.failed(ValueError('late error'))
    assert center.status.value == before
