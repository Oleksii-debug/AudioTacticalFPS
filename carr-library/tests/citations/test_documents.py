"""Actual CSL/OOXML interoperability without asserting a Word/NVDA listening test."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from docx import Document
from lxml import etree

from carr_library.citations.csl import load_custom_style
from carr_library.citations.documents import (
    CitationParagraph, SCHEMA, citation_document, citation_ids, read_citation_docx,
)

RECORDS = [
    {'id': 'source-one', 'type': 'book', 'title': 'Synthetic source',
     'author': [{'family': 'Smith', 'given': 'Jane'}], 'issued': {'date-parts': [[2020]]}},
    {'id': 'source-two', 'type': 'book', 'title': 'Second synthetic source',
     'author': [{'literal': 'Research Office'}], 'issued': {'date-parts': [[2021]]}},
]
PART = 'customXml/carr-citations.xml'
NS = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}


def save(tmp_path, content, name='citations.docx'):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_editable_in_text_runs_preserve_exact_metadata_without_source_claims(tmp_path):
    records = deepcopy(RECORDS)
    content = citation_document(records, locale='uk-UA')
    assert records == RECORDS
    document = Document(BytesIO(content))
    assert any('Smith' in paragraph.text for paragraph in document.paragraphs)
    assert any(run.italic for paragraph in document.paragraphs for run in paragraph.runs)
    snapshot = read_citation_docx(save(tmp_path, content))
    assert snapshot['schema'] == SCHEMA
    assert [row['id'] for row in snapshot['records']] == ['source-one', 'source-two']
    assert snapshot['quotation_verified'] is snapshot['visible_edits_verified'] is False
    assert snapshot['locale'] == 'uk-UA'
    with ZipFile(BytesIO(content)) as archive:
        assert PART in archive.namelist()
        for name in archive.namelist():
            if name.endswith('.rels'):
                assert b'TargetMode="External"' not in archive.read(name)


def test_note_style_has_real_native_endnotes_and_matching_references(tmp_path):
    content = citation_document(RECORDS, style='chicago-note')
    with ZipFile(BytesIO(content)) as archive:
        body = etree.fromstring(archive.read('word/document.xml'))
        notes = etree.fromstring(archive.read('word/endnotes.xml'))
        assert [node.get('{%s}id' % NS['w']) for node in body.findall('.//w:endnoteReference', NS)] == ['1', '2']
        assert [node.get('{%s}id' % NS['w']) for node in notes.findall('w:endnote', NS)] == ['-1', '0', '1', '2']
        assert len(notes.findall('.//w:endnoteRef', NS)) == 2
        assert b'endnotes' in archive.read('word/_rels/document.xml.rels')
        # Chicago applies title casing; this guard checks the actual note text.
        assert 'synthetic source' in ''.join(notes.itertext()).casefold()
    assert read_citation_docx(save(tmp_path, content))['style']['kind'] == 'note'


def test_paragraphs_keep_cluster_order_and_never_guess_a_locator(tmp_path):
    clusters = [[{'id': 'source-one', 'locator': '23', 'label': 'page'}], [{'id': 'source-two'}]]
    ids = citation_ids(clusters)
    paragraphs = [CitationParagraph('First statement', (ids[0],)),
                  CitationParagraph('Second statement', (ids[1],))]
    content = citation_document(RECORDS, citations=clusters, paragraphs=paragraphs)
    snapshot = read_citation_docx(save(tmp_path, content))
    assert [cluster['items'] for cluster in snapshot['clusters']] == clusters
    assert [cluster['id'] for cluster in snapshot['clusters']] == list(ids)
    assert snapshot['quotation_verified'] is False
    assert any('manually' in warning for warning in snapshot['warnings'])
    with pytest.raises(ValueError, match='order'):
        citation_document(RECORDS, citations=clusters, paragraphs=list(reversed(paragraphs)))
    with pytest.raises(ValueError, match='identity'):
        citation_document(RECORDS, paragraphs=[CitationParagraph('Unknown', ('wrong',))])


def rewrite(content, mutator, *, duplicate=False):
    output = BytesIO()
    with ZipFile(BytesIO(content)) as source, ZipFile(output, 'w') as target:
        for item in source.infolist():
            raw = source.read(item)
            target.writestr(item, mutator(raw) if item.filename == PART else raw)
        if duplicate:
            target.writestr(PART, source.read(PART))
    return output.getvalue()


def test_provenance_corruption_and_duplicate_parts_are_rejected(tmp_path):
    content = citation_document(RECORDS[:1])
    corrupted = rewrite(content, lambda raw: raw.replace(b'Synthetic source', b'Changed source'))
    with pytest.raises(ValueError, match='integrity'):
        read_citation_docx(save(tmp_path, corrupted, 'changed.docx'))
    with pytest.warns(UserWarning, match='Duplicate'):
        duplicated = rewrite(content, lambda raw: raw, duplicate=True)
    with pytest.raises(ValueError, match='ambiguous'):
        read_citation_docx(save(tmp_path, duplicated, 'duplicate.docx'))


def test_embedded_snapshot_cannot_claim_quotation_or_visible_edit_verification(tmp_path):
    from hashlib import sha256
    def forge(raw):
        node = etree.fromstring(raw)
        value = json.loads(node.text)
        value['quotation_verified'] = True
        node.text = json.dumps(value, ensure_ascii=False)
        node.set('sha256', sha256(node.text.encode('utf-8')).hexdigest())
        return etree.tostring(node)
    content = rewrite(citation_document(RECORDS[:1]), forge)
    with pytest.raises(ValueError, match='contract'):
        read_citation_docx(save(tmp_path, content))


def test_reading_snapshot_never_loads_a_dtd_or_external_resource(tmp_path):
    content = rewrite(citation_document(RECORDS[:1]), lambda raw:
        b'<!DOCTYPE citations [<!ENTITY remote SYSTEM "file:///synthetic-secret">]>' +
        b'<citations>&remote;</citations>')
    with pytest.raises(Exception) as error:
        read_citation_docx(save(tmp_path, content))
    assert 'DTD' in type(error.value).__name__ or 'Entities' in type(error.value).__name__


def test_custom_style_snapshot_retains_the_exact_existing_validated_style(tmp_path):
    source = b'''<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0" class="in-text">
    <info><title>Synthetic local style</title><id>urn:synthetic:local</id></info>
    <citation><layout prefix="(" suffix=")"><text variable="title"/></layout></citation>
    <bibliography><layout><text variable="title" font-style="italic"/></layout></bibliography>
    </style>'''
    style = load_custom_style(source)
    content = citation_document(RECORDS[:1], style=style)
    snapshot = read_citation_docx(save(tmp_path, content))
    assert snapshot['style']['sha256'] == style.sha256
    assert snapshot['style']['custom_source'].encode('utf-8') == source


def test_cancellation_precedes_formatting():
    with pytest.raises(TimeoutError, match='cancelled'):
        citation_document(RECORDS, cancelled=lambda: True)


def test_xml_expansion_cannot_create_a_snapshot_the_reader_rejects(tmp_path, monkeypatch):
    from carr_library.citations import documents
    body = [CitationParagraph('&' * 1000, ('citation-1',))]
    original = citation_document(RECORDS[:1], paragraphs=body)
    with ZipFile(BytesIO(original)) as archive:
        serialized = archive.read(PART)
    raw_json = etree.fromstring(serialized).text.encode('utf-8')
    limit = len(raw_json) + 1
    assert len(serialized) > limit
    monkeypatch.setattr(documents, '_MAX_METADATA', limit)
    with pytest.raises(ValueError, match='Serialized citation provenance'):
        citation_document(RECORDS[:1], paragraphs=body)
