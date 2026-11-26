"""Real EPUB -> isolated import -> reader/search/backup with original bytes."""
from contextlib import closing
from hashlib import sha256
from io import BytesIO
import json
import sqlite3
from zipfile import ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.ebook import open_ebook, read_chapter
from carr_library.reader.source_file import prepare_source_file
from carr_library.storage.revisions import Storage


def synthetic_epub():
    """Intentionally distinct ZIP, manifest and spine ordering; no private book."""
    stream = BytesIO()
    files = {
        'META-INF/container.xml': '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="EPUB/book.opf" media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>',
        'EPUB/book.opf': '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><metadata><dc:title>Embedded title hint</dc:title>'
            '<dc:creator>Unconfirmed embedded author</dc:creator><dc:language>uk</dc:language></metadata>'
            '<manifest><item id="second" href="second.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="first" href="first.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="nav" href="nav/contents.xhtml" properties="nav" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="cover"/><itemref idref="first"/>'
            '<itemref idref="second"/><itemref idref="notes" linear="no"/></spine></package>',
        'EPUB/second.xhtml': '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Second</title></head>'
            '<body><h1 id="two">Другий розділ</h1><p>Unicode 🦉 and a supplementary link.</p></body></html>',
        'EPUB/notes.xhtml': '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<h2 id="note">Примітки</h2><p>Supplementary evidence from a synthetic source.</p></body></html>',
        'EPUB/first.xhtml': '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<h1 id="one">Перший розділ</h1><p id="p7">Залізнична історія — Straße. '
            'Nested <em>original</em> text.</p><pre>A  B\nC</pre></body></html>',
        'EPUB/cover.xhtml': '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>Cover</title></head>'
            '<body><img src="cover.png" alt="Synthetic cover description"/></body></html>',
        'EPUB/nav/contents.xhtml': '<html xmlns="http://www.w3.org/1999/xhtml" '
            'xmlns:epub="http://www.idpf.org/2007/ops"><body><nav epub:type="toc"><ol>'
            '<li><span>Основний текст</span><ol><li><a href="../first.xhtml#one">Перший розділ</a></li>'
            '<li><a href="../second.xhtml#two">Другий розділ</a></li></ol></li>'
            '<li><a href="../notes.xhtml#note">Примітки</a></li></ol></nav>'
            '<nav epub:type="page-list"><ol><li><a href="../first.xhtml#p7">7</a></li></ol></nav>'
            '</body></html>',
    }
    with ZipFile(stream, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        for name, text in files.items():
            archive.writestr(name, text.encode('utf-8'))
    return stream.getvalue()


@pytest.fixture
def imported(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Academic EPUB sources')
    collection = app.create_collection(workspace, 'Books')
    source = tmp_path / 'synthetic.epub'
    source.write_bytes(synthetic_epub())
    result = app._import_file(workspace, source, title='Confirmed catalog title',
        authors=('Confirmed catalog author',), collections=(collection,),
        work_id='work', edition_id='edition')
    return app, workspace, collection, source, result


def test_isolated_import_spine_contents_search_and_quotation_boundary(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Worker import')
    source = tmp_path / 'synthetic.epub'
    original = synthetic_epub()
    source.write_bytes(original)
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete' and result['verified'] is False
    assert result['completed'] == 4
    contents = open_ebook(app, workspace, result['edition'])
    assert contents['title'] == 'synthetic' and contents['authors'] == []
    assert contents['metadata_hints']['creators'] == ['Unconfirmed embedded author']
    assert [r['member'] for r in contents['chapters']] == [
        'EPUB/cover.xhtml', 'EPUB/first.xhtml', 'EPUB/second.xhtml', 'EPUB/notes.xhtml']
    assert [r['linear'] for r in contents['chapters']] == [True, True, True, False]
    assert contents['chapters'][0]['characters'] == 0
    assert 'images_not_transcribed' in contents['chapters'][0]['warnings']
    assert contents['chapters'][0]['image_descriptions'] == ['Synthetic cover description']
    assert contents['toc'][0]['heading_only'] and contents['toc'][0]['ordinal'] is None
    assert [r['ordinal'] for r in contents['toc'][1:]] == [2, 3, 4]
    assert contents['page_hints'][0]['title'] == '7'
    chapter = read_chapter(app, workspace, result['edition'], result['id'], 2)
    assert chapter['raw_text'] == ('Перший розділ\nЗалізнична історія — Straße. '
                                   'Nested original text.\nA  B\nC\n')
    location = contents['page_hints'][0]['offset']
    assert chapter['raw_text'][location:].startswith('Залізнична')
    hit, = app.search(workspace, 'Залізнична історія')
    assert not hit.verified and 'Segment 2' in hit.page
    assert 'Залізнична історія' in app.preview(workspace, hit.ref, hit.revision)
    topic = app.create_topic(workspace, 'Evidence')
    with pytest.raises(ValueError, match='unverified extraction'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    with pytest.raises(ValueError, match='unverified extraction'):
        app.save_hit(workspace, topic, hit.ref, hit.revision)
    assert app.evidence(workspace, topic) == ()
    assert source.read_bytes() == original
    assert app.assets.resolve(sha256(original).hexdigest()).read_bytes() == original
    assert len(app.import_requests(workspace)) == 1


def test_original_copy_complete_backup_and_reopen_keep_navigation(imported, tmp_path):
    app, workspace, collection, source, result = imported
    before = open_ebook(app, workspace, 'edition')
    assert before['title'] == 'Confirmed catalog title'
    assert before['authors'] == ['Confirmed catalog author']
    hit, = app.search(workspace, 'Залізнична', collection=collection)
    view = prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert view.path.suffix == '.epub' and view.path.read_bytes() == source.read_bytes()
    view.path.write_bytes(b'User edited only the disposable viewing copy.')
    assert app.assets.resolve(before['asset_sha256']).read_bytes() == source.read_bytes()
    backup = tmp_path / 'backup'
    manifest = create_backup(app, backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', manifest))
    assert open_ebook(restored, workspace, 'edition') == before
    assert read_chapter(restored, workspace, 'edition', result['id'], 4)['raw_text'].startswith('Примітки')
    assert len(restored.browse(workspace, collection=collection)) == 1
    assert restored.assets.resolve(before['asset_sha256']).read_bytes() == source.read_bytes()
    assert open_ebook(LibraryApp(app.root), workspace, 'edition') == before


def test_scope_topic_archive_revision_and_cancellation_rechecked(imported):
    app, workspace, _, source, result = imported
    topic = app.create_topic(workspace, 'Bound topic')
    other = app.create_workspace('Other library')
    with pytest.raises(ValueError):
        open_ebook(app, other, 'edition')
    second = app._import_file(other, source, work_id='work', edition_id='edition')
    assert second['id'] != result['id']
    with pytest.raises(ValueError, match='revision changed'):
        read_chapter(app, other, 'edition', result['id'], 1)
    with app.topic_transaction(workspace) as tree:
        tree.archive(topic)
    for operation in (lambda: open_ebook(app, workspace, 'edition', authorized_topic=topic),
                      lambda: read_chapter(app, workspace, 'edition', result['id'], 2,
                                           authorized_topic=topic)):
        with pytest.raises(ValueError, match='archived'):
            operation()
    for bad in (0, -1, 5, True, 1.0, '1'):
        with pytest.raises(ValueError):
            read_chapter(app, workspace, 'edition', result['id'], bad)
    with pytest.raises(InterruptedError):
        open_ebook(app, workspace, 'edition', cancelled=lambda: True)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        read_chapter(app, workspace, 'edition', result['id'], 2)


def test_source_drift_blocks_cached_chapter_and_fresh_search(imported):
    app, workspace, _, _, result = imported
    contents = open_ebook(app, workspace, 'edition')
    retained = app.assets.resolve(contents['asset_sha256'])
    retained.write_bytes(b'Changed retained original.')
    for operation in (lambda: open_ebook(app, workspace, 'edition'),
                      lambda: read_chapter(app, workspace, 'edition', result['id'], 2),
                      lambda: app.search(workspace, 'Залізнична')):
        with pytest.raises(ValueError, match='digest mismatch'):
            operation()


def test_metadata_revision_reopens_same_extraction_and_invalidates_old_reader(imported):
    app, workspace, _, _, result = imported
    before = read_chapter(app, workspace, 'edition', result['id'], 2)['raw_text']
    for title in ('Corrected academic title', 'Second catalog correction'):
        revision = app.update_metadata(workspace, 'edition', title=title,
            authors=('Corrected author',), language='uk', edition='Revised', volume=None)
        assert revision != result['id']
        with pytest.raises(ValueError, match='revision changed'):
            read_chapter(app, workspace, 'edition', result['id'], 2)
        reopened = open_ebook(app, workspace, 'edition')
        assert reopened['title'] == title and reopened['authors'] == ['Corrected author']
        assert reopened['revision'] == revision
        assert read_chapter(app, workspace, 'edition', revision, 2)['raw_text'] == before
        hit, = app.search(workspace, 'Залізнична')
        assert not hit.verified and hit.revision == revision


def test_replay_registration_rejection_is_atomic(imported, monkeypatch):
    app, workspace, existing, source, first = imported
    requested = app.create_collection(workspace, 'New collection')
    before = open_ebook(app, workspace, 'edition')
    original_check = Storage._check
    observed = []

    def reject_membership(storage):
        original_check(storage)
        if storage.db.execute('SELECT 1 FROM catalog_membership WHERE workspace=? AND collection=?',
                              (workspace, requested)).fetchone():
            with closing(sqlite3.connect(app.database)) as reader:
                observed.append(reader.execute('SELECT count(*) FROM catalog_membership '
                    'WHERE workspace=? AND collection=?', (workspace, requested)).fetchone()[0])
            raise RuntimeError('injected EPUB membership rejection')

    metadata = dict(title='Confirmed catalog title', authors=('Confirmed catalog author',),
                    collections=(existing, requested), work_id='work', edition_id='edition')
    with monkeypatch.context() as patch:
        patch.setattr(Storage, '_check', reject_membership)
        with pytest.raises(RuntimeError, match='injected EPUB membership rejection'):
            app._import_file(workspace, source, **metadata)
    assert observed == [0]
    assert app.browse(workspace, collection=requested) == ()
    assert open_ebook(app, workspace, 'edition') == before
    replay = app._import_file(workspace, source, **metadata)
    assert replay['id'] == first['id'] and len(app.browse(workspace, collection=requested)) == 1


def test_incompatible_book_and_cancelled_import_never_publish(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Rejected inputs')
    source = tmp_path / 'synthetic.epub'
    source.write_bytes(synthetic_epub())
    with pytest.raises(InterruptedError):
        app._import_file(workspace, source, cancelled=lambda: True)
    assert app.browse(workspace) == ()
    data = BytesIO()
    with ZipFile(BytesIO(synthetic_epub())) as original, ZipFile(data, 'w') as changed:
        for info in original.infolist():
            raw = original.read(info)
            if info.filename == 'EPUB/first.xhtml':
                raw = raw.replace(b'<em>original</em>', b'<ruby>unsupported<rt>annotation</rt></ruby>')
            changed.writestr(info, raw)
    source.write_bytes(data.getvalue())
    with pytest.raises(ValueError, match='unsupported'):
        app._import_file(workspace, source)
    assert app.browse(workspace) == () and app.search(workspace, 'Залізнична') == ()


def test_revision_commits_contents_descriptors_with_no_invented_pages(imported):
    app, workspace, _, _, result = imported
    with app.read(workspace) as db:
        config, = db.execute('SELECT config FROM revision WHERE id=?', (result['id'],)).fetchone()
        settings = json.loads(config)
        assert settings['adapter'] == 'epub-spine-v1'
        assert settings['kind'] == 'logical_segment'
        assert all(tuple(row) == (None, None, 0) for row in db.execute(
            'SELECT physical,printed,verified FROM page WHERE revision=?', (result['id'],)))
        assert settings['ebook']['chapters'][1]['text_sha256'] == sha256(
            read_chapter(app, workspace, 'edition', result['id'], 2)['raw_text'].encode('utf-8')).hexdigest()
