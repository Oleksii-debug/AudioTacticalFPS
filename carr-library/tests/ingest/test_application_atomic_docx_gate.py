"""A rejected DOCX replay must not publish new collection memberships."""
from contextlib import closing
from hashlib import sha256
import sqlite3

from docx import Document
import pytest

from carr_library.application import LibraryApp
from carr_library.storage.revisions import Storage


def test_docx_replay_gate_preserves_revision_and_rolls_back_new_membership(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'private')
    workspace = app.create_workspace('Synthetic DOCX library')
    existing = app.create_collection(workspace, 'Existing collection')
    requested = app.create_collection(workspace, 'Requested collection')
    source = tmp_path / 'source.docx'
    document = Document()
    document.add_paragraph('Original railway history. Український текст.')
    document.save(source)
    original_bytes = source.read_bytes()
    metadata = dict(title='Synthetic DOCX source', authors=('Synthetic author',),
                    work_id='stable-work', edition_id='stable-edition')
    # Exercise the real native-worker implementation directly so the injected
    # gate runs in this process; the input is an actual generated DOCX archive.
    first = app._import_file(workspace, source, collections=(existing,), **metadata)
    assert first['state'] == 'complete' and not first['verified']

    def snapshot():
        with closing(sqlite3.connect(app.database)) as db:
            return {table: tuple(db.execute('SELECT * FROM ' + table))
                    for table in ('catalog_edition', 'revision', 'active', 'page',
                                  'source_metadata', 'indexed')}

    before = snapshot()
    original_check = Storage._check
    observed = []

    def reject_membership(storage):
        original_check(storage)
        if storage.db.execute('SELECT 1 FROM catalog_membership WHERE workspace=? '
                'AND collection=? AND edition=?', (workspace, requested, metadata['edition_id'])).fetchone():
            with closing(sqlite3.connect(app.database)) as reader:
                observed.append(reader.execute('SELECT count(*) FROM catalog_membership '
                    'WHERE workspace=? AND collection=? AND edition=?',
                    (workspace, requested, metadata['edition_id'])).fetchone()[0])
            raise RuntimeError('injected DOCX replay consistency rejection')

    with monkeypatch.context() as patch:
        patch.setattr(Storage, '_check', reject_membership)
        with pytest.raises(RuntimeError, match='injected DOCX replay consistency rejection'):
            app._import_file(workspace, source, collections=(existing, requested), **metadata)

    assert observed == [0]
    assert snapshot() == before
    assert len(app.browse(workspace, collection=existing)) == 1
    assert app.browse(workspace, collection=requested) == ()
    assert source.read_bytes() == original_bytes
    assert app.assets.resolve(sha256(original_bytes).hexdigest()).read_bytes() == original_bytes

    retried = app._import_file(workspace, source, collections=(existing, requested), **metadata)
    assert retried['state'] == 'complete' and retried['id'] == first['id']
    assert snapshot() == before
    assert len(app.browse(workspace, collection=existing)) == 1
    assert len(app.browse(workspace, collection=requested)) == 1
    hit, = app.search(workspace, 'railway history', collection=requested)
    assert hit.quote == 'railway history' and hit.revision == first['id'] and not hit.verified
