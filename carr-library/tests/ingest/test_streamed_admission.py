"""The explicit streamed profile stays inside the existing owner and publication."""
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker
from carr_library.ingest.streamed_text import TextProfile
from carr_library.operations import ResourceBroker
from carr_library.operations.models import ResourceUnavailable
from carr_library.storage.revisions import Storage


def prepared(tmp_path, *, profile=None):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Selected streamed source')
    source = tmp_path / 'Source.txt'
    raw = b'Exact admitted source\r\n\fSecond original field'
    source.write_bytes(raw)
    metadata = dict(title='Admitted source', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    profile = profile or TextProfile(70 * 1024**2)
    request = worker.prepare_request(app.root, workspace, source, metadata, Config(),
                                    source_profile=profile)
    return app, workspace, source, request, raw


@pytest.mark.parametrize('change', ['legacy', 'missing', 'unknown', 'boolean', 'oversized'])
def test_request_version_cannot_borrow_or_invent_a_source_profile(tmp_path, change):
    app, workspace, source, record, raw = prepared(tmp_path)
    if change == 'legacy':
        record['version'] = 1
    elif change == 'missing':
        record.pop('source_profile')
    elif change == 'unknown':
        record['version'] = 3
    elif change == 'boolean':
        record['source_profile']['maximum_bytes'] = True
    else:
        record['source_profile']['maximum_bytes'] = 128 * 1024**2 + 1
    worker._store(app.root, record['request_id'], record)
    with pytest.raises(ValueError):
        worker.load_request(app.root, record['request_id'])
    assert source.read_bytes() == raw
    assert app.browse(workspace) == ()


def test_impossible_saved_policy_refuses_before_child_or_source_access(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Admission')
    missing = tmp_path / 'never opened.txt'
    broker = ResourceBroker.for_app(app.root)
    previous = broker.limits
    broker.configure(replace(previous, memory_bytes=512 * 1024**2 - 1))
    monkeypatch.setattr(worker, '_invoke', lambda *a, **k: pytest.fail('Child started without admission.'))
    try:
        with pytest.raises(ResourceUnavailable):
            app.import_file(workspace, missing, source_profile=TextProfile())
    finally:
        broker.configure(previous)
    assert not missing.exists()
    assert tuple(app.assets_dir.iterdir()) == ()
    assert app.browse(workspace) == ()
    requests = app.import_requests(workspace)
    assert len(requests) == 1 and requests[0]['state'] == 'pending'
    assert broker.snapshot()['leases'] == []


def test_real_owned_worker_holds_the_single_existing_resource_lease(tmp_path, monkeypatch):
    app, workspace, source, request, raw = prepared(tmp_path)
    broker = ResourceBroker.for_app(app.root)
    actual, observed = worker._invoke, []
    def invoke(message, **kwargs):
        active = broker.snapshot()['leases']
        assert len(active) == 1
        row = active[0]
        assert row['operation_id'] == 'document:' + request['request_id']
        assert row['request']['memory_bytes'] == 512 * 1024**2
        assert row['request']['disk_bytes'] == 10 * request['source_profile']['maximum_bytes'] + 128 * 1024**2
        assert message['request_sha256'] == worker.request_identity(worker.load_request(app.root, request['request_id']))
        assert message['attempt_id'] == worker.load_request(app.root, request['request_id'])['attempt_id']
        observed.append(message)
        return actual(message, **kwargs)
    monkeypatch.setattr(worker, '_invoke', invoke)
    result = app.resume_import(workspace, request['request_id'])
    assert result['state'] == 'complete' and result['isolated']
    assert len(observed) == 1 and source.read_bytes() == raw
    assert broker.snapshot()['leases'] == []
    with app.read(workspace) as db:
        receipt = db.execute('SELECT request_id,attempt FROM import_publication WHERE revision=?',
                             (result['id'],)).fetchone()
        assert tuple(receipt) == (request['request_id'], observed[0]['attempt_id'])


def test_changed_request_after_real_admission_starts_no_child(tmp_path, monkeypatch):
    app, workspace, source, request, raw = prepared(tmp_path)
    actual = ResourceBroker.reserve
    @contextmanager
    def changed(self, *args, **kwargs):
        with actual(self, *args, **kwargs) as lease:
            worker.record_progress(app.root, request['request_id'],
                                   source_profile=TextProfile(71 * 1024**2).to_dict())
            yield lease
    monkeypatch.setattr(ResourceBroker, 'reserve', changed)
    monkeypatch.setattr(worker, '_invoke', lambda *a, **k: pytest.fail('Changed request reached native parsing.'))
    with pytest.raises(ValueError, match='changed while waiting'):
        app.resume_import(workspace, request['request_id'])
    assert source.read_bytes() == raw and app.browse(workspace) == ()
    assert tuple(app.assets_dir.iterdir()) == ()
    assert ResourceBroker.for_app(app.root).snapshot()['leases'] == []


def test_changed_profile_during_real_catalog_staging_rolls_back_every_publication(tmp_path, monkeypatch):
    app, workspace, source, request, raw = prepared(tmp_path)
    admitted, attempt = worker.request_identity(request), uuid4().hex
    worker.record_progress(app.root, request['request_id'], attempt=attempt, attempt_id=attempt)
    stage, observed = Storage.stage_revision, []
    def mutate(self, *args, **kwargs):
        result = stage(self, *args, **kwargs)
        observed.append(self.db.execute('SELECT count(*) FROM page').fetchone()[0])
        worker.record_progress(app.root, request['request_id'],
                               source_profile=TextProfile(71 * 1024**2).to_dict())
        return result
    monkeypatch.setattr(Storage, 'stage_revision', mutate)
    with pytest.raises(ValueError, match='changed after admission'):
        app._import_file(workspace, source, **request['metadata'], config=Config(),
            _request_id=request['request_id'], _source_profile=request['source_profile'],
            _request_sha256=admitted, _request_attempt=attempt)
    assert observed == [2]
    with app.read(workspace) as db:
        for table in ('revision', 'page', 'sealed', 'catalog_edition', 'catalog_asset',
                      'import_publication', 'import_registration'):
            assert db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        assert db.execute('SELECT count(*) FROM checkpoint').fetchone()[0] == 2
        assert db.execute('SELECT state FROM job').fetchone()[0] == 'failed'
    assert source.read_bytes() == raw


def test_streamed_worker_admission_is_checked_before_any_selected_path(tmp_path):
    app, workspace, source, request, raw = prepared(tmp_path)
    admitted = worker.request_identity(request)
    worker.record_progress(app.root, request['request_id'], attempt='a' * 32, attempt_id='a' * 32)
    source.unlink()
    with pytest.raises(ValueError, match='attempt changed'):
        app._import_file(workspace, source, **request['metadata'], config=Config(),
            _request_id=request['request_id'], _source_profile=request['source_profile'],
            _request_sha256=admitted, _request_attempt='b' * 32)
    assert not (app.root / 'imports.sqlite').exists()
    assert app.browse(workspace) == ()


def test_large_publication_hint_uses_only_its_exact_frozen_text_allowance(tmp_path):
    app, _, _, request, _ = prepared(tmp_path)
    manifest = dict(sha256='a' * 64, mime='text/plain', size=65 * 1024**2)
    candidate = dict(attempt='b' * 32, revision='c' * 64, sha256='d' * 64,
                     completed=1001, manifest=manifest)
    request.update(attempt='b' * 32, attempt_id='b' * 32, asset_sha256='a' * 64, job='c' * 64,
                   expected_manifest=manifest, publication_candidate=candidate)
    assert worker.validate_publication_candidate_document(request) == candidate
    request['source_profile'] = TextProfile(64 * 1024**2).to_dict()
    with pytest.raises(ValueError):
        worker.validate_publication_candidate_document(request)
    request['source_profile'] = TextProfile(70 * 1024**2).to_dict()
    manifest['mime'] = 'application/pdf'
    with pytest.raises(ValueError):
        worker.validate_publication_candidate_document(request)


def prepared_ordinary_document(tmp_path):
    from docx import Document
    app = LibraryApp(tmp_path / 'ordinary-library')
    workspace = app.create_workspace('Selected ordinary document')
    source = tmp_path / 'Ordinary source.docx'
    document = Document()
    document.add_paragraph('Exact ordinary source with retained document bytes.')
    document.save(source)
    raw = source.read_bytes()
    metadata = dict(title='Ordinary source', authors=[], language=None, edition=None,
                    volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    request = worker.prepare_request(app.root, workspace, source, metadata, Config())
    assert request['version'] == 1 and 'source_profile' not in request
    return app, workspace, source, request, raw


@pytest.mark.parametrize('suffix', ['.pdf', '.docx', '.epub'])
def test_ordinary_parser_refuses_impossible_policy_before_opening_source(
        tmp_path, monkeypatch, suffix):
    app = LibraryApp(tmp_path / 'ordinary-admission')
    workspace = app.create_workspace('Ordinary admission')
    missing = tmp_path / ('never opened' + suffix)
    broker = ResourceBroker.for_app(app.root)
    previous = broker.limits
    broker.configure(replace(previous, memory_bytes=512 * 1024**2 - 1))
    monkeypatch.setattr(worker, '_invoke',
                        lambda *a, **k: pytest.fail('Ordinary parser bypassed admission.'))
    try:
        with pytest.raises(ResourceUnavailable):
            app.import_file(workspace, missing)
    finally:
        broker.configure(previous)
    assert not missing.exists() and app.browse(workspace) == ()
    assert tuple(app.assets_dir.iterdir()) == ()
    requests = app.import_requests(workspace)
    assert len(requests) == 1 and requests[0]['state'] == 'pending'
    request = worker.load_request(app.root, requests[0]['request_id'])
    assert request['version'] == 1 and 'source_profile' not in request
    assert broker.snapshot()['leases'] == []


def test_real_ordinary_parser_and_publication_share_one_lease_but_replay_needs_none(
        tmp_path, monkeypatch):
    from carr_library.catalog.intake import MAX_BYTES
    app, workspace, source, request, raw = prepared_ordinary_document(tmp_path)
    broker = ResourceBroker.for_app(app.root)
    actual_invoke, actual_settle = worker._invoke, worker._settle_request
    launched, settlements = [], []

    def invoke(message, **kwargs):
        active = broker.snapshot()['leases']
        assert len(active) == 1
        lease = active[0]
        assert lease['operation_id'] == 'document:' + request['request_id']
        assert lease['request']['cpu_slots'] == 1
        assert lease['request']['memory_bytes'] == 512 * 1024**2
        assert lease['request']['disk_bytes'] == 10 * MAX_BYTES + 128 * 1024**2
        # Existing ordinary payload/schema is retained; no streamed authority.
        assert set(message) == {'data_dir', 'request_id', 'timeout'}
        launched.append(lease['id'])
        result = actual_invoke(message, **kwargs)
        assert broker.snapshot()['leases'][0]['id'] == lease['id']
        return result

    def settle(*args, **kwargs):
        settlements.append(tuple(row['id'] for row in broker.snapshot()['leases']))
        return actual_settle(*args, **kwargs)

    monkeypatch.setattr(worker, '_invoke', invoke)
    monkeypatch.setattr(worker, '_settle_request', settle)
    result = app.resume_import(workspace, request['request_id'])
    assert result['state'] == 'complete' and result['isolated'] is True
    assert len(launched) == 1 and settlements == [(launched[0],)]
    assert source.read_bytes() == raw and len(app.browse(workspace)) == 1
    stored = worker.load_request(app.root, request['request_id'])
    assert stored['version'] == 1 and 'source_profile' not in stored
    assert broker.snapshot()['leases'] == []
    with app.read(workspace) as db:
        assert db.execute('SELECT request_id FROM import_publication WHERE revision=?',
                          (result['id'],)).fetchone()[0] == request['request_id']

    # Verified terminal replay is read/reconciliation work, with no new parser
    # and no reservation even when the operator has since lowered capacity.
    previous = broker.limits
    broker.configure(replace(previous, memory_bytes=1))
    try:
        recovered = app.resume_import(workspace, request['request_id'])
    finally:
        broker.configure(previous)
    assert recovered == result
    assert len(launched) == 1 and settlements == [(launched[0],), ()]
    assert broker.snapshot()['leases'] == []
    assert source.read_bytes() == raw and len(app.browse(workspace)) == 1


def test_ordinary_request_change_after_admission_starts_no_parser(tmp_path, monkeypatch):
    app, workspace, source, request, raw = prepared_ordinary_document(tmp_path)
    actual = ResourceBroker.reserve

    @contextmanager
    def changed(self, *args, **kwargs):
        with actual(self, *args, **kwargs) as lease:
            record = worker.load_request(app.root, request['request_id'])
            record['metadata']['title'] = 'Changed after resource admission'
            worker._store(app.root, request['request_id'], record)
            yield lease

    monkeypatch.setattr(ResourceBroker, 'reserve', changed)
    monkeypatch.setattr(worker, '_invoke',
                        lambda *a, **k: pytest.fail('Changed ordinary request reached parsing.'))
    with pytest.raises(ValueError, match='changed while waiting'):
        app.resume_import(workspace, request['request_id'])
    assert source.read_bytes() == raw and app.browse(workspace) == ()
    assert tuple(app.assets_dir.iterdir()) == ()
    assert ResourceBroker.for_app(app.root).snapshot()['leases'] == []


def test_ordinary_waiting_cancel_never_opens_source_or_releases_another_operation(
        tmp_path, monkeypatch):
    from carr_library.operations import ResourceRequest
    app = LibraryApp(tmp_path / 'ordinary-waiting')
    workspace = app.create_workspace('Waiting ordinary source')
    missing = tmp_path / 'not opened.pdf'
    broker = ResourceBroker.for_app(app.root)
    previous = broker.limits
    broker.configure(replace(previous, cpu_slots=1))
    waiting = []
    original_reason = ResourceBroker._reason

    def observed_reason(self, request, volumes, active, limits):
        reason = original_reason(self, request, volumes, active, limits)
        if reason == 'cpu_slots':
            waiting.append(True)
        return reason

    monkeypatch.setattr(ResourceBroker, '_reason', observed_reason)
    monkeypatch.setattr(worker, '_invoke',
                        lambda *a, **k: pytest.fail('Cancelled ordinary request started parsing.'))
    try:
        with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='existing:heavy') as owner:
            result = app.import_file(workspace, missing, cancelled=lambda: bool(waiting))
            assert waiting and result['state'] == 'cancelled' and result['isolated'] is True
            active = broker.snapshot()['leases']
            assert len(active) == 1 and active[0]['id'] == owner.id
            assert active[0]['operation_id'] == 'existing:heavy'
            record = worker.load_request(app.root, result['request_id'])
            assert record['state'] == 'cancelled' and 'attempt_id' not in record
    finally:
        broker.configure(previous)
    assert not missing.exists() and app.browse(workspace) == ()
    assert tuple(app.assets_dir.iterdir()) == ()
    assert broker.snapshot()['leases'] == []


def test_streamed_cancel_before_admission_revokes_old_candidate_without_republication(tmp_path):
    app, workspace, source, request, raw = prepared(tmp_path)
    completed = app.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete'
    record = worker.load_request(app.root, request['request_id'])
    assert record['publication_candidate'] is not None
    old_attempt = record['attempt']
    record['state'] = 'running'
    record.pop('result')
    worker._store(app.root, request['request_id'], record)
    cancelled = app.resume_import(workspace, request['request_id'], cancelled=lambda: True)
    current = worker.load_request(app.root, request['request_id'])
    assert cancelled['state'] == 'cancelled'
    assert current['attempt'] == current['attempt_id'] != old_attempt
    assert current['publication_candidate'] is current['publication'] is None
    assert source.read_bytes() == raw
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
    assert ResourceBroker.for_app(app.root).snapshot()['leases'] == []


@pytest.mark.parametrize('streamed', [False, True], ids=['ordinary', 'streamed'])
def test_cancelled_admission_does_not_rewrite_a_concurrently_changed_request(
        tmp_path, monkeypatch, streamed):
    from carr_library.operations import ResourceRequest
    factory = prepared if streamed else prepared_ordinary_document
    app, workspace, source, request, raw = factory(tmp_path)
    broker = ResourceBroker.for_app(app.root)
    previous = broker.limits
    broker.configure(replace(previous, cpu_slots=1))
    changed = []
    original_reason = ResourceBroker._reason

    def mutate_when_waiting(self, wanted, volumes, active, limits):
        reason = original_reason(self, wanted, volumes, active, limits)
        if reason == 'cpu_slots' and not changed:
            current = worker.load_request(app.root, request['request_id'])
            current['metadata']['title'] = 'Concurrent selected request change'
            worker._store(app.root, request['request_id'], current)
            changed.append(worker._path(app.root, request['request_id']).read_bytes())
        return reason

    monkeypatch.setattr(ResourceBroker, '_reason', mutate_when_waiting)
    monkeypatch.setattr(worker, '_invoke',
                        lambda *a, **k: pytest.fail('Changed cancelled request started a child.'))
    try:
        with broker.reserve(ResourceRequest(cpu_slots=1), operation_id='existing:heavy') as owner:
            with pytest.raises(ValueError, match='changed while waiting'):
                app.resume_import(workspace, request['request_id'], cancelled=lambda: bool(changed))
            assert worker._path(app.root, request['request_id']).read_bytes() == changed[0]
            assert broker.snapshot()['leases'][0]['id'] == owner.id
    finally:
        broker.configure(previous)
    assert source.read_bytes() == raw and app.browse(workspace) == ()
    assert tuple(app.assets_dir.iterdir()) == ()
    assert broker.snapshot()['leases'] == []
