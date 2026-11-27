"""Real isolated imports retain truthful outcomes across child termination.

Only the trusted child bootstrap adds a test barrier. The actual importer,
SQLite publication, supervisor stop and request persistence remain in use.
"""
from pathlib import Path
import textwrap
import time

import pytest
from docx import Document

from carr_library.application import LibraryApp
from carr_library.ingest import worker


def source_case(tmp_path, kind='docx'):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic publication')
    collection = app.create_collection(workspace, 'Requested collection')
    source = tmp_path / ('Оригінал Ω.' + kind)
    if kind == 'docx':
        document = Document()
        document.add_paragraph('Exact railway source. Український оригінал.')
        document.save(source)
    elif kind == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(source))
        canvas.drawString(72, 700, 'Exact railway source.')
        canvas.save()
    elif kind == 'epub':
        from tests.e2e.test_epub_import import write_epub
        write_epub(source)
    else:
        source.write_bytes('Exact railway source.\r\nУкраїнський оригінал.'.encode('utf-8'))
    metadata = dict(title='Exact source', work_id='requested-work', edition_id='requested-edition',
                    authors=('Synthetic Author',), collections=(collection,))
    return app, workspace, collection, source, metadata


def child_barrier(monkeypatch, marker, *, point='after-publication', crash=False):
    original_popen = worker.subprocess.Popen
    launches = []

    def launch(command, *args, **kwargs):
        command = list(command)
        index = command.index('-c') + 1
        assert "runpy.run_module('carr_library.ingest.worker'" in command[index]
        bootstrap = textwrap.dedent(f'''
            import sys, runpy, os, time
            from pathlib import Path
            sys.path[:0] = sys.argv[1:]
            from carr_library.application import LibraryApp, _CatalogPublisher
            marker = Path({str(marker)!r})
            def barrier():
                marker.write_text('actual publication boundary', encoding='utf-8')
                if {crash!r}:
                    os._exit(73)
                time.sleep(30)
            original = _CatalogPublisher.publish_revision
            def publish(self, *args, **kwargs):
                if {point!r} == 'before-publication':
                    barrier()
                result = original(self, *args, **kwargs)
                if {point!r} == 'after-publication':
                    barrier()
                return result
            _CatalogPublisher.publish_revision = publish
            original_import = LibraryApp._import_file
            def import_file(self, *args, **kwargs):
                if {point!r} == 'before-import':
                    barrier()
                result = original_import(self, *args, **kwargs)
                if {point!r} == 'after-import':
                    barrier()
                return result
            LibraryApp._import_file = import_file
            runpy.run_module('carr_library.ingest.worker', run_name='__main__')
        ''')
        command[index] = bootstrap
        process = original_popen(command, *args, **kwargs)
        launches.append(process)
        return process

    monkeypatch.setattr(worker.subprocess, 'Popen', launch)
    return launches


@pytest.mark.parametrize('outcome', ['normal', 'prelaunch-cancel', 'precommit-cancel',
                                    'postcommit-cancel', 'postcommit-timeout', 'postcommit-crash'])
@pytest.mark.parametrize('kind', ['docx', 'pdf', 'epub', 'txt'])
def test_actual_isolated_import_publication_outcome(tmp_path, monkeypatch, outcome, kind):
    app, workspace, collection, source, metadata = source_case(tmp_path, kind)
    original_bytes = source.read_bytes()
    marker = tmp_path / 'child-boundary'
    if outcome != 'normal':
        launches = child_barrier(monkeypatch, marker,
            point='before-publication' if outcome == 'precommit-cancel' else 'after-publication',
            crash=outcome == 'postcommit-crash')
    else:
        launches = []
    if outcome == 'postcommit-timeout':
        clock = time.monotonic
        monkeypatch.setattr(worker.time, 'monotonic', lambda: clock() + (60 if marker.exists() else 0))
    cancelled = lambda: outcome == 'prelaunch-cancel' or (
        outcome in ('precommit-cancel', 'postcommit-cancel') and marker.exists())

    if kind == 'txt':
        # Folder import uses the same durable request supervisor for TXT.
        from carr_library.extract.page_ocr import Config
        planned = worker.prepare_request(app.root, workspace, source,
            dict(language=None, edition=None, volume=None, **metadata), Config())
        result = app.resume_import(workspace, planned['request_id'], timeout=15, cancelled=cancelled)
    else:
        result = app.import_file(workspace, source, timeout=15, cancelled=cancelled, **metadata)

    committed = outcome not in ('prelaunch-cancel', 'precommit-cancel')
    assert result['state'] == ('complete' if committed else 'cancelled'), result
    assert worker.load_request(app.root, result['request_id'])['state'] == result['state']
    assert source.read_bytes() == original_bytes
    assert all(process.poll() is not None for process in launches)
    if outcome == 'prelaunch-cancel':
        assert not launches and not marker.exists()
    elif outcome != 'normal':
        assert marker.exists()
    reopened = LibraryApp(app.root)
    if committed:
        hits = reopened.search(workspace, 'railway', collection=collection)
        assert len(hits) == (2 if kind == 'epub' else 1)
        assert all(hit.revision == result['id'] and hit.verified == (kind == 'txt') for hit in hits)
        assert result['verified'] == (kind == 'txt')
        with reopened.read(workspace) as db:
            assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
    else:
        assert reopened.browse(workspace) == ()


def test_cancelled_replay_does_not_claim_an_existing_publication_as_new_work(tmp_path, monkeypatch):
    app, workspace, original_collection, source, metadata = source_case(tmp_path)
    initial = app.import_file(workspace, source, **metadata)
    requested = app.create_collection(workspace, 'Uncommitted replay collection')
    marker = tmp_path / 'replay-before-import'
    child_barrier(monkeypatch, marker, point='before-import')

    result = app.import_file(workspace, source, cancelled=marker.exists,
                            **dict(metadata, collections=(requested,)))

    assert result['state'] == 'cancelled'
    assert app.browse(workspace, collection=requested) == ()
    hit, = app.search(workspace, 'railway source', collection=original_collection)
    assert hit.revision == initial['id']


def test_stopped_replay_reports_new_membership_that_really_committed(tmp_path, monkeypatch):
    app, workspace, _, source, metadata = source_case(tmp_path)
    initial = app.import_file(workspace, source, **metadata)
    requested = app.create_collection(workspace, 'New committed membership')
    marker = tmp_path / 'after-replay-commit'
    child_barrier(monkeypatch, marker, point='after-import')

    result = app.import_file(workspace, source, cancelled=marker.exists,
                            **dict(metadata, collections=(requested,)))

    assert marker.exists() and result['state'] == 'complete'
    assert result['id'] == initial['id']
    assert len(app.browse(workspace, collection=requested)) == 1


def test_prior_retained_request_does_not_turn_prework_cancellation_into_success(tmp_path, monkeypatch):
    app, workspace, _, source, metadata = source_case(tmp_path)
    initial = app.import_file(workspace, source, **metadata)
    record = worker.load_request(app.root, initial['request_id'])
    record['state'] = 'cancelled'
    record.pop('result')
    worker._store(app.root, initial['request_id'], record)
    marker = tmp_path / 'existing-source-before-replay'
    child_barrier(monkeypatch, marker, point='before-import')

    result = app.resume_import(workspace, initial['request_id'], cancelled=marker.exists)

    assert marker.exists() and result['state'] == 'cancelled'
    assert worker.load_request(app.root, initial['request_id'])['state'] == 'cancelled'
    assert len(app.browse(workspace)) == 1


def test_corrupt_retained_bytes_cannot_be_promoted_to_success(tmp_path, monkeypatch):
    app, workspace, _, source, metadata = source_case(tmp_path)
    marker = tmp_path / 'after-publication'
    child_barrier(monkeypatch, marker)
    tampered = False

    def cancelled():
        nonlocal tampered
        if marker.exists() and not tampered:
            retained, = app.assets_dir.glob('*.docx')
            retained.write_bytes(b'damaged synthetic original')
            tampered = True
        return tampered

    result = app.import_file(workspace, source, cancelled=cancelled, **metadata)

    assert tampered and result['state'] != 'complete'
    assert 'recovered_publication' not in result
    assert worker.load_request(app.root, result['request_id'])['state'] == result['state']


@pytest.mark.parametrize('damage', ['replacement-catalog', 'missing-index'])
def test_catalog_change_after_actual_child_stop_cannot_authorize_completion(tmp_path, monkeypatch, damage):
    from contextlib import closing
    import os
    import sqlite3

    app, workspace, _, source, metadata = source_case(tmp_path)
    marker = tmp_path / 'committed-before-catalog-change'
    child_barrier(monkeypatch, marker)
    stop, changed = worker._stop, []

    def stop_then_change(process):
        stop(process)
        assert process.poll() is not None and marker.exists()
        if changed:
            return
        with closing(sqlite3.connect(app.database)) as db:
            assert db.execute('SELECT count(*) FROM sealed').fetchone()[0] == 1
            if damage == 'replacement-catalog':
                replacement = tmp_path / 'replacement.sqlite'
                with closing(sqlite3.connect(replacement)) as copy:
                    db.backup(copy)
            else:
                db.execute('DELETE FROM indexed')
                db.commit()
        if damage == 'replacement-catalog':
            original_identity = app.database.stat()
            os.replace(replacement, app.database)
            assert not os.path.samestat(original_identity, app.database.stat())
        changed.append(damage)

    monkeypatch.setattr(worker, '_stop', stop_then_change)
    result = app.import_file(workspace, source, cancelled=marker.exists, **metadata)
    assert changed == [damage]
    assert result['state'] == 'cancelled' and 'recovered_publication' not in result
    assert worker.load_request(app.root, result['request_id'])['state'] == 'cancelled'


@pytest.mark.parametrize('kind', ['txt', 'pdf'])
def test_actual_postcommit_crash_survives_complete_backup_with_pending_journal(tmp_path, monkeypatch, kind):
    from contextlib import closing
    import sqlite3
    from carr_library.extract.page_ocr import Config
    from carr_library.library_backup import create_backup, restore_backup

    app, workspace, _, source, metadata = source_case(tmp_path, kind)
    original = source.read_bytes()
    marker = tmp_path / 'committed-before-process-crash'
    child_barrier(monkeypatch, marker, crash=True)
    if kind == 'txt':
        result = worker.run_request(app.root, workspace, source,
            dict(language=None, edition=None, volume=None, **metadata), Config(), _library=app)
    else:
        result = app.import_file(workspace, source, **metadata)
    assert result['state'] == 'complete' and result['recovered_publication'] is True
    assert result['worker_state'] == 'failed'
    with closing(sqlite3.connect(app.root / 'imports.sqlite')) as journal:
        assert journal.execute('SELECT state FROM job WHERE id=?', (result['id'],)).fetchone()[0] == 'pending'
    backup = tmp_path / 'complete-backup'
    digest = create_backup(app, backup)
    source.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    resumed = restored.resume_import(workspace, result['request_id'])
    assert resumed == result and resumed['verified'] is (kind == 'txt')
    assert restored.assets.resolve(result['asset']).read_bytes() == original
    hit, = restored.search(workspace, 'railway source')
    assert hit.revision == result['id'] and hit.verified is (kind == 'txt')
