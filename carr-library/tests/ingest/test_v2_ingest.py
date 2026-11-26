"""Actual V2 TXT/PDF ingestion with one atomic publication transaction."""
from contextlib import closing
import sqlite3

import pytest

from carr_library.catalog.intake import snapshot
from carr_library.catalog.membership import Catalog
from carr_library.catalog.registration import Registration, register_batch
from carr_library.ingest.bundle import create_revision_bundle, restore_revision_bundle
from carr_library.ingest.pipeline import Ingest, IngestError
from carr_library.storage.revisions import Page, Storage


@pytest.fixture
def library(tmp_path):
    with closing(Storage(tmp_path / 'library.sqlite', schema_version=2)) as s:
        s.db.execute('PRAGMA journal_mode=WAL')
        with s.transaction():
            for ws in ('A', 'B'):
                s.db.execute('INSERT INTO workspace VALUES(?)', (ws,))
                Catalog(s.db, ws).create_collection('research', 'Research')
        yield s


def registration(root, filename, author='Author A'):
    return Registration('work', 'edition', snapshot(root, filename).manifest(),
                        title='Same title', authors=(author,), language='uk', collections=('research',))


def test_resume_dedup_and_restore(library, tmp_path):
    source = tmp_path / 'source.txt'
    raw = 'Міст\r\noriginal\fSecond passage'.encode('utf-8')
    source.write_bytes(raw)
    journal = tmp_path / 'journal.sqlite'
    reg = registration(tmp_path, source.name)
    with closing(Ingest(journal, library)) as i:
        job = i.start('A', 'edition', tmp_path, source.name, registration=reg)
        assert i.run('A', job, tmp_path, source.name, max_steps=1)['completed'] == 1
        assert Catalog(library.db, 'A').editions() == ()
    with closing(Ingest(journal, library)) as i:
        assert i.run('A', job, tmp_path, source.name)['state'] == 'complete'
        assert i.start('A', 'edition', tmp_path, source.name, registration=reg) == job
        assert i.run('A', job, tmp_path, source.name)['state'] == 'complete'
        with pytest.raises(IngestError, match='unknown job'):
            i.inspect('B', job)
        other = i.start('B', 'edition', tmp_path, source.name,
                        registration=registration(tmp_path, source.name, 'Author B'))
        assert i.run('B', other, tmp_path, source.name)['state'] == 'complete'
    assert source.read_bytes() == raw
    assert library.get_page('A', job, 1)['raw'] == 'Міст\r\noriginal'
    assert library.get_page('A', job, 2)['physical'] is None
    assert library.get_page('B', job, 1) is None
    assert len(Catalog(library.db, 'A').editions('research')) == 1
    assert library.db.execute('SELECT count(*) FROM revision').fetchone()[0] == 2
    assert library.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 1
    root, target = tmp_path / 'backup', tmp_path / 'restored.sqlite'
    token = create_revision_bundle(library, root, include_all_workspaces=True)
    restore_revision_bundle(root, target, expected_manifest_sha256=token)
    with closing(Storage(target, schema_version=2)) as s:
        assert s.get_page('A', job, 1)['raw'] == 'Міст\r\noriginal'
        assert s.get_page('B', job, 1) is None
        assert s.db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'Міст'").fetchone()[0] == 2
        s.check()


def test_failed_consistency_gate_rolls_back_and_retries(library, tmp_path, monkeypatch):
    source = tmp_path / 'source.txt'
    source.write_text('valid passage', encoding='utf-8')
    with closing(Ingest(tmp_path / 'journal.sqlite', library)) as i:
        job = i.start('A', 'edition', tmp_path, source.name, registration=registration(tmp_path, source.name))
        assert i.run('A', job, tmp_path, source.name, max_steps=1)['completed'] == 1
        actual_check = library._check
        def fail_after_real_check():
            actual_check()
            with closing(sqlite3.connect(tmp_path / 'library.sqlite')) as reader:
                assert reader.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 0
                assert reader.execute('SELECT count(*) FROM active_page').fetchone()[0] == 0
            raise RuntimeError('injected gate failure')
        with monkeypatch.context() as m:
            m.setattr(library, '_check', fail_after_real_check)
            with pytest.raises(RuntimeError, match='injected gate failure'):
                i.run('A', job, tmp_path, source.name)
        assert i.inspect('A', job)['state'] == 'failed'
        assert Catalog(library.db, 'A').editions() == ()
        assert library.get_page('A', job, 1) is None
        assert library.db.execute('SELECT count(*) FROM catalog_asset').fetchone()[0] == 0
        assert library.db.execute("SELECT count(*) FROM page_fts WHERE page_fts MATCH 'valid'").fetchone()[0] == 0
        i.retry('A', job)
        assert i.run('A', job, tmp_path, source.name)['state'] == 'complete'
        assert library.get_page('A', job, 1)['raw'] == 'valid passage'


def test_stale_active_rejected(library, tmp_path):
    source = tmp_path / 'source.txt'
    source.write_text('valid passage', encoding='utf-8')
    reg = registration(tmp_path, source.name)
    with closing(Ingest(tmp_path / 'journal.sqlite', library)) as i:
        job = i.start('A', 'edition', tmp_path, source.name, registration=reg)
        assert i.run('A', job, tmp_path, source.name, max_steps=1)['completed'] == 1
        with library.transaction():
            register_batch(Catalog(library.db, 'A'), (reg,))
            library.stage_revision('A', 'edition', 'winner', reg.manifest['sha256'], 'other', [Page('winner')])
        assert i.run('A', job, tmp_path, source.name)['state'] == 'conflict'
        assert library.get_page('A', job, 1) is None
        assert library.db.execute('SELECT revision FROM active').fetchone()[0] == 'winner'


def test_registration_required_and_bound(library, tmp_path):
    source = tmp_path / 'source.txt'
    source.write_text('original', encoding='utf-8')
    with closing(Ingest(tmp_path / 'journal.sqlite', library)) as i:
        with pytest.raises(IngestError, match='registration'):
            i.start('A', 'edition', tmp_path, source.name)
        reg = registration(tmp_path, source.name)
        with pytest.raises(IngestError, match='registration'):
            i.start('A', 'wrong-edition', tmp_path, source.name, registration=reg)
        job = i.start('A', 'edition', tmp_path, source.name, registration=reg)
        source.write_text('changed', encoding='utf-8')
        with pytest.raises(IngestError, match='registration'):
            i.run('A', job, tmp_path, source.name)
        assert Catalog(library.db, 'A').editions() == ()


def test_native_pdf_publication(library, tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    source = tmp_path / 'source.pdf'
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=200)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 30 100 Td (Verified source) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    writer.write(source)
    with closing(Ingest(tmp_path / 'journal.sqlite', library)) as i:
        job = i.start('A', 'edition', tmp_path, source.name, registration=registration(tmp_path, source.name))
        assert i.run('A', job, tmp_path, source.name)['state'] == 'complete'
        row = library.get_page('A', job, 1)
        assert (row['raw'], row['physical'], row['printed'], row['verified']) == ('Verified source', 1, None, 0)
