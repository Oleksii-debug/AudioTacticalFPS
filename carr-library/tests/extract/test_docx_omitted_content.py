"""Real OOXML packages that python-docx's text API can silently shorten."""
from io import BytesIO

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from lxml import etree
import pytest

from carr_library.extract.docx_ordered import extract_docx


MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'


def snapshot(document):
    target = BytesIO()
    document.save(target)
    return target.getvalue()


def run(text, *, math=False):
    prefix = 'm' if math else 'w'
    node = OxmlElement(prefix + ':r')
    child = OxmlElement(prefix + ':t')
    child.text = text
    node.append(child)
    return node


def paragraph_in(document, location):
    if location == 'body':
        return document.add_paragraph('Before ')
    return document.add_table(rows=1, cols=1).cell(0, 0).paragraphs[0]


@pytest.mark.parametrize('location', ['body', 'cell'])
@pytest.mark.parametrize('structure', ['smart_tag', 'equation', 'ruby', 'symbol'])
def test_omitted_inline_content_requires_explicit_review(location, structure):
    document = Document()
    document.add_paragraph('Keep ordinary text before the unsupported element.')
    paragraph = paragraph_in(document, location)
    if structure == 'smart_tag':
        element = OxmlElement('w:smartTag')
        element.append(run('omitted words'))
        paragraph._p.append(element)
    elif structure == 'equation':
        element = OxmlElement('m:oMath')
        element.append(run('x=1', math=True))
        paragraph._p.append(element)
    elif structure == 'ruby':
        element = OxmlElement('w:ruby')
        element.append(OxmlElement('w:rubyPr'))
        for name, text in [('w:rt', 'annotation'), ('w:rubyBase', 'base text')]:
            part = OxmlElement(name)
            part.append(run(text))
            element.append(part)
        paragraph.add_run()._r.append(element)
    else:
        element = OxmlElement('w:sym')
        element.set(qn('w:font'), 'Symbol')
        element.set(qn('w:char'), 'F061')
        paragraph.add_run()._r.append(element)
    paragraph.add_run(' after')
    with pytest.raises(ValueError, match='unsupported body structure'):
        extract_docx(snapshot(document))


@pytest.mark.parametrize('location', ['body', 'paragraph', 'cell'])
def test_alternate_content_is_not_silently_discarded(location):
    document = Document()
    document.add_paragraph('Visible before.')
    alternate = etree.Element('{' + MC + '}AlternateContent', nsmap={'mc': MC})
    choice = etree.SubElement(alternate, '{' + MC + '}Choice', Requires='w14')
    fallback = etree.SubElement(alternate, '{' + MC + '}Fallback')
    for parent in (choice, fallback):
        element = run('Alternative visible text')
        if location in ('body', 'cell'):
            paragraph = OxmlElement('w:p')
            paragraph.append(element)
            element = paragraph
        parent.append(element)
    if location == 'body':
        document.element.body.insert(1, alternate)
    elif location == 'paragraph':
        document.add_paragraph('Before ')._p.append(alternate)
    else:
        document.add_table(rows=1, cols=1).cell(0, 0)._tc.append(alternate)
    document.add_paragraph('Visible after.')
    with pytest.raises(ValueError, match='unsupported body structure'):
        extract_docx(snapshot(document))


def test_display_equation_between_body_paragraphs_requires_review():
    document = Document()
    document.add_paragraph('The formula is:')
    display = OxmlElement('m:oMathPara')
    equation = OxmlElement('m:oMath')
    equation.append(run('x=1', math=True))
    display.append(equation)
    document.add_paragraph()._p.append(display)
    document.add_paragraph('This paragraph follows the equation.')
    with pytest.raises(ValueError, match='unsupported body structure'):
        extract_docx(snapshot(document))


def test_smart_tag_nested_in_hyperlink_requires_review():
    document = Document()
    paragraph = document.add_paragraph('Reference: ')
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('w:anchor'), 'LocalBookmark')
    wrapper = OxmlElement('w:smartTag')
    wrapper.append(run('lost link label'))
    hyperlink.append(wrapper)
    paragraph._p.append(hyperlink)
    with pytest.raises(ValueError, match='unsupported body structure'):
        extract_docx(snapshot(document))


def test_plain_unicode_hyperlink_and_formatting_remain_supported():
    document = Document()
    paragraph = document.add_paragraph('  Refer to ')
    hyperlink = OxmlElement('w:hyperlink')
    hyperlink.set(qn('w:anchor'), 'LocalBookmark')
    hyperlink.append(run('слово / slovo'))
    paragraph._p.append(hyperlink)
    paragraph.add_run('\tand the next line\n').bold = True
    result = extract_docx(snapshot(document))
    assert result['records'][0]['raw_text'] == '  Refer to слово / slovo\tand the next line\n'
    assert result['status'] == 'review_required'
    assert result['records'][0]['logical_locator'] == [1]
    assert result['records'][0]['physical_index'] is None


def test_soft_hyphen_does_not_silently_join_word_parts():
    document = Document()
    text = document.add_paragraph('co').add_run()
    text._r.append(OxmlElement('w:softHyphen'))
    text.add_text('operate')
    with pytest.raises(ValueError, match='unsupported body structure'):
        extract_docx(snapshot(document))


def test_supported_nonbreaking_hyphen_keeps_word_separator():
    document = Document()
    text = document.add_paragraph('co').add_run()
    text._r.append(OxmlElement('w:noBreakHyphen'))
    text.add_text('operate')
    result = extract_docx(snapshot(document))
    # python-docx deliberately maps this OOXML element to ASCII hyphen.
    # This preview is still not a verified layout or quotation.
    assert result['records'][0]['raw_text'] == 'co-operate'
    assert result['status'] == 'review_required'


@pytest.mark.parametrize('tag', ['w:dir', 'w:bdo'])
@pytest.mark.parametrize('location', ['body', 'cell'])
def test_bidirectional_wrappers_do_not_discard_contained_text(tag, location):
    document = Document()
    paragraph = paragraph_in(document, location)
    wrapper = OxmlElement(tag)
    wrapper.set(qn('w:val'), 'rtl')
    wrapper.append(run('שלום'))
    paragraph._p.append(wrapper)
    paragraph.add_run(' after')
    with pytest.raises(ValueError, match='unsupported body structure'):
        extract_docx(snapshot(document))
