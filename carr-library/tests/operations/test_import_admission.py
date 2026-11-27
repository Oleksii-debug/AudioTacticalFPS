"""Actual isolated imports share admission without replacing their request journal."""
from pathlib import Path
import threading

import pytest
from reportlab.pdfgen.canvas import Canvas

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker
from carr_library.operations import ResourceBroker, ResourceLimits, ResourceRequest, ResourceStateError
from carr_library.operations.broker import ResourceLease


def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Resource admission import')
    original = tmp_path / 'original.pdf'
    document = Canvas(str(original))
    document.drawString(72, 700, 'Original railway source.')
    document.save()
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(
        cpu_slots=1, memory_bytes=4*1024**3, disk_bytes=2*1024**3, min_free_disk_bytes=0))
    return app, workspace, original, broker


def test_actual_native_import_holds_reservation_through_cleanup_and_reuses_completed_request(tmp_path, monkeypatch):
    app, workspace, original, broker = prepared(tmp_path)
    before = original.read_bytes()
    actual, observations = worker._invoke, []
    def invoke(*args, **kwargs):
        observations.append(('start', broker.snapshot()))
        try:
            return actual(*args, **kwargs)
        finally:
            observations.append(('cleaned', broker.snapshot()))
    monkeypatch.setattr(worker, '_invoke', invoke)
    result = app.import_file(workspace, original, timeout=20)
    assert result['state'] == 'complete' and result['isolated'] is True
    identity = result['request_id']
    assert [phase for phase, _ in observations] == ['start', 'cleaned']
    assert all(snapshot['usage']['cpu_slots'] == 1
        and snapshot['usage']['memory_bytes'] == 3*1024**3
        and [row['operation_id'] for row in snapshot['leases']] == ['import:' + identity]
        for _, snapshot in observations)
    assert original.read_bytes() == before and broker.snapshot()['leases'] == []
    with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='other-owner', timeout=0):
        replay = app.resume_import(workspace, identity, timeout=.1)
        assert replay['state'] == 'complete' and replay['request_id'] == identity
        assert len(observations) == 2
        assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['other-owner']
    assert len(app.browse(workspace)) == 1


@pytest.mark.parametrize('reason', ['cancelled', 'timed_out'])
def test_waiting_import_stops_before_child_and_preserves_exact_prepared_request(tmp_path, monkeypatch, reason):
    app, workspace, original, broker = prepared(tmp_path)
    request = worker.prepare_request(app.root, workspace, original, {'title': 'Reviewed source'}, Config())
    identity, before = request['request_id'], original.read_bytes()
    cancelled, started = threading.Event(), []
    timer = threading.Timer(.05, cancelled.set)
    with monkeypatch.context() as patch:
        patch.setattr(worker, '_invoke', lambda *args, **kwargs: started.append(True))
        with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='other-owner', timeout=0):
            if reason == 'cancelled':
                timer.start()
            try:
                result = app.resume_import(workspace, identity, cancelled=cancelled.is_set, timeout=.2)
            finally:
                timer.cancel()
            assert result['state'] == reason and started == []
            record = worker.load_request(app.root, identity)
            assert record['metadata'] == request['metadata'] and 'attempt' not in record
            assert [row['operation_id'] for row in broker.snapshot()['leases']] == ['other-owner']
    assert original.read_bytes() == before and not app.browse(workspace)
    resumed = app.resume_import(workspace, identity, timeout=20)
    assert resumed['state'] == 'complete' and resumed['request_id'] == identity
    assert len(app.browse(workspace)) == 1 and broker.snapshot()['leases'] == []


def test_post_publication_lease_cleanup_error_cannot_relabel_committed_import(tmp_path, monkeypatch):
    app, workspace, original, broker = prepared(tmp_path)
    actual = ResourceLease.release
    def interrupted(lease):
        actual(lease)
        if lease.operation_id.startswith('import:'):
            raise ResourceStateError('Synthetic post-publication cleanup diagnostic')
    with monkeypatch.context() as patch:
        patch.setattr(ResourceLease, 'release', interrupted)
        with pytest.raises(ResourceStateError, match='post-publication'):
            app.import_file(workspace, original, timeout=20)
    request, = app.import_requests(workspace)
    assert request['state'] == 'complete' and request['result']['state'] == 'complete'
    monkeypatch.setattr(worker, '_invoke', lambda *args, **kwargs: pytest.fail('Committed import was repeated'))
    assert app.resume_import(workspace, request['request_id'], timeout=.1)['state'] == 'complete'
    assert len(app.browse(workspace)) == 1 and broker.snapshot()['leases'] == []
