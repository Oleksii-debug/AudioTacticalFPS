"""Actual generated OOXML packages; no Office process or model is used."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import importlib.util
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree
import pytest

from carr_library.extract.docx_ordered import extract_docx
from carr_library.extract.docx_rich import SOURCE_PROFILE, inspect_document
from carr_library.assistant import translation_docx_rich as rich
from carr_library.assistant import translation_layout_worker as worker

_fixture_path = Path(__file__).resolve().parents[2] / 'scripts' / 'translation_rich_docx_fixtures.py'
_spec = importlib.util.spec_from_file_location('docx_rich_original_fixture', _fixture_path)
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)
W, R = fixture.W, fixture.R


def change_xml(raw, member, change):
    with ZipFile(BytesIO(raw)) as archive:
        node = etree.fromstring(archive.read(member))
    change(node)
    return fixture.replace_members(raw, {member: etree.tostring(node, encoding='utf-8', xml_declaration=True)})


def test_plain_docx_keeps_exact_original_record_warning_and_provenance_wire():
    document = Document()
    document.add_paragraph('  Plain Ω\ttext\nnext ')
    document.add_table(rows=1, cols=1).cell(0, 0).text = 'Ordinary table.'
    stream = BytesIO()
    document.save(stream)
    raw = stream.getvalue()
    assert extract_docx(raw) == {
        'status': 'review_required',
        'records': [dict(logical_locator=locator, raw_text=value,
            text_sha256=sha256(value.encode('utf-8')).hexdigest(),
            physical_index=None, printed_label=None, printed_label_verified=False)
            for locator, value in (([1], '  Plain Ω\ttext\nnext '), ([2, 1, 1, 1], 'Ordinary table.'))],
        'warnings': ['body_only', 'logical_locator_not_page', 'layout_not_verified'],
        'provenance': {'asset_sha256': sha256(raw).hexdigest(), 'engine': 'python-docx', 'version': '1.2.0'}}
    assert worker.describe(raw)['schema'] == 'carr-docx-body-layout/1'


def test_actual_notes_have_independent_records_exact_reference_mapping_and_no_page_authority():
    raw = fixture.rich_docx()
    parsed = extract_docx(raw)
    assert parsed['status'] == 'review_required'
    assert parsed['provenance']['source_profile'] == SOURCE_PROFILE
    assert [row['logical_locator'] for row in parsed['records'][:4]] == [[1], [2], [3], [4, 1, 1, 1]]
    assert [row['logical_locator'] for row in parsed['records'][4:]] == [
        {'part': 'word/footnotes.xml', 'kind': 'footnote', 'note_id': 5, 'block': [1]},
        {'part': 'word/footnotes.xml', 'kind': 'footnote', 'note_id': 5, 'block': [2, 1, 1, 1]},
        {'part': 'word/endnotes.xml', 'kind': 'endnote', 'note_id': 9, 'block': [1]}]
    assert [row['raw_text'] for row in parsed['records'][4:]] == [
        'Independent footnote prose Україна.', 'Independent footnote table.',
        'Independent endnote prose Česko.']
    assert parsed['rich_structure']['notes'] == [
        {'kind': 'footnote', 'note_id': 5, 'source_record': 1, 'source_offset': len('Before footnote Ω ')},
        {'kind': 'endnote', 'note_id': 9, 'source_record': 4, 'source_offset': len('Endnote cell ')}]
    assert all(row['physical_index'] is None and row['printed_label'] is None
               and row['printed_label_verified'] is False for row in parsed['records'])
    assert '5' not in parsed['records'][0]['raw_text'] and '9' not in parsed['records'][3]['raw_text']


def test_actual_inline_picture_relationship_header_geometry_and_bytes_are_bound():
    raw = fixture.rich_docx()
    parsed = extract_docx(raw)
    image, = parsed['rich_structure']['media']
    assert image == {'part': 'word/media/image1.png', 'sha256': sha256(fixture.png_bytes()).hexdigest(),
                     'size': len(fixture.png_bytes()), 'media_type': 'image/png', 'width': 2, 'height': 1}
    marker, = [item for item in parsed['rich_structure']['markers'] if item['kind'] == 'image']
    assert marker['record'] == 2 and marker['offset'] == len('Before picture ')
    assert len(marker['xml_sha256']) == 64
    with ZipFile(BytesIO(raw)) as archive:
        assert archive.read(image['part']) == fixture.png_bytes()


@pytest.mark.parametrize('fault', ['missing-note', 'duplicate-note', 'orphan-note', 'duplicate-reference',
    'missing-reference-id', 'negative-reference', 'custom-label', 'missing-label', 'wrong-label',
    'nested-reference', 'unreferenced-prose-in-separator', 'external-note-part'])
def test_invalid_note_graph_is_refused_without_shortened_accepted_text(fault):
    raw = fixture.rich_docx()
    if fault in ('duplicate-reference', 'missing-reference-id', 'negative-reference', 'custom-label'):
        def change(root):
            ref = root.find('.//' + W + 'footnoteReference')
            if fault == 'duplicate-reference':
                ref.addnext(deepcopy(ref))
            elif fault == 'missing-reference-id':
                ref.attrib.pop(W + 'id')
            elif fault == 'negative-reference':
                ref.set(W + 'id', '-1')
            else:
                ref.set(W + 'customMarkFollows', '1')
        raw = change_xml(raw, 'word/document.xml', change)
    elif fault == 'external-note-part':
        def change(root):
            for row in root:
                if row.get('Type', '').endswith('/footnotes'):
                    row.set('TargetMode', 'External')
                    row.set('Target', 'https://example.invalid/never-read-notes')
        raw = change_xml(raw, 'word/_rels/document.xml.rels', change)
    else:
        def change(root):
            normal = next(node for node in root if node.get(W + 'id') == '5')
            if fault == 'missing-note':
                root.remove(normal)
            elif fault == 'duplicate-note':
                root.append(deepcopy(normal))
            elif fault == 'orphan-note':
                other = deepcopy(normal)
                other.set(W + 'id', '6')
                root.append(other)
            elif fault == 'missing-label':
                label = normal.find('.//' + W + 'footnoteRef')
                label.getparent().remove(label)
            elif fault == 'wrong-label':
                normal.find('.//' + W + 'footnoteRef').tag = W + 'endnoteRef'
            elif fault == 'nested-reference':
                normal.find('.//' + W + 'footnoteRef').tag = W + 'footnoteReference'
                normal.find('.//' + W + 'footnoteReference').set(W + 'id', '5')
            else:
                note = root[0]
                r, t = OxmlElement('w:r'), OxmlElement('w:t')
                t.text = 'This ordinary text cannot be discarded as a separator.'
                r.append(t)
                note[0].append(r)
        raw = change_xml(raw, 'word/footnotes.xml', change)
    with pytest.raises(ValueError, match='unsupported body structure'):
        extract_docx(raw)


@pytest.mark.parametrize('fault', ['missing-image', 'linked-image', 'external-image', 'wrong-content-type',
                                  'corrupt-image', 'floating-image', 'chart', 'image-textbox', 'zero-extent'])
def test_unreviewed_picture_authority_or_content_is_refused(fault):
    raw = fixture.rich_docx()
    A = '{http://schemas.openxmlformats.org/drawingml/2006/main}'
    WP = '{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}'
    if fault == 'corrupt-image':
        raw = fixture.replace_members(raw, {'word/media/image1.png': b'broken picture'})
    elif fault == 'wrong-content-type':
        def change(root):
            for row in root:
                if row.get('Extension') == 'png':
                    row.set('ContentType', 'image/svg+xml')
        raw = change_xml(raw, '[Content_Types].xml', change)
    elif fault == 'external-image':
        def change(root):
            for row in root:
                if row.get('Type', '').endswith('/image'):
                    row.set('TargetMode', 'External')
                    row.set('Target', 'https://example.invalid/never-fetch.png')
        raw = change_xml(raw, 'word/_rels/document.xml.rels', change)
    else:
        def change(root):
            blip = root.find('.//' + A + 'blip')
            if fault == 'missing-image':
                blip.set(R + 'embed', 'rIdUnknown')
            elif fault == 'linked-image':
                blip.set(R + 'link', blip.attrib.pop(R + 'embed'))
            elif fault == 'floating-image':
                root.find('.//' + WP + 'inline').tag = WP + 'anchor'
            elif fault == 'chart':
                root.find('.//' + A + 'graphicData').set('uri', 'http://schemas.openxmlformats.org/drawingml/2006/chart')
            elif fault == 'image-textbox':
                box = OxmlElement('w:txbxContent')
                box.append(OxmlElement('w:p'))
                root.find('.//' + W + 'drawing').append(box)
            else:
                root.find('.//' + WP + 'extent').set('cx', '0')
        raw = change_xml(raw, 'word/document.xml', change)
    with pytest.raises(ValueError, match='unsupported body structure'):
        extract_docx(raw)


@pytest.mark.parametrize('part', ['word/document.xml', 'word/footnotes.xml', 'word/endnotes.xml'])
@pytest.mark.parametrize('tag', ['w:vanish', 'w:fldChar', 'w:smartTag', 'm:oMath'])
def test_prior_hidden_active_and_omitted_text_guards_cover_new_note_parts(part, tag):
    raw = fixture.rich_docx()
    def change(root):
        target = root.find('.//' + W + 'r')
        target.append(OxmlElement(tag))
    raw = change_xml(raw, part, change)
    with pytest.raises(ValueError, match='unsupported body structure'):
        extract_docx(raw)



@pytest.mark.parametrize('part', ['word/document.xml', 'word/footnotes.xml'])
@pytest.mark.parametrize('container', ['pPr', 'rPr'])
@pytest.mark.parametrize('payload', ['run-text', 'nested-paragraph', 'note-marker'])
def test_rich_source_refuses_content_hidden_below_preserved_properties(part, container, payload):
    raw = fixture.rich_docx()
    def change(root):
        paragraph = (root.find('.//' + W + 'p') if part == 'word/document.xml' else
            next(node for node in root if node.get(W + 'id') == '5').find(W + 'p'))
        if container == 'pPr':
            properties = OxmlElement('w:pPr')
            paragraph.insert(0, properties)
        else:
            run = paragraph.find(W + 'r')
            properties = OxmlElement('w:rPr')
            run.insert(0, properties)
        if payload == 'note-marker':
            node = OxmlElement('w:footnoteReference')
            node.set(W + 'id', '5')
        else:
            node = OxmlElement('w:r')
            text = OxmlElement('w:t')
            text.text = 'NONBLANK SOURCE MUST NOT DISAPPEAR Ω'
            node.append(text)
            if payload == 'nested-paragraph':
                nested = OxmlElement('w:p')
                nested.append(node)
                node = nested
        properties.append(node)
    raw = change_xml(raw, part, change)
    # Both fixtures still carry actual body images and note references, so this
    # exercises the new rich profile rather than an unchanged legacy path.
    with pytest.raises(ValueError, match='unsupported body structure'):
        extract_docx(raw)


def test_rich_source_preserves_ordinary_tab_stop_properties_without_hidden_text():
    raw = fixture.rich_docx()
    def change(root):
        paragraph = root.find('.//' + W + 'p')
        properties = paragraph.find(W + 'pPr')
        if properties is None:
            properties = OxmlElement('w:pPr')
            paragraph.insert(0, properties)
        tabs = OxmlElement('w:tabs')
        stop = OxmlElement('w:tab')
        stop.set(W + 'val', 'left')
        stop.set(W + 'pos', '720')
        tabs.append(stop)
        properties.append(tabs)
    raw = change_xml(raw, 'word/document.xml', change)
    assert extract_docx(raw)['provenance']['source_profile'] == SOURCE_PROFILE


def test_actual_fixed_bookmark_and_proof_boundaries_partition_and_replay_positions():
    raw = fixture.rich_docx(proof=True)
    mapping = rich.describe(raw)
    before = mapping['records'][2]
    assert [item['tag'] for item in before['markers']] == [
        'bookmarkStart', 'bookmarkEnd', 'proofErr', 'proofErr']
    assert [before['text'][u['start']:u['end']] for u in before['units']] == [
        'Outside bookmark ', 'inside bookmarked text', ' after bookmark.']
    replacements = [{'ordinal': u['ordinal'], 'source_sha256': u['sha256'],
                     'text': 'DERIVED_' + str(u['ordinal']) + ' Ω'}
                    for row in mapping['records'] for u in row['units']]
    output, receipt = rich.render(raw, replacements)
    after = rich.describe(output)['records'][2]
    values = {r['ordinal']: r['text'] for r in replacements}
    assert after['text'] == ''.join(values[u['ordinal']] for u in before['units'])
    assert [m['xml_sha256'] for m in after['markers']] == [m['xml_sha256'] for m in before['markers']]
    expected_offsets = [len(values[before['units'][0]['ordinal']]),
        len(values[before['units'][0]['ordinal']]) + len(values[before['units'][1]['ordinal']])]
    assert [m['offset'] for m in after['markers'][:2]] == expected_offsets
    assert receipt['quotation_authority'] is False
    with ZipFile(BytesIO(raw)) as original, ZipFile(BytesIO(output)) as final:
        assert original.namelist() == final.namelist()
        assert all(original.read(name) == final.read(name) for name in original.namelist()
                   if name not in receipt['changed_members'])


def test_no_rich_feature_keeps_unselected_separator_only_part_in_legacy_body_profile():
    raw = fixture.rich_docx(picture=False, notes=True, bookmark=False)
    def strip_references(root):
        for name in ('footnoteReference', 'endnoteReference'):
            for node in root.findall('.//' + W + name):
                node.getparent().remove(node)
    raw = change_xml(raw, 'word/document.xml', strip_references)
    for kind, identity in (('footnote', '5'), ('endnote', '9')):
        raw = change_xml(raw, 'word/' + kind + 's.xml',
            lambda root, identity=identity: root.remove(next(node for node in root if node.get(W + 'id') == identity)))
    assert extract_docx(raw)['provenance'] == {
        'asset_sha256': sha256(raw).hexdigest(), 'engine': 'python-docx', 'version': '1.2.0'}
    assert worker.describe(raw)['schema'] == 'carr-docx-body-layout/1'


def test_note_table_cannot_silently_admit_merged_or_nested_content():
    raw = fixture.rich_docx()
    def change(root):
        properties = root.find('.//' + W + 'tcPr')
        span = OxmlElement('w:gridSpan')
        span.set(qn('w:val'), '2')
        properties.append(span)
    raw = change_xml(raw, 'word/footnotes.xml', change)
    with pytest.raises(ValueError, match='complex table'):
        extract_docx(raw)


def test_rich_canonical_import_never_opens_a_network_or_word_process(monkeypatch):
    import socket
    import subprocess
    def forbidden(*args, **kwargs):
        raise AssertionError('A canonical package parser must not start a process or socket.')
    monkeypatch.setattr(socket, 'socket', forbidden)
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    assert len(extract_docx(fixture.rich_docx())['records']) == 7


@pytest.mark.parametrize('kind', ['footnote', 'endnote'])
def test_note_separator_keeps_ordinary_paragraph_and_run_properties_without_prose(kind):
    raw = fixture.rich_docx()
    def change(root):
        separator = next(node for node in root if node.get(W + 'type') == 'separator')
        p = separator.find(W + 'p')
        ppr = OxmlElement('w:pPr')
        spacing = OxmlElement('w:spacing')
        for key, value in (('after', '0'), ('line', '240'), ('lineRule', 'auto')):
            spacing.set(W + key, value)
        ppr.append(spacing)
        p.insert(0, ppr)
        rpr = OxmlElement('w:rPr')
        size = OxmlElement('w:sz')
        size.set(W + 'val', '20')
        rpr.append(size)
        p.find(W + 'r').insert(0, rpr)
    part = 'word/' + kind + 's.xml'
    raw = change_xml(raw, part, change)
    mapping = rich.describe(raw)
    replacements = [{'ordinal': unit['ordinal'], 'source_sha256': unit['sha256'],
                     'text': 'Derived ' + row['text'][unit['start']:unit['end']]}
                    for row in mapping['records'] for unit in row['units']]
    output, _ = rich.render(raw, replacements)
    with ZipFile(BytesIO(raw)) as source, ZipFile(BytesIO(output)) as final:
        before, after = (etree.fromstring(archive.read(part)) for archive in (source, final))
        a, b = (next(node for node in root if node.get(W + 'type') == 'separator')
                for root in (before, after))
        assert etree.tostring(a, method='c14n') == etree.tostring(b, method='c14n')
    assert len(extract_docx(output)['records']) == 7
