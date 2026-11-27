"""EPUB section previews survive the real selected-library search handoff."""
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest

from carr_library.application import LibraryApp
from carr_library.search.federated import FederatedSearch


PHRASE = 'Shared evidence phrase'
FIRST_TEXT = PHRASE + ' — Науковий уривок & archival context.'
SECOND_TEXT = 'A separate section must not be stitched into the selected excerpt.'


def _write_epub(path):
    """Small synthetic EPUB using the application fixture's container/spine pattern."""
    container = '''<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="EPUB/package.opf"
    media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
    package = '''<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">urn:uuid:carr-federated-synthetic</dc:identifier>
    <dc:title>Synthetic EPUB hint</dc:title><dc:language>uk</dc:language>
    <meta property="dcterms:modified">2026-11-26T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="second" href="second.xhtml" media-type="application/xhtml+xml"/>
    <item id="first" href="first.xhtml" media-type="application/xhtml+xml"/>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
  </manifest>
  <spine><itemref idref="first"/><itemref idref="second"/></spine>
</package>'''

    def chapter(title, text):
        return ('<?xml version="1.0" encoding="UTF-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>'
                + escape(title) + '</title></head><body><p>'
                + escape(text) + '</p></body></html>')

    nav = '''<html xmlns="http://www.w3.org/1999/xhtml"
        xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head>
      <body><nav epub:type="toc"><ol>
        <li><a href="first.xhtml">Evidence chapter</a></li>
        <li><a href="second.xhtml">Separate chapter</a></li>
      </ol></nav></body></html>'''
    with ZipFile(path, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr('mimetype', 'application/epub+zip', compress_type=ZIP_STORED)
        for name, text in (
            ('META-INF/container.xml', container), ('EPUB/package.opf', package),
            ('EPUB/second.xhtml', chapter('Separate chapter', SECOND_TEXT)),
            ('EPUB/first.xhtml', chapter('Evidence chapter', FIRST_TEXT)),
            ('EPUB/nav.xhtml', nav),
        ):
            archive.writestr(name, text.encode('utf-8'))


def test_federated_epub_preview_keeps_section_and_exact_text_without_quote_authority(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    text_library = app.create_workspace('Selected text library')
    epub_library = app.create_workspace('Selected EPUB library')
    unrelated = app.create_workspace('Unselected library')
    topic = app.create_topic(epub_library, 'Research notes')
    source = tmp_path / 'synthetic.epub'
    _write_epub(source)
    imported = app.import_file(epub_library, source, title='Confirmed EPUB title')
    assert imported['state'] == 'complete' and imported['isolated'] is True
    assert imported['verified'] is False
    app.add_text(text_library, 'Plain source', PHRASE + ' in retained plain text.')
    app.add_text(unrelated, 'Unselected source', PHRASE + ' in an unrelated library.')

    searched = []
    real_search = LibraryApp.search

    def observe_search(self, workspace, *args, **kwargs):
        searched.append(workspace)
        return real_search(self, workspace, *args, **kwargs)

    monkeypatch.setattr(LibraryApp, 'search', observe_search)
    controller = FederatedSearch(app)
    session = controller.search((text_library, epub_library), PHRASE, mode='phrase')
    assert searched == [text_library, epub_library]
    assert session.workspaces == (text_library, epub_library)
    assert [len(group.hits) for group in session.groups] == [1, 1]
    text_hit, epub_hit = session.hits
    assert text_hit.result.verified is True
    assert epub_hit.workspace == epub_library and epub_hit.result.verified is False
    assert epub_hit.result.quote == PHRASE

    preview = controller.preview(epub_hit)
    header, boundary, extracted = preview.partition('\n\n')
    assert boundary and 'Confirmed EPUB title' in header
    assert 'EPUB section 1 of 2: Evidence chapter' in header
    assert 'Segment 1' in header and 'Physical page' not in header
    assert extracted == FIRST_TEXT
    assert SECOND_TEXT not in preview

    for action in (
        lambda: controller.context(epub_hit),
        lambda: controller.copy(epub_hit, 'quote'),
        lambda: controller.copy(epub_hit, 'citation'),
        lambda: controller.save_hit(epub_hit, topic),
    ):
        with pytest.raises(ValueError, match='Unverified extraction'):
            action()
    assert app.evidence(epub_library, topic) == ()
