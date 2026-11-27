"""Real ODF reading through the bounded disabled print-layout exception."""
import pytest

from carr_library.extract.document_formats_xml import extract_xml_document
from tests.extract.test_document_formats_xml import ODF, O, T, odf, texts, zipped
from io import BytesIO
from zipfile import ZipFile


S = ODF + 'style:1.0'
FO = ODF + 'xsl-fo-compatible:1.0'
REGIONS = ('header', 'header-first', 'header-left',
           'footer', 'footer-first', 'footer-left')


def _styles(fragment, *, location='master-page'):
    if location == 'master-page':
        fragment = '<office:master-styles><style:master-page style:name="Page">' + fragment + '</style:master-page></office:master-styles>'
    elif location == 'named-style':
        fragment = '<office:styles><style:style>' + fragment + '</style:style></office:styles>'
    elif location == 'nested-master-page':
        fragment = '<office:master-styles><style:master-page><style:header>' + fragment + '</style:header></style:master-page></office:master-styles>'
    elif location == 'master-styles':
        fragment = '<office:master-styles>' + fragment + '</office:master-styles>'
    return f'<office:document-styles xmlns:office="{O}" xmlns:style="{S}" xmlns:text="{T}" xmlns:fo="{FO}">' + fragment + '</office:document-styles>'


@pytest.mark.parametrize('suffix', ['.odt', '.ods', '.odp'])
@pytest.mark.parametrize('region', REGIONS)
def test_empty_disabled_print_region_preserves_exact_body_and_locators(suffix, region):
    expected = extract_xml_document(odf(suffix), suffix)
    fragment = f'<style:{region} style:display="false"/>'
    actual = extract_xml_document(odf(suffix, styles=_styles(fragment)), suffix)
    assert actual['records'] == expected['records']
    assert texts(actual) == texts(expected)
    assert set(actual['warnings']) == set(expected['warnings']) | {'empty_disabled_page_regions_not_read'}


@pytest.mark.parametrize('fragment', [
    '<style:header-left style:display="false">Hidden source</style:header-left>',
    '<style:header-first style:display="false"><text:p>Hidden source</text:p></style:header-first>',
    '<style:footer-left style:display="false"><text:p/></style:footer-left>',
    '<style:footer-first style:display="false"><text:page-number/></style:footer-first>',
    '<style:header-left style:display="false"/><text:hidden-text/>',
    '<style:header-left style:display="false"/>Hidden tail',
    '<style:header-left style:display="false" foreign="hidden"/>',
    '<style:footer-first style:display="false" style:name="Unreviewed"/>',
], ids=['direct-text', 'paragraph-text', 'empty-child', 'dynamic-field',
        'blocked-sibling', 'text-tail', 'foreign-attribute', 'extra-style-attribute'])
def test_disabled_print_region_never_swallows_source_or_unknown_structure(fragment):
    with pytest.raises(ValueError, match='Hidden|Conditional'):
        extract_xml_document(odf('.ods', styles=_styles(fragment)), '.ods')


@pytest.mark.parametrize('location', ['root', 'named-style', 'master-styles', 'nested-master-page'])
def test_disabled_region_requires_exact_print_layout_ancestry(location):
    fragment = '<style:header-left style:display="false"/>'
    with pytest.raises(ValueError, match='Hidden'):
        extract_xml_document(odf('.odt', styles=_styles(fragment, location=location)), '.odt')


@pytest.mark.parametrize('attribute', ['text:display="none"', 'text:display="false"',
                                       'fo:visibility="hidden"', 'fo:visibility="collapse"'])
def test_disabled_print_region_does_not_override_other_hidden_content_rules(attribute):
    fragment = f'<style:header-left style:display="false" {attribute}/>'
    with pytest.raises(ValueError, match='Hidden'):
        extract_xml_document(odf('.ods', styles=_styles(fragment)), '.ods')


def test_content_xml_cannot_impersonate_styles_print_layout():
    fragment = f'<office:master-styles xmlns:style="{S}"><style:master-page><style:header-left style:display="false"/></style:master-page></office:master-styles>'
    with ZipFile(BytesIO(odf('.odt'))) as archive:
        entries = [(item.filename, archive.read(item).replace(
            b'<office:body>', fragment.encode('utf-8') + b'<office:body>')
            if item.filename == 'content.xml' else archive.read(item))
            for item in archive.infolist()]
    with pytest.raises(ValueError, match='Hidden'):
        extract_xml_document(zipped(entries), '.odt')
