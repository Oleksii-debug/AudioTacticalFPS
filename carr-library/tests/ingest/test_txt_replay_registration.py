"""Regression for completed TXT replay registration; must not bypass atomic gate."""
from contextlib import closing
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.storage.revisions import Storage


@pytest.fixture
def imported(tmp_path):
    app = LibraryApp(tmp_path / 'private')
    ws = app.create_workspace('Synthetic library')
    other = app.create_workspace('Independent library')
    first = app.create_collection(ws, 'First collection')
    requested = app.create_collection(ws, 'Requested collection')
    src = tmp_path / 'source.txt'
    raw = 'Міст railway history.\r\nSecond line.'.encode('utf-8')
    src.write_bytes(raw)
    meta = dict(title='Synthetic source', authors=('Author A',),
                language='uk', work_id='w', edition_id='e')
    initial = app.import_file(ws, src, collections=(first,), **meta)
    assert initial['state'] == 'complete'
    return app, ws, other, first, requested, src, raw, meta, initial


def test_txt_replay_registration_is_atomic(imported, tmp_path, monkeypatch):
    app, ws, other, first, requested, src, raw, meta, initial = imported
    hit, = app.search(ws, 'railway history', collection=first)
    assert hit.verified
    old_check = Storage._check
    observed = []

    def reject(storage):
        old_check(storage)
        sql = ('SELECT count(*) FROM catalog_membership '
               'WHERE workspace=? AND collection=? AND edition=?')
        args = (ws, requested, meta['edition_id'])
        if storage.db.execute(sql, args).fetchone()[0]:
            with closing(sqlite3.connect(app.database)) as reader:
                observed.append(reader.execute(sql, args).fetchone()[0])
            raise RuntimeError('injected TXT replay gate rejection')

    with monkeypatch.context() as m:
        m.setattr(Storage, '_check', reject)
        with pytest.raises(RuntimeError, match='injected TXT replay gate rejection'):
            app.import_file(ws, src, collections=(first, requested), **meta)
    assert observed == [0]
    assert app.browse(ws, collection=requested) == ()
    assert app.search(other, 'railway history') == ()

    replay = app.import_file(ws, src, collections=(first, requested), **meta)
    assert replay['state'] == 'complete' and replay['id'] == initial['id']
    recovered, = app.search(ws, 'railway history', collection=requested)
    assert (recovered.quote, recovered.citation, recovered.revision) == (
        hit.quote, hit.citation, hit.revision)
    assert src.read_bytes() == raw
    with closing(sqlite3.connect(app.database)) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 1

    backup = tmp_path / 'backup'
    token = app.backup(backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', token))
    saved, = restored.search(ws, 'railway history', collection=requested)
    assert (saved.quote, saved.citation, saved.revision) == (
        hit.quote, hit.citation, hit.revision)
    assert restored.search(other, 'railway history') == ()
    assert restored.assets.resolve(replay['asset']).read_bytes() == raw


def test_txt_replay_rejects_conflicting_identity(imported):
    app, ws, other, first, requested, src, raw, meta, initial = imported
    with pytest.raises(ValueError, match='work identity conflict'):
        app.import_file(ws, src, collections=(requested,),
                        **dict(meta, title='Conflicting title'))
    assert app.browse(ws, collection=requested) == ()
    hit, = app.search(ws, 'railway history', collection=first)
    assert hit.title == meta['title'] and hit.revision == initial['id']
    assert app.search(other, 'railway history') == ()
    assert src.read_bytes() == raw
