"""Real DOCX/HTML output from exact evidence; no native Word acceptance claim."""
from dataclasses import replace
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import json
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from docx import Document
import pytest

from carr_library.application import LibraryApp
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.research import academic_documents as documents
from carr_library.research import anchored_export
from carr_library.research.anchored_export import SavedEvidence
from carr_library.research.evidence_export import Evidence


W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'


def source(raw='A railway connected Україна and its neighbours.', **changes):
    row = Evidence('workspace', 'topic', 'evidence', 'edition-id', 'sealed-old',
                   'a' * 64, sha256(raw.encode('utf-8')).hexdigest(), 'physical_page',
                   2, None, 'ix', True, raw, 0, len(raw), raw, 'Historical source',
                   'Independent author', 'First', True, 'My separate research note.', 'II')
    return replace(row, **changes)


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags, self.attributes, self.quotes, self.notes = [], [], [], []
        self.text, self._capture = [], None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        self.attributes.append(attrs)
        if tag == 'blockquote':
            self._capture = (self.quotes, attrs['data-encoding'], [])
        elif tag == 'pre' and attrs.get('class') in ('note', 'note_json'):
            self._capture = (self.notes, attrs['data-encoding'], [])

    def handle_endtag(self, tag):
        if self._capture and (tag == 'blockquote' or
                              tag == 'pre' and self._capture[0] is self.notes):
            destination, encoding, parts = self._capture
            value = ''.join(parts)
            destination.append(json.loads(value) if encoding == 'json-string' else value)
            self._capture = None

    def handle_data(self, value):
        self.text.append(value)
        if self._capture:
            self._capture[2].append(value)


def unpack(result, format):
    if format == 'html':
        parsed = HTMLText()
        parsed.feed(result)
        return parsed.quotes, parsed.notes, '\n'.join(parsed.text)
    doc = Document(BytesIO(result))
    quotes, notes = [], []
    for paragraph in doc.paragraphs:
        name = paragraph.style.name
        if name.startswith('Dossier Quotation') or name.startswith('Dossier Note'):
            value = json.loads(paragraph.text) if name.endswith(' JSON') else paragraph.text
            (quotes if name.startswith('Dossier Quotation') else notes).append(value)
    return quotes, notes, '\n'.join(p.text for p in doc.paragraphs)


@pytest.mark.parametrize('format', ['docx', 'html'])
@pytest.mark.parametrize('raw', [
    '\nLeading LF\nTwo lines\tTabbed.\n',
    'First\r\nSecond\rThird\nFourth\tLast.',
    'Literal \\n and \\r\\n are not source line breaks.',
    'Before\x00NUL\x01control\x1bescape\u202ebidi\u2028separator\ufffeafter',
    'Українська цитата; Cafe\u0301; 中文; العربية; 😀.',
])
def test_source_and_note_character_roundtrip(format, raw):
    row = source(raw, note='Note\r\nSeparate\ttext\u202e.')
    result = documents.export_document('workspace', 'topic', [row], format=format)
    quotes, notes, text = unpack(result, format)
    assert quotes == [raw]
    assert notes == [row.note]
    assert 'Research note' in text and 'Source provenance' in text
    if '\r' in raw or '\x00' in raw:
        assert 'Exact quotation as a JSON string' in text
        assert 'Escapes distinguish carriage returns, line feeds' in text
    if format == 'html' and raw.startswith('\n'):
        assert '<pre><span lang="">\nLeading LF' in result


@pytest.mark.parametrize('format', ['docx', 'html'])
def test_multpage_evidence_keeps_separate_slices_and_one_note(format):
    first = source('First page ends here.', span_count=2)
    second = source('Second page starts here.', physical_index=3, printed_label='x',
                    span_index=2, span_count=2)
    result = documents.export_document('workspace', 'topic', [first, second], format=format)
    quotes, notes, text = unpack(result, format)
    assert quotes == [first.quote, second.quote]
    assert notes == [first.note]
    assert 'Source slice 1 of 2' in text and 'Source slice 2 of 2' in text
    assert 'Physical page 2; printed page ix' in text and 'Physical page 3; printed page x' in text
    assert 'Bibliography entries: 1.' in text
    assert first.raw_text_sha256 in text and second.raw_text_sha256 in text
    assert first.quote + second.quote not in text


@pytest.mark.parametrize('format', ['docx', 'html'])
def test_references_deduplicate_only_identical_source_and_metadata_snapshots(format):
    first = source()
    same = source(evidence_id='second')
    changed = source(evidence_id='third', author='Different saved author')
    revision = source(evidence_id='fourth', revision_id='other-sealed-revision')
    result = documents.export_document('workspace', 'topic', [first, same, changed, revision], format=format)
    _, _, text = unpack(result, format)
    assert 'Bibliography entries: 3.' in text
    assert 'Bibliography reference 1.' in text
    assert 'Bibliography reference 2.' in text and 'Bibliography reference 3.' in text
    assert 'Different saved author.' in text
    assert 'sealed-old' in text and 'other-sealed-revision' in text


@pytest.mark.parametrize('format', ['docx', 'html'])
def test_unknown_metadata_and_unverified_printed_page_are_explicit(format):
    row = source(title='', author='', edition='', volume='',
                 printed_label='Unverified candidate 55', printed_label_verified=False)
    _, _, text = unpack(documents.export_document('workspace', 'topic', [row], format=format), format)
    assert 'Author unknown. Title unknown. Edition unknown; volume unknown.' in text
    assert 'Publication year and publisher are not recorded' in text
    assert 'Physical page 2; printed page unknown' in text
    assert 'Unverified candidate 55' not in text
    logical = replace(row, kind='logical_segment', physical_index=None, segment_index=4,
                      printed_label=None)
    _, _, logical_text = unpack(documents.export_document('workspace', 'topic', [logical], format=format), format)
    assert 'Segment 4 (unpaginated)' in logical_text
    assert 'Physical page' not in logical_text


@pytest.mark.parametrize('format', ['docx', 'html'])
@pytest.mark.parametrize('changes', [
    {'workspace_id': 'foreign'}, {'topic_id': 'other'}, {'verified': False},
    {'quote': 'invented'}, {'raw_text': 'changed'}, {'span_count': 2},
    {'note': None}, {'printed_label': 'ix\nfalse label'},
])
def test_all_validation_completes_before_renderer(format, changes, monkeypatch):
    def forbidden(*args):
        pytest.fail('renderer started before all evidence passed validation')
    monkeypatch.setattr(documents, '_docx' if format == 'docx' else '_html', forbidden)
    bad = source(evidence_id='later', **changes)
    with pytest.raises(ValueError):
        documents.export_document('workspace', 'topic', [source(), bad], format=format)


@pytest.mark.parametrize('format', ['docx', 'html'])
def test_late_historical_rejection_never_reaches_document_renderer(format, monkeypatch):
    identity = Identity('workspace', 'edition-id', 'a' * 64, 'sealed-old')
    first = make_anchor(identity, Locator('physical_page', 1), 'First source.', 0, 13,
                        Metadata('Author', 'Title'), text_verified=True)
    second = make_anchor(identity, Locator('physical_page', 2), 'Second source.', 0, 14,
                         Metadata('Author', 'Title'), text_verified=True)
    rows = [SavedEvidence('topic', 'first', first), SavedEvidence('topic', 'second', second)]
    def load(i, locator):
        if locator.index == 2:
            raise ValueError('review revoked')
        return i, locator, 'First source.', True
    def forbidden(*args, **kwargs):
        pytest.fail('unauthenticated historical evidence reached renderer')
    monkeypatch.setattr(anchored_export, 'export_topic', forbidden)
    with pytest.raises(ValueError, match='review revoked'):
        anchored_export.export_saved('workspace', 'topic', rows, load, format=format)


@pytest.mark.parametrize('format', ['docx', 'html'])
def test_actual_application_historical_authentication_handoff(tmp_path, monkeypatch, format):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Academic test')
    topic = app.create_topic(workspace, 'Railway research')
    app.add_text(workspace, 'Historical title', 'Exact source quotation. More context.',
                 authors=('Historical author',))
    hit, = app.search(workspace, 'Exact source quotation')
    app.save_hit(workspace, topic, hit.ref, hit.revision, 'Independent commentary.')
    app.update_metadata(workspace, hit.edition_id, title='Current catalog title',
                        authors=('Current catalog author',))
    # Dispatcher integration belongs to the application worker. This is the
    # same existing authenticated handoff, with only its final renderer replaced.
    monkeypatch.setattr(anchored_export, 'export_topic', documents.export_document)
    quotes, notes, text = unpack(app.export_topic(workspace, topic, format=format), format)
    assert quotes == [hit.quote] and notes == ['Independent commentary.']
    assert 'Historical title' in text and 'Historical author' in text
    assert 'Current catalog title' not in text and 'Current catalog author' not in text
    assert hit.revision in text
    other = app.create_workspace('Unrelated workspace')
    with pytest.raises(ValueError):
        app.export_topic(other, topic, format=format)


def test_word_has_native_headings_plain_content_and_no_external_relationships():
    hostile = '</pre><script>alert(1)</script><a href="https://invalid.example">link</a>'
    row = source(hostile, title=hostile, note='=HYPERLINK("file:///private","note")')
    result = documents.export_document('workspace', 'topic', [row], title='Academic study')
    doc = Document(BytesIO(result))
    assert doc.paragraphs[0].style.name == 'Title'
    assert doc.paragraphs[0].text == 'Academic study'
    assert [p.text for p in doc.paragraphs if p.style.name == 'Heading 1'] == [
        'Evidence', 'Bibliography', 'Source provenance']
    assert any(p.style.name == 'Heading 2' and p.text == 'Evidence 1' for p in doc.paragraphs)
    assert doc.core_properties.title == 'Academic study'
    assert doc.core_properties.language == 'en-US'
    assert doc.core_properties.author == '' and doc.core_properties.last_modified_by == ''
    assert doc.core_properties.created is None and doc.core_properties.modified is None
    with ZipFile(BytesIO(result)) as archive:
        assert not any('vbaproject' in name.lower() or '/embeddings/' in name for name in archive.namelist())
        for name in archive.namelist():
            if name.endswith('.rels'):
                for relation in ET.fromstring(archive.read(name)).findall(REL + 'Relationship'):
                    assert relation.get('TargetMode') != 'External'
                    assert not relation.get('Target', '').startswith(('http:', 'https:', 'file:'))
        root = ET.fromstring(archive.read('word/document.xml'))
        for tag in ('hyperlink', 'fldSimple', 'fldChar', 'instrText', 'object', 'altChunk'):
            assert root.find('.//' + W + tag) is None
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
    assert unpack(result, 'docx')[0] == [hostile]
    assert documents.export_document('workspace', 'topic', [row], title='Academic study') == result


def test_html_is_semantic_self_contained_and_escapes_source_markup():
    hostile = '<script>alert(1)</script><img src="https://invalid.example/x" onerror="x()">'
    result = documents.export_document('workspace', 'topic', [source(hostile, title=hostile)],
                                      format='html', title=hostile)
    parsed = HTMLText()
    parsed.feed(result)
    assert parsed.quotes == [hostile]
    assert {'main', 'section', 'h1', 'h2', 'h3', 'blockquote'} <= set(parsed.tags)
    assert not {'script', 'iframe', 'object', 'embed', 'form', 'img', 'a', 'link'} & set(parsed.tags)
    assert not any(key in ('src', 'href', 'srcdoc') or key.startswith('on')
                   for attrs in parsed.attributes for key in attrs)
    assert 'Content-Security-Policy' in result and "default-src 'none'" in result
    assert '&lt;script&gt;' in result and '<script>' not in result


@pytest.mark.parametrize('format', ['docx', 'html'])
def test_empty_topic_and_control_metadata_have_no_invented_source(format):
    result = documents.export_document('workspace', 'topic', [], format=format)
    quotes, notes, text = unpack(result, format)
    assert not quotes and not notes
    assert 'No saved evidence in this topic.' in text
    assert 'No source references to list.' in text
    assert 'Author unknown' not in text
    title = 'Safe\x00title\u202e.'
    result = documents.export_document('workspace', 'topic', [source(title=title)],
                                       title=title, format=format)
    _, _, text = unpack(result, format)
    assert '\x00' not in text and '\u202e' not in text
    assert documents._encoded(title) in text and '(JSON string)' in text


def test_full_visible_title_survives_word_core_property_length_limit():
    title = 'A' * 400
    doc = Document(BytesIO(documents.export_document('workspace', 'topic', [], title=title)))
    assert doc.paragraphs[0].text == title
    assert len(doc.core_properties.title) == 255 and doc.core_properties.title.endswith('...')
