"""Existing isolated PDF/OCR supervisor participates in one cooperative policy."""
from contextlib import contextmanager
from pathlib import Path
import tempfile
import threading

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest import worker
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest


def installation(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('OCR resource admission')
    source = tmp_path / 'original.pdf'
    from reportlab.pdfgen.canvas import Canvas
    document = Canvas(str(source))
    document.drawString(72, 700, 'Resource admission preserves this original PDF source.')
    document.save()
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=2, min_free_disk_bytes=0))
    return app, workspace, source, broker


def observe_wait(monkeypatch):
    entered = threading.Event()
    original = ResourceBroker.reserve

    @contextmanager
    def reserve(self, request, *, operation_id, **kwargs):
        if operation_id.startswith('import:'):
            entered.set()
        with original(self, request, operation_id=operation_id, **kwargs) as lease:
            yield lease

    monkeypatch.setattr(ResourceBroker, 'reserve', reserve)
    return entered


@pytest.mark.parametrize('temporary_alias', [False, True], ids=['temporary-root', 'temporary-alias'])
def test_actual_isolated_pdf_import_reserves_both_volumes_and_complete_replay_bypasses_admission(tmp_path, monkeypatch, temporary_alias):
    if temporary_alias:
        child = tmp_path / 'existing-temporary-child'
        child.mkdir()
        # Exercise the same physical-directory equivalence as Windows 8.3
        # aliases without requiring a platform-specific short-name setting.
        monkeypatch.setattr(tempfile, 'tempdir', str(child / '..'))
    app, workspace, source, broker = installation(tmp_path)
    original_bytes = source.read_bytes()
    invoke = worker._invoke
    observed = []

    def admitted(message, **kwargs):
        state = broker.snapshot()
        observed.append(state)
        assert len(state['leases']) == 1
        assert state['usage']['cpu_slots'] == broker.limits.cpu_slots
        assert state['usage']['memory_bytes'] == broker.limits.memory_bytes
        request = ResourceRequest.from_dict(state['leases'][0]['request'])
        assert {path for path, _ in request.disks} == {
            app.root, Path(tempfile.gettempdir()).resolve(strict=True)}
        return invoke(message, **kwargs)

    monkeypatch.setattr(worker, '_invoke', admitted)
    result = app.import_file(workspace, source, timeout=30)
    assert result['state'] == 'complete', result
    assert observed and broker.snapshot()['leases'] == []
    assert source.read_bytes() == original_bytes
    monkeypatch.setattr(worker, '_invoke', lambda *_args, **_kwargs: pytest.fail('Completed import was relaunched'))
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='another-engine', timeout=0):
        assert app.resume_import(workspace, result['request_id']) == result
        assert len(broker.snapshot()['leases']) == 1


def test_ocr_resource_wait_cancel_is_durable_and_starts_no_parser(tmp_path, monkeypatch):
    app, workspace, source, broker = installation(tmp_path)
    entered, stop = observe_wait(monkeypatch), threading.Event()
    results, errors = [], []
    monkeypatch.setattr(worker, '_invoke', lambda *_args, **_kwargs: pytest.fail('Cancelled OCR launched a parser'))

    def run():
        try:
            results.append(app.import_file(workspace, source, cancelled=stop.is_set, timeout=5))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    try:
        with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='occupier', timeout=0):
            thread.start()
            assert entered.wait(3)
            stop.set()
            thread.join(5)
            assert not thread.is_alive() and not errors
            assert results[0]['state'] == 'cancelled'
            assert len(broker.snapshot()['leases']) == 1
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join(5)
    result = results[0]
    assert result['isolated'] is True
    assert worker.load_request(app.root, result['request_id'])['state'] == 'cancelled'
    assert source.exists() and app.browse(workspace) == ()
    assert broker.snapshot()['leases'] == []


def test_ocr_resource_timeout_retains_resumable_request_without_native_start(tmp_path, monkeypatch):
    app, workspace, source, broker = installation(tmp_path)
    monkeypatch.setattr(worker, '_invoke', lambda *_args, **_kwargs: pytest.fail('Timed-out OCR launched a parser'))
    with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='occupier', timeout=0):
        result = app.import_file(workspace, source, timeout=.1)
        assert result['state'] == 'timed_out'
        assert len(broker.snapshot()['leases']) == 1
    assert worker.load_request(app.root, result['request_id'])['state'] == 'timed_out'
    assert app.browse(workspace) == ()


def test_workspace_archived_while_ocr_waits_is_reauthorized_before_launch(tmp_path, monkeypatch):
    app, workspace, source, broker = installation(tmp_path)
    entered, stop = observe_wait(monkeypatch), threading.Event()
    results, errors, launches = [], [], []
    invoke = worker._invoke

    def forbidden(*_args, **_kwargs):
        launches.append(True)
        raise AssertionError('An archived queued workspace launched its parser')

    monkeypatch.setattr(worker, '_invoke', forbidden)

    def run():
        try:
            results.append(app.import_file(workspace, source, cancelled=stop.is_set, timeout=5))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    try:
        with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='occupier', timeout=0):
            thread.start()
            assert entered.wait(3)
            app.archive_workspace(workspace)
        thread.join(5)
        assert not thread.is_alive()
        assert errors and not results and not launches
        assert isinstance(errors[0], (ValueError, PermissionError))
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join(5)
    requests = list((app.root / 'import-requests').glob('*.json'))
    assert len(requests) == 1
    identity = requests[0].stem
    assert worker.load_request(app.root, identity)['state'] == 'pending'
    assert broker.snapshot()['leases'] == []
    app.restore_workspace(workspace)
    monkeypatch.setattr(worker, '_invoke', invoke)
    assert app.resume_import(workspace, identity, timeout=30)['state'] == 'complete'
