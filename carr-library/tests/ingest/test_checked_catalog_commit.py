"""Actual checked catalog commits retain full audits and durable receipt boundaries.

This suite does not certify large-library throughput. It proves that a fresh
catalog publication audits once before its own commit; generic revision stores,
cold opens, recovery and replay retain their independent checks.
"""
from contextlib import closing
from hashlib import sha256
import sqlite3
from threading import Event
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp, _CatalogPublisher
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker
from carr_library.ingest.pipeline import Ingest
from carr_library.storage.revisions import Storage


def _selected(tmp_path, kind='txt'):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Checked publication')
    collection = app.create_collection(workspace, 'Selected originals')
    source = tmp_path / ('Original source.' + kind)
    if kind == 'docx':
        from docx import Document
        document = Document()
        document.add_paragraph('Original railway history. Київ.')
        document.add_paragraph('Second unchanged source paragraph.')
        document.save(source)
    else:
        source.write_bytes('Original railway history. Київ.\r\n\fSecond source.'.encode('utf-8'))
    metadata = dict(title='Selected original', authors=('Actual fixture author',),
                    language='uk', edition=None, volume=None,
                    work_id='checked-work', edition_id='checked-edition',
                    collections=(collection,))
    return app, workspace, collection, source, metadata


def _request(app, workspace, source, metadata):
    record = worker.prepare_request(app.root, workspace, source, metadata, Config())
    attempt = uuid4().hex
    worker.record_progress(app.root, record['request_id'], attempt=attempt)
    return record['request_id'], attempt


def _visible_counts(database):
    with closing(sqlite3.connect(database)) as db:
        return tuple(db.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                     for table in ('catalog_work', 'catalog_asset', 'catalog_edition',
                                   'revision', 'sealed', 'page', 'source_metadata',
                                   'catalog_membership', 'indexed', 'import_publication'))


@pytest.mark.parametrize('kind', ('txt', 'docx'))
def test_fresh_catalog_commit_audits_once_and_cold_open_rechecks_everything(tmp_path, monkeypatch, kind):
    app, workspace, collection, source, metadata = _selected(tmp_path, kind)
    original = source.read_bytes()
    real_check = Storage._check
    checks = []

    def counted(storage):
        real_check(storage)
        checks.append((storage.db.in_transaction,
                       storage.db.execute('SELECT count(*) FROM revision').fetchone()[0]))

    with monkeypatch.context() as patch:
        patch.setattr(Storage, '_check', counted)
        result = app._import_file(workspace, source, **metadata)
        assert result['state'] == 'complete'
        assert checks == [(True, 1)], 'A fresh assembled publication must have one precommit full audit.'
        reopened = LibraryApp(app.root)
        assert checks == [(True, 1), (True, 1)], 'Cold/open validation is not a cached publication receipt.'

    hit, = reopened.search(workspace, 'railway history', collection=collection)
    assert hit.revision == result['id']
    assert source.read_bytes() == original
    assert reopened.assets.resolve(sha256(original).hexdigest()).read_bytes() == original


def test_generic_ingest_storage_keeps_its_separate_full_check(tmp_path, monkeypatch):
    source = tmp_path / 'source.txt'
    source.write_bytes(b'First railway paragraph.\fSecond paragraph.')
    with closing(Storage(tmp_path / 'generic.sqlite')) as storage:
        with closing(Ingest(tmp_path / 'journal.sqlite', storage)) as ingest:
            calls = []
            real_check = storage.check

            def checked():
                calls.append(storage.db.in_transaction)
                return real_check()

            monkeypatch.setattr(storage, 'check', checked)
            job = ingest.start('workspace', 'edition', tmp_path, source.name)
            result = ingest.run('workspace', job, tmp_path, source.name)
            assert result['state'] == 'complete'
            assert calls == [False]
            assert storage.get_page('workspace', job, 1)['raw'] == 'First railway paragraph.'


def test_real_attempt_receipt_is_inside_rejectable_audit_and_retry_reuses_checkpoints(tmp_path, monkeypatch):
    app, workspace, collection, source, metadata = _selected(tmp_path)
    original = source.read_bytes()
    request_id, attempt = _request(app, workspace, source, metadata)
    real_check = Storage._check
    observed = []

    def reject_after_real_check(storage):
        real_check(storage)
        receipt = storage.db.execute(
            'SELECT revision,request_id,attempt,sha256 FROM import_publication').fetchone()
        assert receipt is not None and tuple(receipt[1:3]) == (request_id, attempt)
        observed.append(_visible_counts(app.database))
        raise RuntimeError('Rejected complete checked candidate.')

    with monkeypatch.context() as patch:
        patch.setattr(Storage, '_check', reject_after_real_check)
        with pytest.raises(RuntimeError, match='Rejected complete checked candidate'):
            app._import_file(workspace, source, _request_id=request_id, **metadata)

    assert observed == [(0,) * 10]
    assert _visible_counts(app.database) == (0,) * 10
    request = worker.load_request(app.root, request_id)
    job = request['job']
    assert request['publication_candidate']['attempt'] == attempt
    assert worker._reconcile_publication(
        app.root, request_id, attempt, workspace=workspace, timeout=1.0) is None
    with closing(sqlite3.connect(app.root / 'imports.sqlite')) as journal:
        checkpoints = tuple(journal.execute(
            'SELECT ordinal,payload,digest FROM checkpoint WHERE job=? ORDER BY ordinal', (job,)))
        assert len(checkpoints) == 2
        assert journal.execute('SELECT state FROM job WHERE id=?', (job,)).fetchone() == ('failed',)

    completed = app._import_file(workspace, source, _request_id=request_id,
                                 _resume_job=job, **metadata)
    assert completed['state'] == 'complete' and completed['id'] == job
    recovered = worker._reconcile_publication(
        app.root, request_id, attempt, workspace=workspace, timeout=1.0)
    assert recovered['state'] == 'complete' and recovered['id'] == job
    assert recovered['publication_recovered'] is True
    with closing(sqlite3.connect(app.root / 'imports.sqlite')) as journal:
        assert tuple(journal.execute(
            'SELECT ordinal,payload,digest FROM checkpoint WHERE job=? ORDER BY ordinal', (job,))) == checkpoints
    with app.read(workspace) as db:
        receipt = db.execute('SELECT request_id,attempt,sha256 FROM import_publication '
                             'WHERE revision=?', (job,)).fetchone()
        state = worker._publication_state(db, worker.load_request(app.root, request_id), job)
        assert tuple(receipt) == (request_id, attempt, state['sha256'])
    assert len(app.search(workspace, 'railway', collection=collection)) == 1
    assert source.read_bytes() == original
    assert app.assets.resolve(sha256(original).hexdigest()).read_bytes() == original


@pytest.mark.parametrize('damage', ('raw-digest', 'fts-postings', 'schema'))
def test_receipt_callback_damage_is_rejected_before_any_publication(tmp_path, monkeypatch, damage):
    app, workspace, _, source, metadata = _selected(tmp_path)
    request_id, attempt = _request(app, workspace, source, metadata)
    real_record = worker.record_publication_candidate
    candidates = []

    def damage_after_receipt(root, selected_request, db, selected_workspace, revision):
        real_record(root, selected_request, db, selected_workspace, revision)
        assert db.execute('SELECT attempt FROM import_publication WHERE revision=?',
                          (revision,)).fetchone()[0] == attempt
        candidates.append(revision)
        if damage == 'raw-digest':
            trigger = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='page_bu'").fetchone()[0]
            db.execute('DROP TRIGGER page_bu')
            db.execute("UPDATE page SET raw=raw || ' changed' WHERE revision=? AND ordinal=1", (revision,))
            db.execute(trigger)
        elif damage == 'fts-postings':
            row = db.execute('SELECT id,raw FROM page WHERE revision=? AND ordinal=1',
                             (revision,)).fetchone()
            db.execute("INSERT INTO page_fts(page_fts,rowid,raw) VALUES('delete',?,?)", tuple(row))
        else:
            db.execute('DROP TRIGGER indexed_ai')

    with monkeypatch.context() as patch:
        patch.setattr(worker, 'record_publication_candidate', damage_after_receipt)
        with pytest.raises((ValueError, sqlite3.DatabaseError)):
            app._import_file(workspace, source, _request_id=request_id, **metadata)

    assert len(candidates) == 1
    assert _visible_counts(app.database) == (0,) * 10
    assert worker._reconcile_publication(
        app.root, request_id, attempt, workspace=workspace, timeout=1.0) is None
    reopened = LibraryApp(app.root)
    assert reopened.browse(workspace) == ()
    assert reopened.assets.resolve(sha256(source.read_bytes()).hexdigest()).read_bytes() == source.read_bytes()


def test_late_cancel_keeps_checked_commit_and_its_exact_attempt_receipt(tmp_path, monkeypatch):
    app, workspace, collection, source, metadata = _selected(tmp_path)
    request_id, attempt = _request(app, workspace, source, metadata)
    stopped = Event()
    real_publish = _CatalogPublisher.publish_revision
    committed = []

    def committed_then_cancelled(publisher, *args, **kwargs):
        result = real_publish(publisher, *args, **kwargs)
        with closing(sqlite3.connect(app.database)) as observer:
            committed.append(observer.execute(
                'SELECT request_id,attempt FROM import_publication').fetchone())
        assert committed == [(request_id, attempt)]
        stopped.set()
        return result

    monkeypatch.setattr(_CatalogPublisher, 'publish_revision', committed_then_cancelled)
    result = app._import_file(workspace, source, _request_id=request_id,
                              cancelled=stopped.is_set, **metadata)
    assert stopped.is_set() and result['state'] == 'complete'
    recovered = worker._reconcile_publication(
        app.root, request_id, attempt, workspace=workspace, timeout=1.0)
    assert recovered['state'] == 'complete' and recovered['id'] == result['id']
    assert len(LibraryApp(app.root).search(workspace, 'railway', collection=collection)) == 1


@pytest.mark.parametrize('kind', ('txt', 'docx'))
def test_terminal_replay_still_runs_full_checks_without_a_new_publication(tmp_path, monkeypatch, kind):
    app, workspace, _, source, metadata = _selected(tmp_path, kind)
    first = app._import_file(workspace, source, **metadata)
    real_check = Storage._check
    checks = []

    def full_check(storage):
        real_check(storage)
        checks.append(storage.db.in_transaction)

    def forbidden(*args, **kwargs):
        raise AssertionError('A terminal replay must not invoke the fresh checked-commit operation.')

    monkeypatch.setattr(Storage, '_check', full_check)
    monkeypatch.setattr(_CatalogPublisher, 'publish_checked_revision', forbidden)
    replay = app._import_file(workspace, source, **metadata)
    assert replay['state'] == 'complete' and replay['id'] == first['id']
    assert len(checks) >= 2 and all(checks)
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
