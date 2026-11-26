"""Port PR31's pre-commit consistency regression to the assembled V3 app."""
from contextlib import closing
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import restore_backup
from carr_library.storage.revisions import Storage


def test_gate_failure_is_invisible_and_retry_restore_preserves_quote(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'private')
    ws = app.create_workspace('Synthetic A')
    other = app.create_workspace('Synthetic B')
    src = tmp_path / 'source.txt'
    raw = 'Міст railway history.\r\nSecond line.'
    src.write_bytes(raw.encode('utf-8'))
    meta = dict(title='Synthetic source', authors=('Author A',), language='uk',
                work_id='stable-work', edition_id='stable-edition')
    original_check = Storage._check
    observed = []

    def reject_after_real_check(storage):
        original_check(storage)
        if storage.db.execute('SELECT 1 FROM active WHERE workspace=? AND edition=?',
                              (ws, meta['edition_id'])).fetchone():
            # A separate connection must never observe the candidate while
            # the consistency gate is still capable of rejecting it.
            with closing(sqlite3.connect(app.database)) as reader:
                observed.append(tuple(reader.execute(
                    'SELECT count(*) FROM ' + table).fetchone()[0]
                    for table in ('catalog_edition', 'revision', 'active_page',
                                  'source_metadata', 'indexed')))
            raise RuntimeError('injected pre-publication gate rejection')

    with monkeypatch.context() as m:
        m.setattr(Storage, '_check', reject_after_real_check)
        with pytest.raises(RuntimeError, match='injected pre-publication gate rejection'):
            app.import_file(ws, src, **meta)

    assert observed == [(0, 0, 0, 0, 0)]
    assert app.browse(ws) == ()
    assert app.search(ws, 'railway') == ()
    with closing(sqlite3.connect(app.root / 'imports.sqlite')) as journal:
        job, state = journal.execute('SELECT id,state FROM job WHERE workspace=?',
                                     (ws,)).fetchone()
        assert state == 'failed'
        assert journal.execute('SELECT count(*) FROM checkpoint WHERE job=?',
                               (job,)).fetchone()[0] == 1

    result = app._import_file(ws, src, _resume_job=job, **meta)
    assert result['id'] == job and result['state'] == 'complete'
    hit, = app.search(ws, 'railway history')
    assert hit.quote == 'railway history' and hit.verified
    assert app.copy(ws, hit.ref, hit.revision, 'quote') == hit.quote
    assert app.search(other, 'railway history') == ()
    assert src.read_bytes() == raw.encode('utf-8')

    backup = tmp_path / 'backup'
    token = app.backup(backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', token))
    recovered, = restored.search(ws, 'railway history')
    assert recovered.quote == hit.quote and recovered.citation == hit.citation
    assert restored.search(other, 'railway history') == ()
    assert restored.assets.resolve(result['asset']).read_bytes() == src.read_bytes()

