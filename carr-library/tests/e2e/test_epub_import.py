"""Independent EPUB application checks with real archives, workers and SQLite.

The synthetic book deliberately disagrees with ZIP/manifest filename order.
These checks make no screen-reader, rendered-layout or citation-review claim.
"""
from hashlib import sha256
from io import BytesIO
import os
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest.worker import _store, load_request
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.source_file import launch_source_file, prepare_source_file


FIRST_TEXT = 'Railway theory. Наукова бібліотека — štúdia. Originmarker.'
SECOND_TEXT = 'Railway practice. Дослідження джерел.'


def write_epub(path, *, first_text=FIRST_TEXT, repeat_first=False, unsupported=False):
    """Create a complete tiny EPUB without borrowing copyrighted source text."""
    container = '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="EPUB/package.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
    first_mime = 'image/svg+xml' if unsupported else 'application/xhtml+xml'
    repeated = '<itemref idref="first"/>' if repeat_first else ''
    package = f'''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">urn:uuid:carr-synthetic-epub</dc:identifier>
    <dc:title>Embedded title is only a metadata hint</dc:title>
    <dc:creator>Embedded creator</dc:creator><dc:language>uk</dc:language>
    <meta property="dcterms:modified">2026-11-26T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="second" href="text/a-second.xhtml" media-type="application/xhtml+xml"/>
    <item id="nav" href="navigation.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="first" href="text/z-first.xhtml" media-type="{first_mime}"/>
  </manifest>
  <spine><itemref idref="first"/><itemref idref="second"/>{repeated}</spine>
</package>'''
    first = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="uk">
  <head><title>First chapter</title></head>
  <body><h1 id="first">First chapter</h1><p>{escape(first_text)}</p></body>
</html>'''
    if unsupported:
        first = '<svg xmlns="http://www.w3.org/2000/svg"><text>Unsupported chapter</text></svg>'
    second = f'''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="uk">
  <head><title>Second chapter</title></head>
  <body><h1 id="second">Second chapter</h1><p>{escape(SECOND_TEXT)}</p></body>
</html>'''
    nav = '''<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
  <head><title>Contents</title></head>
  <body><nav epub:type="toc" id="toc"><h1>Contents</h1><ol>
    <li><a href="text/z-first.xhtml#first">First chapter</a></li>
    <li><a href="text/a-second.xhtml#second">Second chapter</a></li>
  </ol></nav></body>
</html>'''
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        entries = (
            ('mimetype', 'application/epub+zip'),
            ('EPUB/text/a-second.xhtml', second),
            ('EPUB/text/z-first.xhtml', first),
            ('EPUB/navigation.xhtml', nav),
            ('META-INF/container.xml', container),
            ('EPUB/package.opf', package),
        )
        for name, text in entries:
            info = ZipInfo(name, date_time=(2026, 11, 26, 0, 0, 0))
            info.compress_type = ZIP_STORED if name == 'mimetype' else ZIP_DEFLATED
            archive.writestr(info, text.encode('utf-8'))
    path.write_bytes(output.getvalue())
    return output.getvalue()


@pytest.fixture
def app(tmp_path):
    return LibraryApp(tmp_path / 'library')


def assert_no_publication(app, workspace):
    assert app.browse(workspace) == ()
    assert app.search(workspace, 'Railway') == ()
    with app.read(workspace) as db:
        for table in ('catalog_edition', 'revision', 'page', 'source_metadata'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0


def test_epub_worker_preserves_order_metadata_and_unverified_logical_locations(app, tmp_path):
    source = tmp_path / 'Бібліотека.epub'
    original = write_epub(source)
    workspace = app.create_workspace('Academic sources')
    collection = app.create_collection(workspace, 'Selected books')
    topic = app.create_topic(workspace, 'Source quotations')
    result = app.import_file(
        workspace, source, title='Confirmed title', authors=('Олена Коваль', 'Ján Novák'),
        language='uk', edition='Teaching edition', volume='II', collections=(collection,),
    )
    assert result['state'] == 'complete', result
    assert result['isolated'] is True and result['verified'] is False
    assert result['completed'] == 2
    book, = app.browse(workspace, collection=collection)
    assert (book.title, book.authors, book.language, book.edition, book.volume) == (
        'Confirmed title', ('Олена Коваль', 'Ján Novák'), 'uk', 'Teaching edition', 'II',
    )
    assert book.asset == sha256(original).hexdigest()
    request = load_request(app.root, result['request_id'])
    assert request['asset_sha256'] == book.asset
    assert request['metadata']['title'] == 'Confirmed title'
    with app.read(workspace) as db:
        pages = db.execute('SELECT ordinal,raw,physical,printed,verified,digest FROM page '
                           'WHERE revision=? ORDER BY ordinal', (result['id'],)).fetchall()
    assert len(pages) == 2
    for ordinal, row, expected in zip((1, 2), pages, (FIRST_TEXT, SECOND_TEXT)):
        assert row['ordinal'] == ordinal and expected in row['raw']
        assert (row['physical'], row['printed'], row['verified']) == (None, None, 0)
        assert row['digest'] == sha256(row['raw'].encode('utf-8')).hexdigest()
    hits = app.search(workspace, 'Railway', collection=collection)
    assert len(hits) == 2
    assert all(not hit.verified and 'Segment' in hit.page and 'PDF page' not in hit.page
               for hit in hits)
    first = next(hit for hit in hits if FIRST_TEXT in app.preview(workspace, hit.ref, hit.revision))
    preview = app.preview(workspace, first.ref, first.revision)
    assert SECOND_TEXT not in preview
    assert len(app.search(workspace, 'Наукова бібліотека')) == 1
    assert len(app.search(workspace, 'štúdia')) == 1
    with pytest.raises(ValueError, match='unverified'):
        app.copy(workspace, first.ref, first.revision, 'quote')
    with pytest.raises(ValueError, match='unverified'):
        app.save_hit(workspace, topic, first.ref, first.revision)
    assert app.evidence(workspace, topic) == ()
    assert source.read_bytes() == original


def test_repeated_spine_occurrences_remain_distinct_logical_segments(app, tmp_path):
    source = tmp_path / 'repeated.epub'
    write_epub(source, repeat_first=True)
    workspace = app.create_workspace('Repeated reading order')
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete' and result['completed'] == 3, result
    hits = app.search(workspace, 'Originmarker')
    assert len(hits) == 2
    assert {prepare_source_file(app, workspace, hit.ref, hit.revision).locator.index
            for hit in hits} == {1, 3}
    assert len({hit.ref for hit in hits}) == 2


def test_epub_sections_share_exact_locations_with_reading_desk_bookmarks(app, tmp_path):
    from carr_library.reader.ebook import read_ebook_section
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.reader.reading_state import ReadingState
    from carr_library.ui.reading_desk import ReaderController

    source = tmp_path / 'bookmark-sections.epub'
    original = write_epub(source, repeat_first=True)
    workspace = app.create_workspace('EPUB reading integration')
    result = app.import_file(workspace, source, title='Book with repeated sections')
    assert result['state'] == 'complete', result
    book, = app.browse(workspace)
    reader = ReaderController(ReadingDesk(app, workspace, book.edition_id), ReadingState(app.root))
    first = reader.start()
    third = reader.go(3)
    assert first.raw_text == third.raw_text and first.location != third.location
    assert third.verified is False and third.location.kind == 'logical_segment'
    bookmark = reader.add_bookmark('Repeated section for seminar', third.location)

    reopened = LibraryApp(app.root)
    resumed = ReaderController(ReadingDesk(reopened, workspace, book.edition_id),
                               ReadingState(reopened.root))
    assert resumed.start().location == bookmark.location
    resumed.go(2)
    assert resumed.open_bookmark(bookmark).location == third.location
    section = read_ebook_section(reopened, workspace, book.edition_id, result['id'], 3)
    assert section.raw_text == third.raw_text and section.position == 3 and not section.verified
    assert reopened._selections == {} and source.read_bytes() == original

    reopened.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        resumed.open_bookmark(bookmark)
    with pytest.raises(ValueError, match='archived'):
        read_ebook_section(reopened, workspace, book.edition_id, result['id'], 3)


def test_epub_original_survives_reopen_and_backup_restore_exactly(app, tmp_path):
    source = tmp_path / 'retained.epub'
    original = write_epub(source)
    workspace = app.create_workspace('Retained books')
    result = app.import_file(workspace, source, title='Retained EPUB')
    assert result['state'] == 'complete', result
    book, = app.browse(workspace)
    hit, = app.search(workspace, 'Originmarker')
    before = app.preview(workspace, hit.ref, hit.revision)
    source.unlink()
    selected = prepare_source_file(app, workspace, hit.ref, hit.revision)
    assert selected.path.suffix == '.epub' and selected.mime == 'application/epub+zip'
    assert selected.locator.kind == 'logical_segment' and selected.locator.index == 1
    assert selected.path.read_bytes() == original
    assert not os.path.samefile(selected.path, app.assets.resolve(book.asset))
    opened = []
    launch_source_file(app, selected, lambda path: opened.append(path) is None)
    assert opened == [str(selected.path)]
    reopened = LibraryApp(app.root)
    again, = reopened.search(workspace, 'Originmarker')
    assert reopened.preview(workspace, again.ref, again.revision) == before
    backup = tmp_path / 'backup'
    digest = create_backup(reopened, backup)
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    after, = restored.search(workspace, 'Originmarker')
    assert after.revision == hit.revision and after.verified is False
    assert restored.preview(workspace, after.ref, after.revision) == before
    assert prepare_source_file(restored, workspace, after.ref, after.revision).path.read_bytes() == original
    assert restored.assets.resolve(book.asset).read_bytes() == original
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM source_page_review').fetchone()[0] == 0


def test_epub_same_ids_and_assets_remain_isolated_between_libraries(app, tmp_path):
    source = tmp_path / 'shared.epub'
    write_epub(source)
    one = app.create_workspace('Library one')
    two = app.create_workspace('Library two')
    first = app.import_file(one, source, work_id='same-work', edition_id='same-edition')
    second = app.import_file(two, source, work_id='same-work', edition_id='same-edition')
    assert first['state'] == second['state'] == 'complete', (first, second)
    assert first['id'] != second['id']
    assert len(tuple(app.assets_dir.glob('*.epub'))) == 1
    hit, = app.search(one, 'Originmarker')
    collection = app.create_collection(one, 'Only library one')
    with app.catalog_transaction(one) as catalog:
        catalog.assign(collection, 'same-edition')
    assert len(app.search(two, 'Originmarker')) == 1
    with pytest.raises(ValueError):
        app.preview(two, hit.ref, hit.revision)
    with pytest.raises(ValueError):
        prepare_source_file(app, two, hit.ref, hit.revision)
    with pytest.raises(ValueError):
        app.search(two, 'Originmarker', collection=collection)
    with pytest.raises(ValueError, match='another library'):
        app.resume_import(two, first['request_id'])
    assert {r['request_id'] for r in app.import_requests(two)} == {second['request_id']}


def test_exact_epub_replay_reuses_revision_and_adds_only_requested_memberships(app, tmp_path):
    source = tmp_path / 'repeat.epub'
    write_epub(source)
    workspace = app.create_workspace('Idempotent imports')
    one = app.create_collection(workspace, 'One')
    two = app.create_collection(workspace, 'Two')
    metadata = dict(title='Stable book', authors=('Stable author',),
                    work_id='stable-work', edition_id='stable-edition')
    first = app.import_file(workspace, source, collections=(one,), **metadata)
    second = app.import_file(workspace, source, collections=(two,), **metadata)
    assert first['state'] == second['state'] == 'complete', (first, second)
    assert first['id'] == second['id']
    assert len(app.browse(workspace)) == 1
    assert app.browse(workspace, collection=one) == app.browse(workspace, collection=two)
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM source_metadata').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM page').fetchone()[0] == 2
    conflict = app.import_file(workspace, source, **dict(metadata, title='Conflicting title'))
    assert conflict['state'] == 'failed'
    assert app.browse(workspace)[0].title == 'Stable book'


def test_changed_epub_requires_distinct_edition_and_preserves_old_selected_source(app, tmp_path):
    source = tmp_path / 'revised.epub'
    original = write_epub(source)
    workspace = app.create_workspace('Source revisions')
    metadata = dict(title='Academic book', authors=('Author',), work_id='work')
    first = app.import_file(workspace, source, edition_id='original', **metadata)
    assert first['state'] == 'complete', first
    old, = app.search(workspace, 'Originmarker')
    old_preview = app.preview(workspace, old.ref, old.revision)
    replacement = write_epub(source, first_text='Replacementtoken. Revised research text.')
    conflict = app.import_file(workspace, source, edition_id='original', **metadata)
    assert conflict['state'] == 'failed'
    assert app.preview(workspace, old.ref, old.revision) == old_preview
    newer = app.import_file(workspace, source, edition_id='revised', **metadata)
    assert newer['state'] == 'complete' and newer['id'] != first['id'], newer
    current, = app.search(workspace, 'Replacementtoken')
    assert current.edition_id == 'revised'
    assert app.preview(workspace, old.ref, old.revision) == old_preview
    assert prepare_source_file(app, workspace, old.ref, old.revision).path.read_bytes() == original
    assert prepare_source_file(app, workspace, current.ref, current.revision).path.read_bytes() == replacement
    assert len(app.search(workspace, 'Originmarker')) == 1
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 2


def test_epub_commit_survives_lost_worker_reply_and_missing_original_path(app, tmp_path):
    source = tmp_path / 'Original title.epub'
    write_epub(source)
    workspace = app.create_workspace('Worker recovery')
    first = app.import_file(workspace, source)
    assert first['state'] == 'complete', first
    request = load_request(app.root, first['request_id'])
    request['state'] = 'running'
    request.pop('result')
    _store(app.root, first['request_id'], request)
    source.unlink()
    recovered = LibraryApp(app.root).resume_import(workspace, first['request_id'])
    assert recovered['state'] == 'complete' and recovered['id'] == first['id'], recovered
    assert app.browse(workspace)[0].title == 'Original title'
    assert len(app.search(workspace, 'Railway')) == 2
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 1


def test_cancelled_epub_has_no_partial_catalog_and_can_resume(app, tmp_path):
    source = tmp_path / 'cancelled.epub'
    write_epub(source)
    workspace = app.create_workspace('Cancellation')
    cancelled = app.import_file(workspace, source, cancelled=lambda: True)
    assert cancelled['state'] == 'cancelled' and cancelled['isolated'] is True
    assert_no_publication(app, workspace)
    resumed = app.resume_import(workspace, cancelled['request_id'])
    assert resumed['state'] == 'complete' and resumed['completed'] == 2, resumed


def test_unsupported_primary_epub_content_cannot_publish_partial_catalog(app, tmp_path):
    source = tmp_path / 'unsupported.epub'
    write_epub(source, unsupported=True)
    workspace = app.create_workspace('Unsupported source')
    result = app.import_file(workspace, source)
    assert result['state'] == 'failed' and result['isolated'] is True, result
    assert result.get('message')
    assert_no_publication(app, workspace)


def test_interrupted_epub_publication_rolls_back_catalog_then_public_retry_succeeds(app, tmp_path, monkeypatch):
    from carr_library.storage.revisions import Storage

    source = tmp_path / 'publication.epub'
    write_epub(source)
    workspace = app.create_workspace('Publication rollback')
    metadata = dict(title='Rollback source', work_id='work', edition_id='edition')
    original = Storage.stage_revision

    def interrupted(storage, *args, **kwargs):
        original(storage, *args, **kwargs)
        raise RuntimeError('simulated EPUB publication interruption')

    with monkeypatch.context() as patch:
        patch.setattr(Storage, 'stage_revision', interrupted)
        with pytest.raises(RuntimeError, match='publication interruption'):
            app._import_file(workspace, source, **metadata)
    assert_no_publication(app, workspace)
    result = app.import_file(workspace, source, **metadata)
    assert result['state'] == 'complete' and result['completed'] == 2, result
    assert len(app.browse(workspace)) == 1
    assert len(app.search(workspace, 'Railway')) == 2
