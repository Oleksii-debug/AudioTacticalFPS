"""Late cancellation cannot turn an already committed import into a failure."""
from contextlib import closing
import sqlite3
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest.pipeline import Ingest


@pytest.mark.parametrize('format', ['txt', 'pdf'])
def test_cancel_after_committed_publication_returns_complete_and_keeps_replay_guard(
        tmp_path, monkeypatch, format):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Publication boundary')
    first = app.create_collection(workspace, 'Published collection')
    requested = app.create_collection(workspace, 'Later replay collection')
    source = tmp_path / ('source.' + format)
    if format == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(source))
        canvas.drawString(72, 700, 'Original railway history')
        canvas.save()
    else:
        source.write_bytes('Оригінал railway history.\r\n'.encode('utf-8'))
    original_bytes = source.read_bytes()
    metadata = dict(title='Original source', authors=('Synthetic Author',),
                    work_id='publication-work', edition_id='publication-edition')
    stopped, observed = Event(), []
    original_run = Ingest.run

    def stop_after_publication(ingest, *args, **options):
        result = original_run(ingest, *args, **options)
        assert result['state'] == 'complete'
        # A separate connection must already see the complete publication before
        # this cancellation is injected. No publisher/source method is stubbed.
        with closing(sqlite3.connect(app.database)) as reader:
            observed.append((
                reader.execute('SELECT count(*) FROM catalog_membership '
                               'WHERE workspace=? AND collection=?',
                               (workspace, first)).fetchone()[0],
                reader.execute('SELECT count(*) FROM revision r JOIN sealed s '
                               'ON s.revision=r.id').fetchone()[0],
                reader.execute('SELECT count(*) FROM catalog_asset').fetchone()[0]))
        assert observed == [(1, 1, 1)]
        stopped.set()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Ingest, 'run', stop_after_publication)
        result = app._import_file(workspace, source, collections=(first,),
                                  cancelled=stopped.is_set, **metadata)
    assert result['state'] == 'complete' and stopped.is_set()
    assert source.read_bytes() == original_bytes
    reopened = LibraryApp(app.root)
    hit, = reopened.search(workspace, 'railway history', collection=first)
    assert hit.revision == result['id']
    if format == 'txt':
        assert reopened.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway history'
    else:
        assert not hit.verified
        with pytest.raises(ValueError):
            reopened.copy(workspace, hit.ref, hit.revision, 'quote')

    # A fresh attempt could reuse the completed revision, but cancellation before
    # source admission must still prevent any new membership from being committed.
    cancelled = reopened._import_file(workspace, source, collections=(requested,),
                                      cancelled=stopped.is_set, **metadata)
    assert cancelled['state'] == 'cancelled' and 'id' not in cancelled
    assert reopened.browse(workspace, collection=requested) == ()
    stopped.clear()
    replay = reopened._import_file(workspace, source, collections=(requested,),
                                   cancelled=stopped.is_set, **metadata)
    assert replay['id'] == result['id'] and replay['state'] == 'complete'
    assert reopened.browse(workspace, collection=first) == reopened.browse(
        workspace, collection=requested)
    with reopened.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 1
    assert source.read_bytes() == original_bytes
