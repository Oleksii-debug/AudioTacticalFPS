"""Real guarded ingestion -> native FTS -> revision bundle, no mocked services."""
from contextlib import closing
from hashlib import sha256

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from bootstrap.vendor.cairn_extractors.hasher import hash_file
from carr_library.ingest.bundle import create_revision_bundle, restore_revision_bundle
from carr_library.ingest.pipeline import IngestError
from carr_library.ingest.writer_guard import exclusive_writer, open_ingest_session
from carr_library.storage.revisions import Storage


def search(storage, workspace, query):
    return [tuple(r) for r in storage.db.execute(
        'SELECT p.revision,p.ordinal,p.raw FROM page_fts JOIN active_page p ON p.id=page_fts.rowid '
        'WHERE page_fts MATCH ? AND p.workspace=? ORDER BY p.revision,p.ordinal', (query, workspace))]


def test_real_txt_import_resume_backup_and_historical_text(tmp_path):
    root = tmp_path / 'Дослідження'
    root.mkdir()
    sources = tmp_path / 'Sources'
    sources.mkdir()
    path = sources / 'book.txt'
    original = 'Перший railway\r\n\fДругий passage'.encode()
    path.write_bytes(original)
    db, journal = root / 'library.sqlite', root / 'jobs.sqlite'
    with open_ingest_session(root, db, journal) as j:
        a = j.start('A', 'same-edition', sources, path.name)
        assert j.run('A', a, sources, path.name, max_steps=1)['completed'] == 1
        assert not search(j.storage, 'A', 'railway')
    with open_ingest_session(root, db, journal) as j:
        assert j.run('A', a, sources, path.name, cancelled=lambda: True)['state'] == 'cancelled'
        j.retry('A', a)
        assert j.run('A', a, sources, path.name)['state'] == 'complete'
        assert j.start('A', 'same-edition', sources, path.name) == a
        assert j.run('A', a, sources, path.name)['state'] == 'complete'
        assert path.read_bytes() == original
        b = j.start('B', 'same-edition', sources, path.name)
        assert b != a
        assert j.run('B', b, sources, path.name)['state'] == 'complete'
        assert search(j.storage, 'A', 'railway') == [(a, 1, 'Перший railway\r\n')]
        assert search(j.storage, 'B', 'railway') == [(b, 1, 'Перший railway\r\n')]
        path.write_bytes(b'New source content')
        newer = j.start('A', 'same-edition', sources, path.name)
        assert j.run('A', newer, sources, path.name)['state'] == 'complete'
        assert not search(j.storage, 'A', 'railway')
        assert path.read_bytes() == b'New source content'
        manifest = create_revision_bundle(j.storage, root / 'backup', include_all_workspaces=True)
    with exclusive_writer(root):
        restored = restore_revision_bundle(root / 'backup', root / 'copy.sqlite',
                                           expected_manifest_sha256=manifest)
        with closing(Storage(restored)) as copy:
            copy.check()
            assert search(copy, 'A', 'content') == [(newer, 1, 'New source content')]
            assert search(copy, 'B', 'railway') == [(b, 1, 'Перший railway\r\n')]
            old = copy.get_page('A', a, 1)
            assert (old['raw'], old['physical'], old['printed'], old['verified'], old['digest']) == (
                'Перший railway\r\n', None, None, 0, sha256('Перший railway\r\n'.encode()).hexdigest())
            assert copy.get_page('B', a, 1) is None
            assert copy.db.execute('SELECT asset FROM revision WHERE id=?', (a,)).fetchone()[0] == sha256(original).hexdigest()


def test_real_pdf_import_roundtrip(tmp_path):
    root = tmp_path / 'Library'
    root.mkdir()
    pdf = tmp_path / 'book.pdf'
    writer = canvas.Canvas(str(pdf))
    writer.drawString(80, 700, 'Synthetic railway evidence')
    writer.showPage()
    writer.drawString(80, 700, 'Another synthetic chapter')
    writer.showPage()
    writer.save()
    source_hash = hash_file(pdf)
    expected = [p.extract_text() for p in PdfReader(pdf).pages]
    db, journal = root / 'library.sqlite', root / 'jobs.sqlite'
    with open_ingest_session(root, db, journal) as j:
        job = j.start('A', 'pdf-edition', tmp_path, pdf.name)
        assert j.run('A', job, tmp_path, pdf.name, max_steps=1)['completed'] == 1
        assert not search(j.storage, 'A', 'railway')
    with open_ingest_session(root, db, journal) as j:
        result = j.run('A', job, tmp_path, pdf.name)
        assert result['state'] == 'complete', result
        assert hash_file(pdf) == source_hash == result['asset']
        assert search(j.storage, 'A', 'railway') == [(job, 1, expected[0])]
        assert not search(j.storage, 'B', 'railway')
        manifest = create_revision_bundle(j.storage, root / 'backup', include_all_workspaces=True)
    with exclusive_writer(root):
        restored = restore_revision_bundle(root / 'backup', root / 'copy.sqlite',
                                           expected_manifest_sha256=manifest)
        with closing(Storage(restored)) as copy:
            copy.check()
            for i, raw in enumerate(expected, 1):
                page = copy.get_page('A', job, i)
                assert (page['raw'], page['physical'], page['printed'], page['verified'], page['digest']) == (
                    raw, i, None, 0, sha256(raw.encode()).hexdigest())
            assert copy.db.execute('SELECT asset FROM revision WHERE id=?', (job,)).fetchone()[0] == source_hash


def test_changed_source_cannot_enter_backup(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    path = tmp_path / 'book.txt'
    path.write_bytes(b'Original railway\fSecond page')
    db, journal = root / 'library.sqlite', root / 'jobs.sqlite'
    with open_ingest_session(root, db, journal) as j:
        job = j.start('A', 'edition', tmp_path, path.name)
        assert j.run('A', job, tmp_path, path.name, max_steps=1)['completed'] == 1
        path.write_bytes(b'Changed content')
        with pytest.raises(IngestError, match='changed'):
            j.run('A', job, tmp_path, path.name)
        assert not search(j.storage, 'A', 'railway')
        assert j.inspect('A', job)['completed'] == 1
        manifest = create_revision_bundle(j.storage, root / 'backup', include_all_workspaces=True)
    with exclusive_writer(root):
        restored = restore_revision_bundle(root / 'backup', root / 'copy.sqlite',
                                           expected_manifest_sha256=manifest)
        with closing(Storage(restored)) as copy:
            copy.check()
            assert copy.get_page('A', job, 1) is None
            assert not search(copy, 'A', 'railway')
            assert not search(copy, 'A', 'Changed')
