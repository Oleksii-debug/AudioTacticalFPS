"""The immutable transaction receipt rejects coherently altered source rows.

The actual import child commits and then pauses. After the production supervisor
reaps it, the fixture changes raw text, its page digest, the search index and the
mutable sidecar hash together. It restores the exact SQL schema and leaves the
committed transaction receipt intact, so rejection must come from rechecking the
publication digest, not from detecting a missing trigger or an incomplete index.
"""
from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
import subprocess

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker
from carr_library.storage.revisions import (
    CURRENT_SCHEMA_VERSION, Storage, _expected_signature, _schema_signature,
)


def test_recovery_rechecks_immutable_receipt_after_coherent_source_tamper(
        tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Immutable publication receipt')
    source = tmp_path / 'source.txt'
    source.write_text('Original synthetic source.\n', encoding='utf-8')
    marker = tmp_path / 'committed'
    popen = subprocess.Popen
    bootstrap = f"""
import time
from pathlib import Path
from carr_library.application import _CatalogPublisher
_publish = _CatalogPublisher.publish_revision
def _after_commit(self, *args, **kwargs):
    _publish(self, *args, **kwargs)
    Path({str(marker)!r}).write_text('committed', encoding='utf-8')
    time.sleep(20)
_CatalogPublisher.publish_revision = _after_commit
"""

    def launch(command, **options):
        # Windows taskkill and any other helper keep their production invocation.
        if not isinstance(command, (list, tuple)) or '-c' not in command:
            return popen(command, **options)
        copied = list(command)
        index = copied.index('-c') + 1
        if (index >= len(copied)
                or "runpy.run_module('carr_library.ingest.worker'" not in copied[index]):
            return popen(command, **options)
        copied[index] = ('import sys,runpy\nsys.path[:0]=sys.argv[1:]\n' + bootstrap
                         + "\nrunpy.run_module('carr_library.ingest.worker',run_name='__main__')")
        return popen(copied, **options)

    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    stop = worker._stop
    changed = []

    def reap_then_change(process):
        stop(process)
        assert process.poll() is not None
        if changed or not marker.exists():
            return
        request_path, = (app.root / 'import-requests').glob('*.json')
        record = worker.load_request(app.root, request_path.stem)
        revision = record['publication_candidate']['revision']
        with closing(sqlite3.connect(app.database)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA recursive_triggers=ON')
            before = tuple(db.execute(
                'SELECT * FROM import_publication WHERE revision=?',
                (revision,)).fetchone())
            assert before[1:3] == (record['request_id'], record['attempt'])
            trigger = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='page_bu'"
            ).fetchone()[0]
            page_id, = db.execute(
                'SELECT id FROM page WHERE revision=? AND ordinal=1',
                (revision,)).fetchone()
            replacement = 'Coherently altered synthetic source.\n'
            # This deliberately models damaged/restored storage. The exact
            # trigger is restored before the verifier sees the snapshot.
            db.execute('DROP TRIGGER page_bu')
            db.execute('UPDATE page SET raw=?,digest=? WHERE id=?',
                       (replacement, sha256(replacement.encode('utf-8')).hexdigest(),
                        page_id))
            db.execute('DELETE FROM indexed WHERE id=?', (page_id,))
            db.execute('INSERT INTO indexed(id,raw) VALUES(?,?)',
                       (page_id, replacement))
            db.execute(trigger)
            db.commit()
            assert _schema_signature(db) == _expected_signature(CURRENT_SCHEMA_VERSION)
            assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
            assert tuple(db.execute(
                'SELECT * FROM import_publication WHERE revision=?',
                (revision,)).fetchone()) == before
            record['result']['pages_sha256'] = worker._pages_digest(
                tuple(row) for row in db.execute(
                    'SELECT ordinal,physical,printed,verified,digest FROM page '
                    'WHERE revision=? ORDER BY ordinal', (revision,)))
            assert worker._publication_state(db, record, revision)['sha256'] != before[3]
        worker._store(app.root, record['request_id'], record)
        # All preexisting catalog/index/page checks still accept this coherent
        # state. Only the immutable transaction digest can reject its history.
        with closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
            storage.check()
        retained = app.assets.resolve(record['asset_sha256'])
        assert sha256(retained.read_bytes()).hexdigest() == record['asset_sha256']
        changed.append(record['request_id'])

    monkeypatch.setattr(worker, '_stop', reap_then_change)
    metadata = dict(title='Synthetic publication', authors=[], language=None,
                    edition=None, volume=None, collections=[],
                    work_id='receipt-work', edition_id='receipt-edition')
    result = worker.run_request(app.root, workspace, source, metadata, Config(),
                                cancelled=marker.exists, timeout=15.0)
    assert changed == [result['request_id']]
    assert result['state'] == 'failed', result
    assert not result.get('publication_recovered')
    assert not result.get('recovered')
    assert worker.load_request(app.root, result['request_id'])['state'] == 'failed'
