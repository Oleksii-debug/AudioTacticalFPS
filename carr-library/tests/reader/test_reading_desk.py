"""Full-source reading on real SQLite, retained bytes and native import adapters."""
from contextlib import closing
from dataclasses import replace
import sqlite3
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.citations.anchors import digest
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.storage.revisions import Page, Storage


@pytest.fixture
def text_book(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Reading library')
    raw = 'Передмова\r\nStraße. Україна 🚂.\f\fSecond section. Preserved end.\n'
    book = app.add_text(workspace, 'Synthetic source', raw,
                        authors=('Synthetic Author',), language='uk', edition='First')
    assert book['state'] == 'complete'
    return app, workspace, book, raw


def _mutate_page(app, revision, sql, values):
    """Corrupt synthetic bytes while restoring the exact expected DDL identity."""
    with sqlite3.connect(app.database) as db:
        trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='page_bu'").fetchone()[0]
        db.execute('DROP TRIGGER page_bu')
        db.execute(sql, (*values, revision))
        db.execute(trigger)


def test_full_text_reader_has_no_search_dependency_and_keeps_exact_unicode(text_book):
    app, workspace, book, raw = text_book
    desk = ReadingDesk(app, workspace, book['edition'])
    assert app._selections == {}
    assert [page.ordinal for page in desk.pages] == [1, 2, 3]
    assert [page.locator.index for page in desk.pages] == [1, 2, 3]
    assert desk.title == 'Synthetic source' and desk.author == 'Synthetic Author'
    pages = [desk.page(n) for n in range(1, 4)]
    assert [page.raw_text for page in pages] == raw.split('\f')
    assert all(page.verified is True for page in pages)
    assert pages[1].raw_text == ''
    assert pages[2].location.text_sha256 == digest(raw.split('\f')[2])
    assert pages[2].location.asset_sha256 == book['asset']
    assert 'Segment 3' in pages[2].description
    assert 'unpaginated' in pages[2].description
    assert 'Physical page' not in pages[2].description
    assert app._selections == {}


def test_reopen_and_resolve_exact_saved_page(text_book):
    app, workspace, book, _ = text_book
    first = ReadingDesk(app, workspace, book['edition']).page(3)
    reopened = LibraryApp(app.root)
    desk = ReadingDesk(reopened, workspace, book['edition'], revision=first.location.revision)
    assert desk.validate(first.location) == first
    assert desk.page(3).text == first.raw_text


@pytest.mark.parametrize('bad', [0, -1, 4, True, 1.0, '1', None])
def test_invalid_navigation_never_wraps_or_coerces(text_book, bad):
    app, workspace, book, _ = text_book
    desk = ReadingDesk(app, workspace, book['edition'])
    with pytest.raises(ValueError, match='within this book'):
        desk.page(bad)


@pytest.mark.parametrize('field,value', [
    ('workspace', 'other-library'), ('edition', 'other-edition'),
    ('revision', 'other-revision'), ('asset_sha256', '0' * 64),
    ('text_sha256', '0' * 64), ('index', 999), ('kind', 'physical_page'),
])
def test_bookmarks_are_bound_to_exact_authorized_source(text_book, field, value):
    app, workspace, book, _ = text_book
    desk = ReadingDesk(app, workspace, book['edition'])
    location = desk.page(1).location
    with pytest.raises(ValueError, match='saved position'):
        desk.validate(replace(location, **{field: value}))


def test_cross_library_access_denied_before_original_bytes(text_book, monkeypatch):
    app, workspace, book, _ = text_book
    other = app.create_workspace('Other library')

    def forbidden(*args, **kwargs):
        pytest.fail('An unauthorized edition must not read original bytes.')

    monkeypatch.setattr(app.assets, 'resolve', forbidden)
    with pytest.raises(ValueError, match='unknown edition'):
        ReadingDesk(app, other, book['edition'])


def test_archive_revokes_open_reader_and_bookmark(text_book):
    app, workspace, book, _ = text_book
    desk = ReadingDesk(app, workspace, book['edition'])
    location = desk.page(2).location
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        desk.page(1)
    with pytest.raises(ValueError, match='archived'):
        desk.validate(location)


def test_topic_authorization_is_rechecked_for_every_page(text_book):
    app, workspace, book, _ = text_book
    topic = app.create_topic(workspace, 'Reader scope')
    desk = ReadingDesk(app, workspace, book['edition'], topic=topic)
    page = desk.page(1)
    with app.topic_transaction(workspace) as tree:
        tree.archive(topic)
    with pytest.raises(ValueError):
        desk.validate(page.location)
    # Archiving the topic does not archive the independent parent library.
    assert ReadingDesk(app, workspace, book['edition']).page(1).raw_text == page.raw_text


def test_metadata_revision_invalidates_session_without_retargeting_saved_position(text_book):
    app, workspace, book, _ = text_book
    desk = ReadingDesk(app, workspace, book['edition'])
    location = desk.page(3).location
    new_revision = app.update_metadata(workspace, book['edition'], title='Revised catalog title',
                                       authors=('New Metadata Author',))
    assert new_revision != desk.revision
    with pytest.raises(ValueError, match='revision changed'):
        desk.page(1)
    with pytest.raises(ValueError, match='revision changed'):
        desk.validate(location)
    with pytest.raises(ValueError, match='no longer active'):
        ReadingDesk(app, workspace, book['edition'], revision=location.revision)
    new = ReadingDesk(app, workspace, book['edition'])
    assert new.page(3).location.revision == new_revision
    with pytest.raises(ValueError, match='different library, edition or revision'):
        new.validate(location)


@pytest.mark.parametrize('damage', ['missing', 'same_size', 'extended'])
def test_original_tampering_is_error_and_not_unverified_fallback(text_book, damage):
    app, workspace, book, _ = text_book
    desk = ReadingDesk(app, workspace, book['edition'])
    location = desk.page(1).location
    path = app.assets.resolve(book['asset'])
    original = path.read_bytes()
    if damage == 'missing':
        path.unlink()
    elif damage == 'same_size':
        path.write_bytes(original.replace(b'Second', b'Broken'))
    else:
        path.write_bytes(original + b'Added text')
    with pytest.raises(ValueError):
        desk.page(1)
    with pytest.raises(ValueError):
        desk.validate(location)
    with pytest.raises(ValueError):
        ReadingDesk(app, workspace, book['edition'])


def test_damaged_stored_text_is_hashed_on_every_read(text_book):
    app, workspace, book, _ = text_book
    desk = ReadingDesk(app, workspace, book['edition'])
    original = desk.page(1)
    _mutate_page(app, desk.revision,
        'UPDATE page SET raw=? WHERE ordinal=1 AND revision=?', ('Corrupt stored text',))
    with pytest.raises(ValueError, match='damaged'):
        desk.page(1)
    with pytest.raises(ValueError, match='damaged'):
        desk.validate(original.location)


def test_resealed_page_digest_does_not_retarget_existing_session(text_book):
    app, workspace, book, _ = text_book
    desk = ReadingDesk(app, workspace, book['edition'])
    _mutate_page(app, desk.revision,
        'UPDATE page SET raw=?,digest=? WHERE ordinal=1 AND revision=?',
        ('Changed and rehashed', digest('Changed and rehashed')))
    with pytest.raises(ValueError, match='identity changed'):
        desk.page(1)
    # Reopening must not turn tampered plaintext into an unverified extraction:
    # UTF-8 originals can and must be authenticated exactly without human review.
    fresh = ReadingDesk(app, workspace, book['edition'])
    with pytest.raises(ValueError, match='original UTF-8 source'):
        fresh.page(1)


def test_native_verification_consumes_the_reader_deadline(text_book, monkeypatch):
    import carr_library.application as application
    app, workspace, book, _ = text_book
    desk = ReadingDesk(app, workspace, book['edition'], timeout=1.0)
    clock = [100.0]
    original = app.assets.resolve

    def slow_resolution(asset):
        result = original(asset)
        clock[0] += 2.0
        return result

    monkeypatch.setattr(application, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(app.assets, 'resolve', slow_resolution)
    with pytest.raises(TimeoutError, match='cancelled|timed out'):
        desk.page(1)


def test_cancellation_during_original_check_never_returns_a_page(text_book, monkeypatch):
    app, workspace, book, _ = text_book
    stopped = Event()
    desk = ReadingDesk(app, workspace, book['edition'], cancelled=stopped.is_set)
    original = app.assets.resolve

    def cancel_after_resolution(asset):
        result = original(asset)
        stopped.set()
        return result

    monkeypatch.setattr(app.assets, 'resolve', cancel_after_resolution)
    with pytest.raises(TimeoutError, match='cancelled|timed out'):
        desk.page(1)
    assert app._selections == {}


def test_reader_limits_reject_oversized_page_before_text_control(text_book, monkeypatch):
    app, workspace, book, _ = text_book
    desk = ReadingDesk(app, workspace, book['edition'])
    monkeypatch.setattr('carr_library.reader.reading_desk.MAX_PAGE_CHARACTERS', 5)
    with pytest.raises(ValueError, match='too large'):
        desk.page(1)


def test_reader_page_inventory_is_bounded(text_book, monkeypatch):
    app, workspace, book, _ = text_book
    monkeypatch.setattr('carr_library.reader.reading_desk.MAX_PAGES', 2)
    with pytest.raises(ValueError, match='page limit'):
        ReadingDesk(app, workspace, book['edition'])


def test_unreviewed_pdf_reading_does_not_mint_quote_authority(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    source = tmp_path / 'source.pdf'
    canvas = Canvas(str(source), invariant=1, pageCompression=0)
    for n in range(1, 3):
        canvas.drawString(50, 700, f'Synthetic physical PDF page {n}.')
        canvas.showPage()
    canvas.save()
    app = LibraryApp(tmp_path / 'pdf-library')
    workspace = app.create_workspace('PDF sources')
    book = app.import_file(workspace, source, title='PDF reading fixture')
    assert book['state'] == 'complete'
    desk = ReadingDesk(app, workspace, book['edition'])
    page = desk.page(2)
    assert 'Synthetic physical PDF page 2' in page.raw_text
    assert not page.verified and 'unverified' in page.status
    assert page.location.kind == 'physical_page' and page.location.index == 2
    assert 'printed page unverified' in page.description
    assert app._selections == {}
    hit = app.search(workspace, 'Synthetic')[0]
    with pytest.raises(ValueError, match='unverified'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')


def test_docx_reading_is_explicitly_unpaginated_and_unverified(tmp_path):
    from docx import Document
    source = tmp_path / 'source.docx'
    document = Document()
    document.add_paragraph('Synthetic DOCX reading passage.')
    document.save(source)
    app = LibraryApp(tmp_path / 'docx-library')
    workspace = app.create_workspace('DOCX sources')
    book = app.import_file(workspace, source)
    assert book['state'] == 'complete'
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    assert 'Synthetic DOCX reading passage' in page.raw_text
    assert not page.verified
    assert page.location.kind == 'logical_segment'
    assert 'unpaginated' in page.description and 'Physical page' not in page.description


def test_noncontiguous_physical_locations_use_reading_order_not_fake_page_numbers(tmp_path):
    from reportlab.pdfgen.canvas import Canvas
    source = tmp_path / 'ten-pages.pdf'
    canvas = Canvas(str(source), invariant=1, pageCompression=0)
    for n in range(1, 11):
        canvas.drawString(50, 700, f'Synthetic page {n}.')
        canvas.showPage()
    canvas.save()
    app = LibraryApp(tmp_path / 'sparse-library')
    workspace = app.create_workspace('Sparse extracted pages')
    book = app.import_file(workspace, source, title='Sparse source')
    with closing(Storage(app.database)) as storage:
        with storage.transaction():
            rows = storage.db.execute(
                'SELECT raw,physical FROM page WHERE revision=? AND physical IN (5,9) ORDER BY physical',
                (book['id'],)).fetchall()
            assert len(rows) == 2
            storage.stage_revision(workspace, book['edition'], 'reader-sparse', book['asset'],
                'synthetic-extraction', [Page(rows[0]['raw'], 5, 'v', True), Page(rows[1]['raw'], 9)])
            storage.db.execute(
                'INSERT INTO source_metadata SELECT workspace,edition,?,author,title,volume,edition_label '
                'FROM source_metadata WHERE workspace=? AND edition=? AND revision=?',
                ('reader-sparse', workspace, book['edition'], book['id']))
    desk = ReadingDesk(app, workspace, book['edition'])
    assert [p.ordinal for p in desk.pages] == [1, 2]
    assert [p.locator.index for p in desk.pages] == [5, 9]
    assert 'Physical page 5' in desk.pages[0].description
    first = desk.page(1)
    second = desk.page(2)
    assert first.location.index == 5 and second.location.index == 9
    assert not first.verified and not first.locator.verified
    assert 'printed page unverified' in first.description
    assert 'p. v' not in first.description
    assert desk.validate(second.location) == second
    with pytest.raises(ValueError, match='within this book'):
        desk.page(9)
