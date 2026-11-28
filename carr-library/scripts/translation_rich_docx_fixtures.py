"""Original generated DOCX fixtures; no private document or Office automation."""
from copy import deepcopy
from io import BytesIO
import struct
import xml.etree.ElementTree as ET
from zipfile import ZipFile
import zlib

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
R = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}'
PACKAGE_REL = '{http://schemas.openxmlformats.org/package/2006/relationships}'
TYPES = '{http://schemas.openxmlformats.org/package/2006/content-types}'


def png_bytes():
    def chunk(kind, raw):
        return struct.pack('>I', len(raw)) + kind + raw + struct.pack('>I', zlib.crc32(kind + raw))
    return (b'\x89PNG\r\n\x1a\n'
        + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 1, 8, 6, 0, 0, 0))
        + chunk(b'IDAT', zlib.compress(b'\x00\x22\x44\x66\xff\x66\x44\x22\xff'))
        + chunk(b'IEND', b''))


def replace_members(data, changes, *, removed=()):
    output = BytesIO()
    remaining = dict(changes)
    with ZipFile(BytesIO(data)) as before, ZipFile(output, 'w') as after:
        after.comment = before.comment
        for item in before.infolist():
            if item.filename not in removed:
                after.writestr(item, remaining.pop(item.filename, before.read(item)))
        for name, raw in remaining.items():
            after.writestr(name, raw)
    return output.getvalue()


def rich_docx(*, picture=True, notes=True, bookmark=True, proof=False):
    from docx import Document
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches
    from lxml import etree

    def marker(paragraph, name, identity=None):
        node = OxmlElement('w:' + name)
        if identity is not None:
            node.set(qn('w:id'), str(identity))
        paragraph.add_run()._r.append(node)
        return node

    document = Document()
    paragraph = document.add_paragraph(style='Heading 1')
    paragraph.add_run('Before footnote Ω ').bold = True
    if notes:
        marker(paragraph, 'footnoteReference', 5)
    paragraph.add_run(' after footnote.').italic = True
    paragraph = document.add_paragraph('Before picture ')
    if picture:
        paragraph.add_run().add_picture(BytesIO(png_bytes()), width=Inches(.25))
    paragraph.add_run(' after picture.')
    paragraph = document.add_paragraph('Outside bookmark ')
    if bookmark:
        start = OxmlElement('w:bookmarkStart')
        start.set(qn('w:id'), '17')
        start.set(qn('w:name'), 'RetainedFixedBookmark')
        paragraph._p.append(start)
    paragraph.add_run('inside bookmarked text').bold = True
    if bookmark:
        end = OxmlElement('w:bookmarkEnd')
        end.set(qn('w:id'), '17')
        paragraph._p.append(end)
    if proof:
        start = OxmlElement('w:proofErr')
        start.set(qn('w:type'), 'spellStart')
        paragraph._p.append(start)
    paragraph.add_run(' after bookmark.')
    if proof:
        end = OxmlElement('w:proofErr')
        end.set(qn('w:type'), 'spellEnd')
        paragraph._p.append(end)
    cell = document.add_table(rows=1, cols=1).cell(0, 0)
    cell.paragraphs[0].add_run('Endnote cell ')
    if notes:
        marker(cell.paragraphs[0], 'endnoteReference', 9)
    cell.paragraphs[0].add_run(' after endnote.')
    document.sections[0].header.paragraphs[0].text = 'Untranslated retained header.'
    stream = BytesIO()
    document.save(stream)
    raw = stream.getvalue()
    if not notes:
        return raw
    with ZipFile(BytesIO(raw)) as archive:
        types = ET.fromstring(archive.read('[Content_Types].xml'))
        relations = ET.fromstring(archive.read('word/_rels/document.xml.rels'))
    changes = {}
    for kind, identity, prose in (
            ('footnote', 5, 'Independent footnote prose Україна.'),
            ('endnote', 9, 'Independent endnote prose Česko.')):
        root = OxmlElement('w:' + kind + 's')
        for number, category in ((-1, 'separator'), (0, 'continuationSeparator')):
            note = OxmlElement('w:' + kind)
            note.set(qn('w:id'), str(number))
            note.set(qn('w:type'), category)
            p, r = OxmlElement('w:p'), OxmlElement('w:r')
            r.append(OxmlElement('w:' + category))
            p.append(r)
            note.append(p)
            root.append(note)
        note = OxmlElement('w:' + kind)
        note.set(qn('w:id'), str(identity))
        paragraph = document.add_paragraph()
        marker(paragraph, kind + 'Ref')
        paragraph.add_run(prose).italic = True
        note.append(deepcopy(paragraph._p))
        if kind == 'footnote':
            table = document.add_table(rows=1, cols=1)
            table.cell(0, 0).text = 'Independent footnote table.'
            note.append(deepcopy(table._tbl))
        root.append(note)
        path = 'word/' + kind + 's.xml'
        changes[path] = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
        ET.SubElement(types, TYPES + 'Override', {
            'PartName': '/' + path,
            'ContentType': 'application/vnd.openxmlformats-officedocument.wordprocessingml.' + kind + 's+xml'})
        ET.SubElement(relations, PACKAGE_REL + 'Relationship', {
            'Id': 'rIdCARR' + kind, 'Type': R[1:-1] + '/' + kind + 's',
            'Target': kind + 's.xml'})
    changes['[Content_Types].xml'] = ET.tostring(types, encoding='utf-8', xml_declaration=True)
    changes['word/_rels/document.xml.rels'] = ET.tostring(relations, encoding='utf-8', xml_declaration=True)
    return replace_members(raw, changes)
