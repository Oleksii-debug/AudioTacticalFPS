"""Independent compatibility fixtures for accepted canonical catalog v5.

The v5 addendum and legacy digest formula below are frozen from canonical
c9cdacd93dd57e8644889761a44b01b59c4fda20 (revisions blob 0e326329 and worker
blob e219ff74). They intentionally do not import the new receipt writer or
derive the accepted schema from its current SCHEMA_V5.
"""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker
from carr_library.library_backup import restore_backup
from carr_library.storage.revisions import Storage
from carr_library.storage.schema_identity import expected_schema_fingerprint


V4_FINGERPRINT = 'cd1006b2764a3c494aaced5ed9a0515ab3884eda7bed4f5e081d190e4474c409'

ACCEPTED_V5_SQL = '''
CREATE TABLE import_publication(
 revision TEXT PRIMARY KEY NOT NULL REFERENCES sealed(revision),
 request_id TEXT NOT NULL CHECK(length(request_id)=32 AND request_id NOT GLOB '*[^0-9a-f]*'),
 attempt TEXT NOT NULL CHECK(length(attempt)=32 AND attempt NOT GLOB '*[^0-9a-f]*'),
 sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
 UNIQUE(request_id,attempt)
);
CREATE TRIGGER import_publication_no_update BEFORE UPDATE ON import_publication
BEGIN SELECT RAISE(ABORT,'immutable import publication'); END;
CREATE TRIGGER import_publication_no_delete BEFORE DELETE ON import_publication
BEGIN SELECT RAISE(ABORT,'immutable import publication'); END;
CREATE TRIGGER import_publication_no_replace BEFORE INSERT ON import_publication
WHEN EXISTS(SELECT 1 FROM import_publication WHERE revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'import publication already exists'); END;
'''

PROTOTYPE_V5_SQL = '''
CREATE TABLE publication_receipt(
 request_id TEXT NOT NULL CHECK(length(request_id)=32 AND request_id NOT GLOB '*[^0-9a-f]*'),
 attempt_id TEXT NOT NULL CHECK(length(attempt_id)=32 AND attempt_id NOT GLOB '*[^0-9a-f]*'),
 workspace TEXT NOT NULL, edition TEXT NOT NULL, revision TEXT NOT NULL UNIQUE,
 asset TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(request_id,attempt_id),
 FOREIGN KEY(workspace,edition,asset) REFERENCES catalog_edition(workspace,id,asset),
 FOREIGN KEY(workspace,edition,revision) REFERENCES revision(workspace,edition,id)
   DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(revision) REFERENCES sealed(revision) DEFERRABLE INITIALLY DEFERRED
);
CREATE TRIGGER publication_receipt_bi BEFORE INSERT ON publication_receipt
 WHEN EXISTS(SELECT 1 FROM sealed WHERE revision=new.revision)
 BEGIN SELECT RAISE(ABORT,'receipt requires a new publication'); END;
CREATE TRIGGER publication_receipt_bu BEFORE UPDATE ON publication_receipt
 BEGIN SELECT RAISE(ABORT,'immutable publication receipt'); END;
CREATE TRIGGER publication_receipt_bd BEFORE DELETE ON publication_receipt
 BEGIN SELECT RAISE(ABORT,'immutable publication receipt'); END;
'''


def _canonical(value):
    # Exact accepted legacy worker JSON representation; not the new proof helper.
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _signature(db):
    return tuple(tuple(row) for row in db.execute(
        'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'))


def _fingerprint(db):
    payload = dict(format='carr-sqlite-schema-v1',
                   user_version=db.execute('PRAGMA user_version').fetchone()[0],
                   sqlite_master=_signature(db))
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True,
                     separators=(',', ':')).encode('utf-8')
    return sha256(raw).hexdigest()


def _frozen_database(root, addendum=ACCEPTED_V5_SQL):
    root.mkdir()
    path = root / 'library.sqlite'
    with closing(Storage(path, schema_version=4)) as storage:
        # Do not let a changed historical schema become the expected fixture.
        assert _fingerprint(storage.db) == V4_FINGERPRINT
    with closing(sqlite3.connect(path, isolation_level=None)) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA recursive_triggers=ON')
        db.executescript('BEGIN IMMEDIATE;\n' + addendum
                         + '\nPRAGMA user_version=5;\nCOMMIT;\n')
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
    return path


def _legacy_state(db, record, revision):
    """The c9 legacy digest preimage, copied independently of the new verifier."""
    workspace, metadata = record['workspace'], record['metadata']
    row = db.execute('SELECT workspace,edition,asset,config FROM revision WHERE id=?',
                     (revision,)).fetchone()
    catalog = db.execute(
        'SELECT e.work,e.asset,a.mime,a.size,e.language,e.edition,e.volume,w.title '
        'FROM catalog_edition e JOIN catalog_asset a ON a.digest=e.asset '
        'JOIN catalog_work w ON w.workspace=e.workspace AND w.id=e.work '
        'WHERE e.workspace=? AND e.id=?',
        (workspace, metadata['edition_id'])).fetchone()
    source = db.execute(
        'SELECT author,title,volume,edition_label FROM source_metadata '
        'WHERE workspace=? AND edition=? AND revision=?',
        (workspace, metadata['edition_id'], revision)).fetchone()
    authors = tuple(row[0] for row in db.execute(
        'SELECT name FROM catalog_author WHERE workspace=? AND work=? ORDER BY position',
        (workspace, metadata['work_id'])))
    identity = {key: record.get(key) for key in (
        'version', 'request_id', 'data_dir', 'workspace', 'source',
        'metadata', 'config', 'expected_manifest')}
    assert row is not None and catalog is not None and source is not None
    digest = sha256(_canonical([identity, list(row), list(catalog),
                                list(source), authors]).encode('utf-8'))
    completed = 0
    for page in db.execute(
            'SELECT ordinal,physical,printed,verified,raw,digest '
            'FROM page WHERE revision=? ORDER BY ordinal', (revision,)):
        completed += 1
        assert page[0] == completed
        assert sha256(page[4].encode('utf-8')).hexdigest() == page[5]
        digest.update(b'\n')
        digest.update(_canonical(list(page)).encode('utf-8'))
    assert completed > 0
    return dict(sha256=digest.hexdigest(), completed=completed,
                manifest=dict(sha256=catalog[1], mime=catalog[2], size=catalog[3]))


def _legacy_published(tmp_path):
    """A genuine old-format receipt over actual catalog/source rows.

    Direct TXT ingestion provides immutable source data without invoking the
    new receipt producer. Only this independent fixture inserts the frozen
    accepted legacy receipt, exactly as the old publication hook did.
    """
    root = tmp_path / 'library'
    _frozen_database(root)
    app = LibraryApp(root)
    workspace = app.create_workspace('Accepted canonical v5')
    source = tmp_path / 'legacy.txt'
    source.write_text('Canonical accepted source. Точний український текст.',
                      encoding='utf-8')
    metadata = dict(title='Accepted source', authors=['Автор'], language='uk',
                    edition=None, volume=None, collections=[],
                    work_id='a' * 32, edition_id='b' * 32)
    request_id, attempt = 'c' * 32, 'd' * 32
    initial = worker._request_record(root, workspace, source, metadata, Config(), request_id)
    worker._store(root, request_id, initial, create=True)
    published = app._import_file(workspace, source, **metadata)
    assert published['state'] == 'complete'
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0
        asset = db.execute('SELECT asset FROM revision WHERE id=?',
                           (published['id'],)).fetchone()[0]
    record = deepcopy(initial)
    record.update(state='running', asset_sha256=asset, job=published['id'], attempt=attempt)
    with app.write(workspace) as db:
        state = _legacy_state(db, record, published['id'])
        db.execute('INSERT INTO import_publication(revision,request_id,attempt,sha256) '
                   'VALUES(?,?,?,?)',
                   (published['id'], request_id, attempt, state['sha256']))
    record['publication_candidate'] = dict(
        attempt=attempt, revision=published['id'], **state)
    worker._store(root, request_id, record)
    return app, workspace, source, initial, attempt, published, record


def test_exact_accepted_v5_schema_opens_without_redefining_history(tmp_path):
    path = _frozen_database(tmp_path / 'accepted')
    with closing(sqlite3.connect(path)) as db:
        before = _signature(db)
        fingerprint = _fingerprint(db)
    with closing(Storage(path, schema_version=5)) as storage:
        storage.check()
        assert _signature(storage.db) == before
        assert _fingerprint(storage.db) == fingerprint
        assert storage.db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0
        assert storage.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='publication_receipt'").fetchone() is None
    assert expected_schema_fingerprint(4) == V4_FINGERPRINT
    assert expected_schema_fingerprint(5) == fingerprint


@pytest.mark.parametrize('historical', [False, True])
def test_authentic_accepted_legacy_receipt_verifies_without_reissuing_it(tmp_path, historical):
    app, workspace, _source, initial, attempt, published, _record = _legacy_published(tmp_path)
    with app.read(workspace) as db:
        before = tuple(db.execute('SELECT * FROM import_publication').fetchone())
    if historical:
        app.update_metadata(workspace, initial['metadata']['edition_id'], title='Later catalog title')
    recovered = worker._published_after_stop(app, initial, attempt)
    assert recovered is not None
    assert recovered['id'] == published['id']
    assert recovered['state'] == ('superseded' if historical else 'complete')
    assert recovered['verified'] is (not historical)
    with app.read(workspace) as db:
        assert tuple(db.execute('SELECT * FROM import_publication').fetchone()) == before


@pytest.mark.parametrize('forgery', ['request-language', 'other-request', 'wrong-attempt'])
def test_legacy_receipt_does_not_authenticate_forged_request_or_attempt(tmp_path, forgery):
    app, workspace, _source, initial, attempt, _published, record = _legacy_published(tmp_path)
    before = deepcopy(record)
    if forgery == 'request-language':
        # Language has no historical source_metadata column. A verifier must
        # bind the request through the stored digest, not infer it from the source.
        initial = deepcopy(initial)
        initial['metadata']['language'] = record['metadata']['language'] = 'en'
        worker._store(app.root, initial['request_id'], record)
    elif forgery == 'other-request':
        # A changed request identity cannot borrow this valid digest by copying
        # an old sidecar candidate. Keep the receipt's actual attempt unchanged.
        initial = deepcopy(initial)
        initial['request_id'] = 'e' * 32
        record['request_id'] = initial['request_id']
        worker._store(app.root, initial['request_id'], record, create=True)
    else:
        attempt = 'f' * 32
    assert worker._published_after_stop(app, initial, attempt) is None
    with app.read(workspace) as db:
        row = db.execute('SELECT request_id,attempt,sha256 FROM import_publication').fetchone()
        assert tuple(row) == ('c' * 32, 'd' * 32, before['publication_candidate']['sha256'])


def test_accepted_v5_backup_restore_preserves_schema_receipt_and_retained_source(tmp_path):
    app, workspace, source, initial, attempt, published, record = _legacy_published(tmp_path)
    with app.read(workspace) as db:
        signature, fingerprint = _signature(db), _fingerprint(db)
        receipt = tuple(db.execute('SELECT * FROM import_publication').fetchone())
    original = source.read_bytes()
    backup = tmp_path / 'backup'
    checksum = app.backup(backup)
    manifest = json.loads((backup / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['schema_version'] == 5 and manifest['schema_fingerprint'] == fingerprint
    source.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', checksum))
    with restored.read(workspace) as db:
        assert _signature(db) == signature and _fingerprint(db) == fingerprint
        assert tuple(db.execute('SELECT * FROM import_publication').fetchone()) == receipt
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
    assert restored.assets.resolve(record['asset_sha256']).read_bytes() == original
    rebased = worker.load_request(restored.root, initial['request_id'])
    assert rebased['data_dir'] == str(restored.root)
    assert rebased['source'] != record['source']
    # Old c9 receipts hashed absolute paths. Restoring the file does not grant a
    # new process authority to assert an old attempt using rewritten identities.
    relocated_initial = deepcopy(initial)
    relocated_initial.update(data_dir=str(restored.root), source=rebased['source'])
    assert worker._published_after_stop(restored, relocated_initial, attempt) is None
    with restored.read(workspace) as db:
        assert tuple(db.execute('SELECT * FROM import_publication').fetchone()) == receipt


@pytest.mark.parametrize('opener', ['storage', 'application'])
def test_incompatible_unreleased_v5_is_rejected_without_relabeling_or_writing(tmp_path, opener):
    path = _frozen_database(tmp_path / 'prototype', PROTOTYPE_V5_SQL)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='schema'):
        if opener == 'storage':
            with closing(Storage(path, schema_version=5)):
                pass
        else:
            LibraryApp(path.parent)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='publication_receipt'").fetchone() is not None
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='import_publication'").fetchone() is None
