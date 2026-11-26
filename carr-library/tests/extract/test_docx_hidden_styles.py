"""Hidden OOXML text is not an ordinary searchable body preview."""
from io import BytesIO

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml import OxmlElement
import pytest

from carr_library.extract.docx_ordered import extract_docx


def snapshot(document):
    target = BytesIO()
    document.save(target)
    return target.getvalue()


@pytest.mark.parametrize('kind', ['character', 'paragraph', 'inherited', 'default'])
def test_hidden_style_rejected(kind):
    document = Document()
    document.add_paragraph('Visible ordinary text.')
    if kind == 'default':
        document.styles['Normal'].font.hidden = True
        document.add_paragraph('Hidden synthetic words')
    elif kind == 'paragraph':
        style = document.styles.add_style('HiddenParagraph', WD_STYLE_TYPE.PARAGRAPH)
        style.font.hidden = True
        document.add_paragraph('Hidden synthetic words', style=style)
    else:
        style = document.styles.add_style('HiddenCharacter', WD_STYLE_TYPE.CHARACTER)
        style.font.hidden = True
        if kind == 'inherited':
            child = document.styles.add_style('InheritedCharacter', WD_STYLE_TYPE.CHARACTER)
            child.base_style = style
            style = child
        document.add_paragraph('Visible prefix ').add_run('Hidden synthetic words', style=style)
    with pytest.raises(ValueError, match='hidden text style'):
        extract_docx(snapshot(document))


@pytest.mark.parametrize('tag', ['w:vanish', 'w:webHidden', 'w:specVanish'])
def test_direct_hidden_properties_rejected(tag):
    document = Document()
    run = document.add_paragraph('Visible prefix ').add_run('Hidden synthetic words')
    run._r.get_or_add_rPr().append(OxmlElement(tag))
    with pytest.raises(ValueError, match='unsupported body structure'):
        extract_docx(snapshot(document))


def test_plain_unicode_with_nonhidden_style_keeps_preview():
    document = Document()
    style = document.styles.add_style('OrdinaryCharacter', WD_STYLE_TYPE.CHARACTER)
    style.font.bold = True
    paragraph = document.add_paragraph('Історія: ')
    paragraph.add_run('слово / histoire', style=style)
    result = extract_docx(snapshot(document))
    assert result['records'][0]['raw_text'] == 'Історія: слово / histoire'
    assert result['status'] == 'review_required'
    assert result['records'][0]['physical_index'] is None


from zipfile import ZipFile
from lxml import etree
from docx.oxml.ns import qn

REL = 'http://schemas.microsoft.com/office/2007/relationships/stylesWithEffects'

def effects_fixture(tag=None, external=False):
    d = Document()
    d.add_paragraph('Visible prefix ')
    d.add_paragraph('Synthetic body text.')
    output = BytesIO()
    with ZipFile(BytesIO(snapshot(d))) as zin, ZipFile(output, 'w') as zout:
        for item in zin.infolist():
            data = zin.read(item)
            if item.filename == 'word/stylesWithEffects.xml' and tag:
                root = etree.fromstring(data)
                normal = next(s for s in root if s.tag == qn('w:style') and s.get(qn('w:styleId')) == 'Normal')
                rpr = normal.find(qn('w:rPr'))
                if rpr is None:
                    rpr = etree.SubElement(normal, qn('w:rPr'))
                etree.SubElement(rpr, qn(tag))
                data = etree.tostring(root)
            if item.filename == 'word/_rels/document.xml.rels' and external:
                root = etree.fromstring(data)
                relation = next(r for r in root if r.get('Type') == REL)
                relation.set('TargetMode', 'External')
                relation.set('Target', 'https://example.invalid/styles.xml')
                data = etree.tostring(root)
            zout.writestr(item, data)
    return output.getvalue()

@pytest.mark.parametrize('tag', ['w:vanish', 'w:webHidden', 'w:specVanish'])
def test_alternate_style_hidden_requires_review(tag):
    with pytest.raises(ValueError, match='hidden text style'):
        extract_docx(effects_fixture(tag))

def test_external_effects_style_never_followed():
    with pytest.raises(ValueError, match='external styles'):
        extract_docx(effects_fixture(external=True))

def test_ordinary_effects_part_keeps_logical_text():
    result = extract_docx(effects_fixture())
    assert [r['raw_text'] for r in result['records']] == ['Visible prefix ', 'Synthetic body text.']
    assert all(r['physical_index'] is None and not r['printed_label_verified'] for r in result['records'])
