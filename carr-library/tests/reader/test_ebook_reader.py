"""Direct chapter-reader behavior over real imported and retained EPUB files."""
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

from carr_library.application import LibraryApp
from carr_library.reader.ebook import list_ebooks, read_ebook_section


def book(path):
    container = '''<container version="1.0"
      xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>
      <rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/>
      </rootfiles></container>'''
    package = '''<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:identifier id="id">synthetic-reader-book</dc:identifier>
        <dc:title>Source metadata hint</dc:title><dc:language>uk</dc:language>
        <meta property="dcterms:modified">2026-11-26T00:00:00Z</meta>
      </metadata><manifest>
        <item id="two" href="chapters/a.xhtml" media-type="application/xhtml+xml"/>
        <item id="one" href="chapters/z.xhtml" media-type="application/xhtml+xml"/>
        <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
        <item id="image" href="diagram.svg" media-type="image/svg+xml"/>
      </manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>'''
    first = '''<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="uk">
      <head><title>Перший розділ</title></head><body>
      <h1 id="first">Перший розділ</h1><p>Railway research. Словацька štúdia.</p>
      <img src="../diagram.svg" alt="Авторський опис діаграми джерел."/>
      </body></html>'''
    second = '''<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="uk">
      <head><title>Другий розділ</title></head><body>
      <h1 id="second">Другий розділ</h1><p>Archive context. Українські джерела.</p>
      </body></html>'''
    nav = '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
      <head><title>Contents</title></head><body><nav epub:type="toc" id="toc"><ol>
      <li><a href="chapters/z.xhtml#first">Перший розділ</a></li>
      <li><a href="chapters/a.xhtml#second">Другий розділ</a></li>
      </ol></nav></body></html>'''
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        for name, text in (
            ('mimetype', 'application/epub+zip'),
            ('META-INF/container.xml', container),
            ('OPS/package.opf', package),
            ('OPS/chapters/a.xhtml', second),
            ('OPS/chapters/z.xhtml', first),
            ('OPS/nav.xhtml', nav),
            ('OPS/diagram.svg', '<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'),
        ):
            info = ZipInfo(name, date_time=(2026, 11, 26, 0, 0, 0))
            info.compress_type = ZIP_STORED if name == 'mimetype' else ZIP_DEFLATED
            archive.writestr(info, text.encode('utf-8'))
    path.write_bytes(output.getvalue())


@pytest.fixture
def imported(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Chapter reading')
    source = tmp_path / 'chapters.epub'
    book(source)
    result = app.import_file(workspace, source, title='User supplied title',
                             authors=('Research author',), work_id='work', edition_id='edition')
    assert result['state'] == 'complete', result
    return app, workspace, source, result


def test_chapters_are_available_without_search_and_keep_source_order(imported):
    app, workspace, source, result = imported
    app.add_text(workspace, 'Unrelated plain text', 'Separate source text.')
    summaries = list_ebooks(app, workspace)
    assert len(summaries) == 1
    summary, = summaries
    assert (summary.edition_id, summary.revision, summary.title) == (
        'edition', result['id'], 'User supplied title',
    )
    assert app._selections == {}
    first = read_ebook_section(app, workspace, 'edition', result['id'], 1)
    second = read_ebook_section(app, workspace, 'edition', result['id'], 2)
    assert (first.position, second.position, first.count, second.count) == (1, 2, 2, 2)
    assert first.title == 'Перший розділ' and second.title == 'Другий розділ'
    assert first.resource == 'OPS/chapters/z.xhtml'
    assert second.resource == 'OPS/chapters/a.xhtml'
    assert 'Railway research. Словацька štúdia.' in first.raw_text
    assert 'Archive context.' not in first.raw_text
    assert 'Archive context. Українські джерела.' in second.raw_text
    assert 'Railway research.' not in second.raw_text
    assert first.linear is True and second.linear is True
    assert first.verified is False and second.verified is False
    assert first.warnings and second.warnings
    assert first.alternatives == ((1, 'Авторський опис діаграми джерел.'),)
    assert second.alternatives == ()
    assert 'Авторський опис діаграми джерел.' not in first.raw_text
    source.unlink()
    reopened = LibraryApp(app.root)
    assert read_ebook_section(reopened, workspace, 'edition', result['id'], 1) == first
    assert reopened._selections == {}


@pytest.mark.parametrize('position', [0, -1, 3, True, 1.0, '1'])
def test_invalid_chapter_positions_do_not_fall_back_to_another_chapter(imported, position):
    app, workspace, _, result = imported
    with pytest.raises((ValueError, IndexError, TypeError)):
        read_ebook_section(app, workspace, 'edition', result['id'], position)


def test_foreign_library_and_topic_are_rejected_before_original_is_read(imported, monkeypatch):
    app, workspace, _, result = imported
    other = app.create_workspace('Other library')
    foreign_topic = app.create_topic(other, 'Private topic')

    def forbidden(*args, **kwargs):
        pytest.fail('Unauthorized chapter request reached original source bytes.')

    monkeypatch.setattr(app.assets, 'resolve', forbidden)
    assert list_ebooks(app, other) == ()
    with pytest.raises(ValueError):
        read_ebook_section(app, other, 'edition', result['id'], 1)
    with pytest.raises(ValueError):
        list_ebooks(app, workspace, authorized_topic=foreign_topic)
    with pytest.raises(ValueError):
        read_ebook_section(app, workspace, 'edition', result['id'], 1,
                          authorized_topic=foreign_topic)


def test_archived_topic_and_workspace_invalidate_reader_requests(imported):
    app, workspace, _, result = imported
    topic = app.create_topic(workspace, 'Current topic')
    before = read_ebook_section(app, workspace, 'edition', result['id'], 1,
                               authorized_topic=topic)
    assert before.raw_text
    with app.topic_transaction(workspace) as tree:
        tree.archive(topic)
    with pytest.raises(ValueError, match='archived'):
        list_ebooks(app, workspace, authorized_topic=topic)
    with pytest.raises(ValueError, match='archived'):
        read_ebook_section(app, workspace, 'edition', result['id'], 1,
                          authorized_topic=topic)
    app.archive_workspace(workspace)
    with pytest.raises(ValueError, match='archived'):
        list_ebooks(app, workspace)
    with pytest.raises(ValueError, match='archived'):
        read_ebook_section(app, workspace, 'edition', result['id'], 1)


@pytest.mark.parametrize('operation', ['list', 'read'])
def test_reader_cancellation_is_checked_before_original_access(imported, monkeypatch, operation):
    app, workspace, _, result = imported

    def forbidden(*args, **kwargs):
        pytest.fail('Cancelled chapter request reached original source bytes.')

    monkeypatch.setattr(app.assets, 'resolve', forbidden)
    with pytest.raises((InterruptedError, TimeoutError), match='cancel'):
        if operation == 'list':
            list_ebooks(app, workspace, cancelled=lambda: True)
        else:
            read_ebook_section(app, workspace, 'edition', result['id'], 1,
                              cancelled=lambda: True)


def test_reader_checks_retained_original_again_on_every_section_request(imported):
    app, workspace, _, result = imported
    summary, = list_ebooks(app, workspace)
    read_ebook_section(app, workspace, summary.edition_id, summary.revision, 1)
    asset = app.browse(workspace)[0].asset
    original = app.assets.resolve(asset)
    raw = original.read_bytes()
    original.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    with pytest.raises((ValueError, OSError)):
        read_ebook_section(app, workspace, 'edition', result['id'], 1)


def test_metadata_revision_does_not_retarget_an_existing_reader_location(imported):
    app, workspace, _, result = imported
    original = read_ebook_section(app, workspace, 'edition', result['id'], 2)
    revision = app.update_metadata(workspace, 'edition', title='Corrected catalog title',
                                   authors=('Corrected author',))
    assert revision != result['id']
    summary, = list_ebooks(app, workspace)
    assert summary.revision == revision and summary.title == 'Corrected catalog title'
    assert read_ebook_section(app, workspace, 'edition', result['id'], 2) == original
    current = read_ebook_section(app, workspace, 'edition', revision, 2)
    assert current.raw_text == original.raw_text
    assert current.position == original.position
    with pytest.raises(ValueError):
        read_ebook_section(app, workspace, 'another-edition', result['id'], 2)
    with pytest.raises(ValueError):
        read_ebook_section(app, workspace, 'edition', 'missing-revision', 2)
