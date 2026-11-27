"""A committed first import stays complete through its final application handoff."""
from contextlib import closing
import sqlite3

import pytest

import carr_library.application as application
from carr_library.application import LibraryApp
from carr_library.ingest.pipeline import Ingest


@pytest.fixture(params=['txt', 'pdf'])
def first_import(tmp_path, request):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('First publication')
    collection = app.create_collection(workspace, 'Selected collection')
    source = tmp_path / ('source.' + request.param)
    if request.param == 'pdf':
        from reportlab.pdfgen.canvas import Canvas
        canvas = Canvas(str(source))
        canvas.drawString(72, 700, 'Synthetic railway publication')
        canvas.save()
    else:
        source.write_bytes('Synthetic railway publication\r\nМіст.'.encode('utf-8'))
    metadata = dict(title='First publication', authors=('Synthetic author',),
                    work_id='work', edition_id='edition', collections=(collection,))
    return app, workspace, collection, source, metadata


def _assert_durable(first_import, result, raw):
    app, workspace, collection, source, metadata = first_import
    assert result['state'] == 'complete'
    reopened = LibraryApp(app.root)
    hit, = reopened.search(workspace, 'railway', collection=collection)
    assert hit.revision == result['id'] and hit.title == metadata['title']
    assert source.read_bytes() == raw
    with closing(sqlite3.connect(app.database)) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM sealed WHERE revision=?',
                          (result['id'],)).fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM catalog_membership WHERE workspace=? '
                          'AND collection=? AND edition=?',
                          (workspace, collection, metadata['edition_id'])).fetchone()[0] == 1


def test_cancel_after_first_durable_publication_does_not_report_failure(first_import, monkeypatch):
    app, workspace, _, source, metadata = first_import
    raw = source.read_bytes()
    stopped = False
    original_run = Ingest.run

    def completed_then_cancelled(*args, **kwargs):
        nonlocal stopped
        result = original_run(*args, **kwargs)
        assert result['state'] == 'complete'
        # A separate connection observes a real commit before the late request.
        with closing(sqlite3.connect(app.database)) as observer:
            assert observer.execute('SELECT 1 FROM sealed WHERE revision=?',
                                    (result['id'],)).fetchone() == (1,)
        stopped = True
        return result

    monkeypatch.setattr(Ingest, 'run', completed_then_cancelled)
    result = app._import_file(workspace, source, cancelled=lambda: stopped, **metadata)
    _assert_durable(first_import, result, raw)


def test_first_publication_does_not_repeat_committed_catalog_registration(first_import, monkeypatch):
    app, workspace, _, source, metadata = first_import
    raw = source.read_bytes()
    original_register = application.register_batch
    registrations = []

    def register_once(*args, **kwargs):
        if registrations:
            raise RuntimeError('A committed first import was registered a second time.')
        result = original_register(*args, **kwargs)
        registrations.append(True)
        return result

    monkeypatch.setattr(application, 'register_batch', register_once)
    result = app._import_file(workspace, source, **metadata)
    assert registrations == [True]
    _assert_durable(first_import, result, raw)
