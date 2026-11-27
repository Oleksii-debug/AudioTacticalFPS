"""Independent PR150 competing-publication matrix against the selected V5.

Real isolated children, real SQLite publication and real imported source files.
Only native process timing and a non-authoritative legacy sidecar hint are injected.
"""
from pathlib import Path
import runpy
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker
from carr_library.operations import ResourceBroker

helpers = runpy.run_path(str(Path(__file__).with_name('test_worker_publication_boundary.py')))
source_file = helpers['source_file']


def receipt_commit_barrier(monkeypatch, marker):
    """Pause the real catalog transaction after validation, before COMMIT."""
    popen = worker.subprocess.Popen
    def launch(command, *args, **kwargs):
        if isinstance(command, list) and '-c' in command:
            index = command.index('-c') + 1
            anchor = "runpy.run_module('carr_library.ingest.worker',run_name='__main__')"
            if anchor in command[index]:
                body = '\n'.join([
                    '@contextmanager',
                    'def before_commit(self):',
                    '    with original(self):',
                    '        yield',
                    "        if self.db.execute('PRAGMA user_version').fetchone()[0] == 5 and self.db.execute('SELECT count(*) FROM publication_receipt').fetchone()[0]:",
                    '            self._check()',
                    '            Path(' + repr(str(marker)) + ').write_text("receipt and source staged", encoding="utf-8")',
                    '            time.sleep(60)',
                ])
                hook = ('import time; from pathlib import Path; from contextlib import contextmanager; '
                        'from carr_library.storage.revisions import Storage; original=Storage.transaction; '
                        'exec(' + repr(body) + '); Storage.transaction=before_commit; ')
                command = list(command)
                command[index] = command[index].replace(anchor, hook + anchor)
        return popen(command, *args, **kwargs)
    monkeypatch.setattr(worker.subprocess, 'Popen', launch)


@pytest.mark.parametrize('kind', ('txt', 'pdf', 'docx'))
@pytest.mark.parametrize('competitor', ('direct', 'second-request', 'forged-sidecar'))
def test_competing_publication_never_borrows_current_attempt(tmp_path, monkeypatch, kind, competitor):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Independent competing publication audit')
    source = source_file(tmp_path, kind)
    original = source.read_bytes()
    metadata = dict(title='One retained source', authors=('Synthetic author',), language='uk',
                    edition=None, volume=None, collections=(),
                    work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = worker.prepare_request(app.root, workspace, source, metadata, Config())
    marker = tmp_path / 'uncommitted'
    real_popen, real_stop, real_invoke = worker.subprocess.Popen, worker._stop, worker._invoke
    real_reconcile = worker._published_after_stop
    receipt_commit_barrier(monkeypatch, marker)
    attempts, completed, stopped = {}, [], []

    def capture_attempt(message, **kwargs):
        attempts[message['request_id']] = message['attempt_id']
        return real_invoke(message, **kwargs)

    def observed_stop(process):
        real_stop(process)
        stopped.append(process)

    def publish_before_reconciliation(owner, initial, attempt_id):
        if initial['request_id'] != planned['request_id'] or not marker.exists() or completed:
            return real_reconcile(owner, initial, attempt_id)
        # The outer supervisor still owns its reservation throughout native
        # cleanup. Interleave the independent publication after release and
        # immediately before this attempt's unchanged receipt reconciliation.
        assert stopped and all(process.poll() is not None for process in stopped)
        assert ResourceBroker.for_app(app.root).snapshot()['leases'] == []
        completed.append(None)
        with app.read(workspace) as db:
            assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
        with monkeypatch.context() as clean:
            clean.setattr(worker.subprocess, 'Popen', real_popen)
            if competitor == 'direct':
                completed[0] = app._import_file(workspace, source, config=Config(), **metadata)
            else:
                completed[0] = worker.run_request(app.root, workspace, source, metadata, Config(), timeout=30)
        assert completed[0]['state'] == 'complete', completed
        if competitor == 'forged-sidecar':
            stopped_record = worker.load_request(app.root, planned['request_id'])
            with app.read(workspace) as db:
                receipt = db.execute('SELECT * FROM publication_receipt').fetchone()
                asset = db.execute('SELECT digest,mime,size FROM catalog_asset').fetchone()
            stopped_record['attempt'] = attempts[planned['request_id']]
            stopped_record['publication_candidate'] = dict(
                attempt=stopped_record['attempt'], revision=receipt['revision'], sha256=receipt['digest'],
                completed=1, manifest=dict(sha256=asset[0], mime=asset[1], size=asset[2]))
            worker._store(app.root, planned['request_id'], stopped_record)
        return real_reconcile(owner, initial, attempt_id)

    monkeypatch.setattr(worker, '_invoke', capture_attempt)
    monkeypatch.setattr(worker, '_stop', observed_stop)
    monkeypatch.setattr(worker, '_published_after_stop', publish_before_reconciliation)
    result = worker.run_request(app.root, workspace, source, metadata, Config(),
                                cancelled=marker.exists, timeout=30, request_id=planned['request_id'])
    assert marker.exists() and completed[0]['state'] == 'complete', completed
    assert result['state'] == 'cancelled' and 'recovered_publication' not in result, result
    assert source.read_bytes() == original
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM publication_receipt WHERE request_id=?',
                          (planned['request_id'],)).fetchone()[0] == 0
        receipt = db.execute('SELECT request_id,attempt_id FROM publication_receipt').fetchone()
        if competitor == 'direct':
            assert receipt is None
        else:
            assert tuple(receipt) == (completed[0]['request_id'], attempts[completed[0]['request_id']])

