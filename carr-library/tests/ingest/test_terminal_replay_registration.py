"""Completed ingestion cannot bypass source identity or the catalog write gate."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest.pipeline import canonical
from carr_library.storage.revisions import Storage


@pytest.fixture(params=['txt', 'pdf'])
def imported(tmp_path, request):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Terminal replay')
    first = app.create_collection(workspace, 'First')
    requested = app.create_collection(workspace, 'Requested')
    source = tmp_path / ('source.' + request.param)
    if request.param == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(source))
        canvas.drawString(72, 700, 'Synthetic railway history')
        canvas.save()
    else:
        source.write_bytes(b'Synthetic railway history\r\n')
    metadata = dict(title='Original title', authors=('Synthetic author',),
                    work_id='stable-work', edition_id='stable-edition', language='en')
    # The real worker implementation runs in this process to allow an injected
    # consistency rejection; actual PDF parsing/runtime validation is retained.
    initial = app._import_file(workspace, source, collections=(first,), **metadata)
    assert initial['state'] == 'complete'
    return app, workspace, first, requested, source, metadata, initial


def test_terminal_replay_registration_is_invisible_until_accepted(imported, monkeypatch):
    app, workspace, first, requested, source, metadata, initial = imported
    original_bytes = source.read_bytes()
    original_check = Storage._check
    observed = []

    def reject(storage):
        original_check(storage)
        args = (workspace, requested)
        sql = 'SELECT count(*) FROM catalog_membership WHERE workspace=? AND collection=?'
        if storage.db.execute(sql, args).fetchone()[0]:
            with closing(sqlite3.connect(app.database)) as observer:
                observed.append(observer.execute(sql, args).fetchone()[0])
            raise RuntimeError('replay publication rejected')

    with monkeypatch.context() as patch:
        patch.setattr(Storage, '_check', reject)
        with pytest.raises(RuntimeError, match='replay publication rejected'):
            app._import_file(workspace, source, collections=(requested,), **metadata)
    assert observed == [0]
    assert app.browse(workspace, collection=requested) == ()
    replay = app._import_file(workspace, source, collections=(requested,), **metadata)
    assert replay['id'] == initial['id'] and replay['state'] == 'complete'
    assert app.browse(workspace, collection=first) == app.browse(workspace, collection=requested)
    assert source.read_bytes() == original_bytes
    hit, = app.search(workspace, 'railway', collection=requested)
    assert hit.revision == initial['id']
    assert hit.verified is (source.suffix == '.txt')


def test_superseded_replay_uses_current_metadata_without_reactivating_old_source(imported):
    app, workspace, first, requested, source, metadata, initial = imported
    old, = app.search(workspace, 'railway', collection=first)
    current_metadata = dict(metadata, title='Current title')
    current = app.update_metadata(workspace, metadata['edition_id'], title='Current title',
                                  authors=metadata['authors'], language=metadata['language'])
    with pytest.raises(ValueError, match='work identity conflict'):
        app._import_file(workspace, source, collections=(requested,), **metadata)
    assert app.browse(workspace, collection=requested) == ()
    replay = app._import_file(workspace, source, collections=(requested,), **current_metadata)
    assert replay['state'] == 'superseded' and replay['id'] == initial['id']
    active, = app.search(workspace, 'railway', collection=requested)
    assert active.revision == current and active.title == 'Current title'
    historical = app.preview(workspace, old.ref, old.revision)
    current_preview = app.preview(workspace, active.ref, active.revision)
    assert historical.startswith('Original title\n') and current_preview.startswith('Current title\n')
    assert historical.split('\n', 1)[1] == current_preview.split('\n', 1)[1]
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 2
        assert db.execute('SELECT revision FROM active WHERE workspace=? AND edition=?',
                          (workspace, metadata['edition_id'])).fetchone()[0] == current


def test_cancelled_completed_replay_does_not_add_memberships(imported):
    app, workspace, first, requested, source, metadata, initial = imported
    with pytest.raises(InterruptedError, match='cancelled'):
        app._import_file(workspace, source, collections=(requested,),
                         cancelled=lambda: True, **metadata)
    assert app.browse(workspace, collection=requested) == ()
    assert app.search(workspace, 'railway', collection=first)[0].revision == initial['id']


def test_replay_rejects_rehashed_checkpoint_text_before_registration(imported):
    app, workspace, first, requested, source, metadata, initial = imported
    with closing(sqlite3.connect(app.root / 'imports.sqlite', isolation_level=None)) as journal:
        raw, = journal.execute('SELECT payload FROM checkpoint WHERE job=? AND ordinal=1',
                               (initial['id'],)).fetchone()
        record = json.loads(raw)
        record['raw_text'] = 'Forged but checksummed checkpoint'
        changed = canonical(record)
        journal.execute('UPDATE checkpoint SET payload=?,digest=? WHERE job=? AND ordinal=1',
                        (changed, sha256(changed.encode()).hexdigest(), initial['id']))
    with pytest.raises(ValueError, match='published page mismatch'):
        app._import_file(workspace, source, collections=(requested,), **metadata)
    assert app.browse(workspace, collection=requested) == ()
    assert app.search(workspace, 'railway', collection=first)[0].revision == initial['id']


@pytest.mark.parametrize('state', ['complete', 'superseded'])
def test_forged_terminal_journal_without_published_source_does_not_register(tmp_path, state):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Incomplete publication')
    requested = app.create_collection(workspace, 'Requested')
    source = tmp_path / 'source.txt'
    source.write_text('Original railway text', encoding='utf-8')
    metadata = dict(work_id='work', edition_id='edition', title='Original')
    cancelled = app.import_file(workspace, source, cancelled=lambda: True, **metadata)
    assert cancelled['state'] == 'cancelled'
    with closing(sqlite3.connect(app.root / 'imports.sqlite', isolation_level=None)) as journal:
        journal.execute('UPDATE job SET state=? WHERE id=?', (state, cancelled['id']))
    with pytest.raises(ValueError, match='missing its sealed source'):
        app.import_file(workspace, source, collections=(requested,), **metadata)
    assert app.browse(workspace) == ()
    assert app.search(workspace, 'railway') == ()
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
