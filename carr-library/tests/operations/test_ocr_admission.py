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


def test_fresh_precancelled_request_is_durable_then_reuses_its_real_worker_identity(tmp_path, monkeypatch):
    app, workspace, source, broker = installation(tmp_path)
    original_bytes = source.read_bytes()
    invoke, launches = worker._invoke, []

    def observed(message, **kwargs):
        launches.append(dict(message))
        return invoke(message, **kwargs)

    monkeypatch.setattr(worker, '_invoke', observed)
    try:
        result = app.import_file(workspace, source, cancelled=lambda: True, timeout=5)
        assert result['state'] == 'cancelled' and result['isolated'] is True
        identity = result['request_id']
        record = worker.load_request(app.root, identity)
        assert record['state'] == 'cancelled' and record['result'] == result
        assert record.get('attempt') is None and record.get('publication_candidate') is None
        assert not launches and app.browse(workspace) == ()
        assert app.import_requests(workspace) == (
            dict(request_id=identity, file=source.name, state='cancelled', result=result),)
        assert source.read_bytes() == original_bytes and broker.snapshot()['leases'] == []

        # A later cancelled observer of this same saved request owns no new
        # admission outcome and must leave its complete durable bytes alone.
        path = app.root / 'import-requests' / (identity + '.json')
        saved = path.read_bytes()
        repeated = app.resume_import(workspace, identity, cancelled=lambda: True, timeout=5)
        assert repeated == result and path.read_bytes() == saved
        assert not launches and worker.load_request(app.root, identity) == record

        resumed = app.resume_import(workspace, identity, timeout=30)
        assert resumed['state'] == 'complete' and resumed['request_id'] == identity
        assert len(launches) == 1 and launches[0]['request_id'] == identity
        final = worker.load_request(app.root, identity)
        assert final['state'] == 'complete' and final['result'] == resumed
        assert final['attempt'] == launches[0]['attempt_id']
        assert worker._request_identity(final) == worker._request_identity(record)
        assert app.import_requests(workspace) == (
            dict(request_id=identity, file=source.name, state='complete', result=resumed),)
        assert len(app.browse(workspace)) == 1
        assert source.read_bytes() == original_bytes and broker.snapshot()['leases'] == []
        assert [item.name for item in path.parent.glob('*.json')] == [path.name]
    finally:
        assert app.close_search()


def test_precancelled_resume_does_not_rewrite_an_explicitly_prepared_request(tmp_path, monkeypatch):
    from carr_library.extract.page_ocr import Config

    app, workspace, source, broker = installation(tmp_path)
    original_bytes = source.read_bytes()
    monkeypatch.setattr(worker, '_invoke',
                        lambda *_args, **_kwargs: pytest.fail('A cancelled prepared request launched native work'))
    try:
        prepared = worker.prepare_request(app.root, workspace, source,
            dict(title=None, authors=(), language=None, edition=None, volume=None,
                 collections=(), work_id=None, edition_id=None), Config())
        identity = prepared['request_id']
        path = app.root / 'import-requests' / (identity + '.json')
        saved = path.read_bytes()
        result = app.resume_import(workspace, identity, cancelled=lambda: True, timeout=5)
        assert result['state'] == 'cancelled' and result['request_id'] == identity
        assert result['isolated'] is True
        assert path.read_bytes() == saved
        assert worker.load_request(app.root, identity) == prepared
        assert prepared['state'] == 'pending' and prepared.get('attempt') is None
        assert app.import_requests(workspace) == (
            dict(request_id=identity, file=source.name, state='pending', result=None),)
        assert app.browse(workspace) == () and source.read_bytes() == original_bytes
        assert broker.snapshot()['leases'] == []
    finally:
        assert app.close_search()


@pytest.mark.parametrize('outcome', ['cancelled', 'timed_out'])
def test_fresh_waiting_outcome_cannot_overwrite_a_changed_current_record(tmp_path, monkeypatch, outcome):
    app, workspace, source, broker = installation(tmp_path)
    original_bytes = source.read_bytes()
    entered, stop = observe_wait(monkeypatch), threading.Event()
    results, errors, launches = [], [], []

    def forbidden(*_args, **_kwargs):
        launches.append(True)
        pytest.fail('A refused resource waiter launched native work')

    monkeypatch.setattr(worker, '_invoke', forbidden)

    def run():
        try:
            results.append(app.import_file(workspace, source, cancelled=stop.is_set, timeout=3))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    try:
        with broker.reserve(ResourceRequest(cpu_slots=2), operation_id='occupier', timeout=0):
            thread.start()
            assert entered.wait(3)
            assert thread.is_alive() and len(broker.snapshot()['leases']) == 1
            paths = list((app.root / 'import-requests').glob('*.json'))
            assert len(paths) == 1
            path, identity = paths[0], paths[0].stem
            initial = worker.load_request(app.root, identity)
            assert initial['state'] == 'pending' and initial.get('attempt') is None
            # Exercise the real progress writer while admission is genuinely
            # occupied. The stable request identity remains unchanged; only
            # a later persisted status differs from this invocation's capture.
            later = dict(state='pending', message='Independent status retained while admission is occupied.',
                         request_id=identity, isolated=True)
            worker.record_progress(app.root, identity, state='pending', result=later)
            changed = worker.load_request(app.root, identity)
            saved = path.read_bytes()
            assert changed != initial
            assert worker._request_identity(changed) == worker._request_identity(initial)
            if outcome == 'cancelled':
                stop.set()
            thread.join(5)
            assert not thread.is_alive() and not results and not launches
            assert len(errors) == 1 and isinstance(errors[0], ValueError)
            assert str(errors[0]) == 'The fresh import request changed before admission settled.'
            assert path.read_bytes() == saved and worker.load_request(app.root, identity) == changed
            assert len(broker.snapshot()['leases']) == 1
        with worker.request_lock(app.root, identity):
            assert worker.load_request(app.root, identity) == changed
        assert app.import_requests(workspace) == (
            dict(request_id=identity, file=source.name, state='pending', result=later),)
        assert app.browse(workspace) == () and source.read_bytes() == original_bytes
        assert broker.snapshot()['leases'] == []
    finally:
        stop.set()
        if thread.ident is not None:
            thread.join(5)
        assert not thread.is_alive()
        assert app.close_search()
