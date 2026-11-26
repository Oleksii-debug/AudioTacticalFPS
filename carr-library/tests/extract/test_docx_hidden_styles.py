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
