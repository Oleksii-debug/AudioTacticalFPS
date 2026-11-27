"""Actual late-stopped publication remains passively restorable in common4."""
from hashlib import sha256
from pathlib import Path
import runpy
import sqlite3
import subprocess

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest.worker import load_request
from carr_library.library_backup import create_backup, restore_backup, verify_backup


# Reuse the actual child-publication barrier from the reviewed outer boundary
# suite. The importer, SQLite publication and process supervisor remain real.
_boundary = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'ingest' /
                              'test_worker_publication_boundary.py'))


@pytest.mark.parametrize('kind', ['txt', 'pdf'])
@pytest.mark.parametrize('stop', ['cancel', 'crash'])
def test_recovered_publication_keeps_pending_journal_and_restores_without_execution(
        tmp_path, monkeypatch, kind, stop):
    app = LibraryApp(tmp_path / 'Бібліотека čítanie')
    workspace = app.create_workspace('Late publication backup')
    source = _boundary['source_file'](tmp_path, kind)
    marker = tmp_path / 'committed'
    processes = _boundary['publication_barrier'](monkeypatch, marker, crash=stop == 'crash')
    cancelled = lambda: stop == 'cancel' and marker.exists()
    if kind == 'txt':
        batch = app.plan_import_batch(workspace, [source])
        completed = app.run_import_batch(workspace, batch['batch_id'], cancelled=cancelled, timeout=30)
        result = completed['rows'][0]['result']
    else:
        result = app.import_file(workspace, source, cancelled=cancelled, timeout=30)
    assert result['state'] == 'complete' and result['recovered_publication'] is True
    assert result['worker_state'] == ('cancelled' if stop == 'cancel' else 'failed')
    assert len(processes) == 1 and processes[0].poll() is not None
    request_id = result['request_id']
    request = load_request(app.root, request_id)
    assert request['state'] == 'complete' and request['result'] == result
    with sqlite3.connect(app.root / 'imports.sqlite') as journal:
        assert journal.execute('SELECT state FROM job WHERE id=?', (result['id'],)).fetchone() == ('pending',)
        assert journal.execute('SELECT count(*) FROM checkpoint WHERE job=?', (result['id'],)).fetchone() == (1,)
    source.unlink()
    backup = tmp_path / 'backup'
    target = tmp_path / 'restored'

    def no_process(*args, **kwargs):
        raise AssertionError('Recovered import backup, restore and cached completion must not execute a worker.')

    with monkeypatch.context() as passive:
        passive.setattr(subprocess, 'Popen', no_process)
        checksum = create_backup(app, backup)
        manifest = verify_backup(backup, checksum)
        assert manifest['pending']['version'] == 1
        before = {p.relative_to(backup): sha256(p.read_bytes()).hexdigest()
                  for p in backup.rglob('*') if p.is_file()}
        restored = LibraryApp(restore_backup(backup, target, checksum))
        assert {p.relative_to(backup): sha256(p.read_bytes()).hexdigest()
                for p in backup.rglob('*') if p.is_file()} == before
        after = load_request(restored.root, request_id)
        assert after['state'] == 'complete' and after['result'] == result
        assert after['source'] == str(restored.assets.resolve(request['asset_sha256']))
        assert after['data_dir'] == str(restored.root)
        assert (restored.root / 'imports.sqlite').read_bytes() == (backup / 'pending/imports.sqlite').read_bytes()
        assert restored.resume_import(workspace, request_id) == result
        with sqlite3.connect(restored.root / 'imports.sqlite') as journal:
            assert journal.execute('SELECT state FROM job WHERE id=?', (result['id'],)).fetchone() == ('pending',)
        with restored.read(workspace) as db:
            assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
            assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 1
