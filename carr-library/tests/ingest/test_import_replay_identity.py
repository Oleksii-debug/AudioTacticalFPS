"""Real TXT/PDF replay registration, with immutable source identity and rollback."""
from contextlib import closing
from hashlib import sha256
import json
import sqlite3
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest.pipeline import Ingest, IngestError, canonical
from carr_library.storage.revisions import Page, Storage


@pytest.fixture(params=('txt', 'pdf'))
def imported(request, tmp_path):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Synthetic source library')
    other = app.create_workspace('Independent library')
    first = app.create_collection(workspace, 'Existing collection')
    requested = app.create_collection(workspace, 'Requested collection')
    source = tmp_path / ('source.' + request.param)
    if request.param == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(source))
        canvas.drawString(72, 700, 'Original railway history.')
        canvas.showPage()
        canvas.save()
    else:
        source.write_bytes('Original railway history.\r\nУкраїнський текст.'.encode('utf-8'))
    metadata = dict(title='Original title', authors=('Original author',),
                    language='en', edition='First printing',
                    work_id='stable-work', edition_id='stable-edition')
    initial = app._import_file(workspace, source, collections=(first,), **metadata)
    assert initial['state'] == 'complete'
    return SimpleNamespace(app=app, workspace=workspace, other=other, first=first,
        requested=requested, source=source, metadata=metadata, initial=initial,
        raw=source.read_bytes(), kind=request.param)


def source_state(item):
    """Collection changes must not republish sources or their metadata."""
    with item.app.read(item.workspace) as db:
        return {table: tuple(tuple(row) for row in db.execute('SELECT * FROM ' + table))
                for table in ('catalog_work', 'catalog_author', 'catalog_asset',
                              'catalog_edition', 'revision', 'active', 'page',
                              'source_metadata', 'source_page_review', 'indexed')}


def replay(item, **options):
    return item.app._import_file(item.workspace, item.source,
        collections=(item.first, item.requested),
        **dict(item.metadata, **options))


def test_registration_failure_rolls_back_then_replay_adds_only_membership(imported, monkeypatch):
    item = imported
    before = source_state(item)
    check = Storage._check
    observed = []

    def reject(storage):
        check(storage)
        query = ('SELECT count(*) FROM catalog_membership WHERE workspace=? '
                 'AND collection=? AND edition=?')
        args = (item.workspace, item.requested, item.metadata['edition_id'])
        if storage.db.execute(query, args).fetchone()[0]:
            with closing(sqlite3.connect(item.app.database)) as reader:
                observed.append(reader.execute(query, args).fetchone()[0])
            raise RuntimeError('replay consistency rejected')

    with monkeypatch.context() as patch:
        patch.setattr(Storage, '_check', reject)
        with pytest.raises(RuntimeError, match='replay consistency rejected'):
            replay(item)
    assert observed == [0]
    assert source_state(item) == before
    assert item.app.browse(item.workspace, collection=item.requested) == ()

    result = replay(item)
    assert result['state'] == 'complete' and result['id'] == item.initial['id']
    assert result['verified'] == (item.kind == 'txt')
    assert source_state(item) == before
    hit, = item.app.search(item.workspace, 'railway history', collection=item.requested)
    assert hit.revision == item.initial['id'] and hit.quote == 'railway history'
    assert item.app.search(item.other, 'railway history') == ()
    assert item.source.read_bytes() == item.raw
    assert item.app.assets.resolve(sha256(item.raw).hexdigest()).read_bytes() == item.raw


@pytest.mark.parametrize('changed', [dict(title='Conflicting title'),
    dict(authors=('Conflicting author',)), dict(edition='Another printing'),
    dict(work_id='another-work')])
def test_replay_conflicting_identity_cannot_leave_catalog_or_membership(imported, changed):
    before = source_state(imported)
    with pytest.raises(ValueError, match='identity conflict'):
        replay(imported, **changed)
    assert source_state(imported) == before
    assert imported.app.browse(imported.workspace, collection=imported.requested) == ()


@pytest.mark.parametrize('current_metadata', [False, True])
def test_replay_after_metadata_edit_preserves_revision_and_validates_current_metadata(imported, current_metadata):
    item = imported
    historical, = item.app.search(item.workspace, 'railway history')
    historical_preview = item.app.preview(item.workspace, historical.ref, historical.revision)
    edited = dict(title='Edited title', authors=('Edited author',), language='uk',
                  edition='Second printing', volume='II')
    current_revision = item.app.update_metadata(item.workspace,
        item.metadata['edition_id'], **edited)
    before = source_state(item)
    if current_metadata:
        result = replay(item, **edited)
        assert result['state'] == 'superseded' and result['id'] == item.initial['id']
        assert result['verified'] is False
        selected, = item.app.search(item.workspace, 'railway history', collection=item.requested)
        assert selected.revision == current_revision and selected.title == 'Edited title'
    else:
        with pytest.raises(ValueError, match='identity conflict'):
            replay(item)
        assert item.app.browse(item.workspace, collection=item.requested) == ()
    assert source_state(item) == before
    hit, = item.app.search(item.workspace, 'railway history')
    assert hit.revision == current_revision and hit.title == 'Edited title'
    assert item.app.preview(item.workspace, historical.ref, historical.revision) == historical_preview
    assert item.source.read_bytes() == item.raw


def test_replay_rejects_checkpoint_text_that_differs_from_sealed_source(imported):
    item = imported
    before = source_state(item)
    with closing(sqlite3.connect(item.app.root / 'imports.sqlite')) as db:
        payload, = db.execute('SELECT payload FROM checkpoint WHERE job=? AND ordinal=1',
                              (item.initial['id'],)).fetchone()
        record = json.loads(payload)
        record['raw_text'] = 'Replacement railway text'
        payload = canonical(record)
        db.execute('UPDATE checkpoint SET payload=?,digest=? WHERE job=? AND ordinal=1',
            (payload, sha256(payload.encode('utf-8')).hexdigest(), item.initial['id']))
        db.commit()
    with pytest.raises(IngestError, match='published page mismatch'):
        replay(item)
    assert source_state(item) == before
    assert item.app.browse(item.workspace, collection=item.requested) == ()


def test_replay_rechecks_active_revision_before_registering(imported, monkeypatch):
    item = imported
    run = Ingest.run
    replacement = uuid4().hex

    def run_then_replace(ingest, *args, **kwargs):
        result = run(ingest, *args, **kwargs)
        assert result['state'] == 'complete'
        # Simulate a separate SQLite writer immediately after the ingestion
        # snapshot has been checked. The exact active revision must be checked
        # again inside the membership transaction, even with unchanged bytes.
        with closing(Storage(item.app.database)) as storage:
            with storage.transaction():
                prior = storage.db.execute('SELECT asset,config FROM revision WHERE id=?',
                                           (item.initial['id'],)).fetchone()
                pages = tuple(Page(row['raw'], row['physical'], row['printed'], bool(row['verified']))
                    for row in storage.db.execute('SELECT * FROM page WHERE revision=? ORDER BY ordinal',
                                                  (item.initial['id'],)))
                storage.stage_revision(item.workspace, item.metadata['edition_id'],
                                       replacement, prior['asset'], prior['config'], pages)
                storage.db.execute('INSERT INTO source_metadata '
                    'SELECT workspace,edition,?,author,title,volume,edition_label '
                    'FROM source_metadata WHERE revision=?', (replacement, item.initial['id']))
                storage._check()
        return result

    monkeypatch.setattr(Ingest, 'run', run_then_replace)
    with pytest.raises(ValueError, match='active revision changed'):
        replay(item)
    assert item.app.browse(item.workspace, collection=item.requested) == ()
    hit, = item.app.search(item.workspace, 'railway history')
    assert hit.revision == replacement and hit.quote == 'railway history'


def test_cancelled_replay_does_not_add_membership(imported):
    before = source_state(imported)
    with pytest.raises(InterruptedError, match='cancelled'):
        replay(imported, cancelled=lambda: True)
    assert source_state(imported) == before
    assert imported.app.browse(imported.workspace, collection=imported.requested) == ()


@pytest.mark.parametrize('kind', ('txt', 'pdf'))
def test_fresh_post_commit_cancellation_returns_committed_source(tmp_path, monkeypatch, kind):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Post-commit cancellation')
    collection = app.create_collection(workspace, 'Committed collection')
    source = tmp_path / ('source.' + kind)
    if kind == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(source))
        canvas.drawString(72, 700, 'Original railway history.')
        canvas.save()
    else:
        source.write_bytes('Original railway history.\r\nУкраїнський текст.'.encode('utf-8'))
    raw = source.read_bytes()
    digest = sha256(raw).hexdigest()
    stopped = Event()
    observed = []
    run = Ingest.run

    def run_then_cancel(ingest, *args, **kwargs):
        result = run(ingest, *args, **kwargs)
        assert result['state'] == 'complete'
        # Observe the actual durable publication on an independent connection
        # before cancelling, rather than substituting a terminal ingest result.
        with closing(sqlite3.connect(app.database)) as reader:
            published = reader.execute(
                'SELECT r.workspace,r.edition,r.asset FROM revision r '
                'JOIN sealed s ON s.revision=r.id JOIN active a '
                'ON a.workspace=r.workspace AND a.edition=r.edition AND a.revision=r.id '
                'WHERE r.id=?', (result['id'],)).fetchone()
            assert published == (workspace, 'late-edition', digest)
            assert reader.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
            assert reader.execute(
                'SELECT count(*) FROM catalog_membership '
                'WHERE workspace=? AND collection=? AND edition=?',
                (workspace, collection, 'late-edition')).fetchone()[0] == 1
            assert 'railway history' in reader.execute(
                'SELECT raw FROM page WHERE revision=? ORDER BY ordinal',
                (result['id'],)).fetchone()[0]
        observed.append(result['id'])
        stopped.set()
        return result

    monkeypatch.setattr(Ingest, 'run', run_then_cancel)
    result = app._import_file(workspace, source, title='Original title',
        work_id='late-work', edition_id='late-edition', collections=(collection,),
        cancelled=stopped.is_set)
    assert stopped.is_set() and observed == [result['id']]
    assert result['state'] == 'complete' and result['verified'] is (kind == 'txt')
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM sealed').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM catalog_membership').fetchone()[0] == 1
    hit, = app.search(workspace, 'railway history', collection=collection)
    assert hit.revision == result['id'] and hit.verified is (kind == 'txt')
    assert source.read_bytes() == raw
    assert app.assets.resolve(digest).read_bytes() == raw

