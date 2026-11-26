"""Actual offline custom CSL processing and hostile/frozen XML boundaries."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
import socket
import sys

import pytest

from carr_library.citations import csl


SOURCE = '''<?xml version="1.0" encoding="UTF-8"?>
<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0" class="in-text">
  <info>
    <title>Local research style</title>
    <id>urn:uuid:4991f1d9-1dc5-4ced-995c-572015e883be</id>
    <updated>2026-11-26T00:00:00Z</updated>
  </info>
  <locale><terms><term name="and">plus</term></terms></locale>
  <macro name="creator"><names variable="author"><name form="short" and="text"/></names></macro>
  <citation><layout prefix="[" suffix="]" delimiter="; "><group delimiter=", ">
    <text macro="creator"/><date variable="issued"><date-part name="year"/></date>
  </group></layout></citation>
  <bibliography><sort><key variable="title"/></sort><layout suffix="."><group delimiter=". ">
    <text macro="creator"/><text variable="title" font-style="italic"/>
    <date variable="issued"><date-part name="year"/></date>
  </group></layout></bibliography>
</style>'''

BOOK = {'id': 'known', 'type': 'book', 'title': 'Known source',
        'author': [{'family': 'Smith', 'given': 'Jane'}, {'family': 'Jones', 'given': 'John'}],
        'issued': {'date-parts': [[2020]]}}


def source_with(body, *, citation='<text variable="title"/>'):
    return (SOURCE[:SOURCE.index('  <locale>')]
            + '<citation><layout>' + citation + '</layout></citation>'
            + '<bibliography><layout>' + body + '</layout></bibliography></style>')


def test_exact_utf8_snapshot_and_metadata_are_frozen():
    data = ('\ufeff' + SOURCE.replace('Local research style', 'العربية: Український стиль')
            .replace('\n', '\r\n')).encode('utf-8')
    style = csl.load_custom_style(data)
    assert style.source is data
    assert style.source == data and style.sha256 == sha256(data).hexdigest()
    assert style.info == csl.StyleInfo('urn:uuid:4991f1d9-1dc5-4ced-995c-572015e883be',
                                      'العربية: Український стиль', 'in-text', '')
    assert csl.load_custom_style(data.decode('utf-8')) == style
    with pytest.raises(FrozenInstanceError):
        style.source = b'changed'
    with pytest.raises(FrozenInstanceError):
        style.info.title = 'changed'


def test_loader_never_interprets_content_as_a_path_or_url(tmp_path, monkeypatch):
    selected = tmp_path / 'chosen.csl'
    selected.write_text(SOURCE)

    def denied(*args, **kwargs):
        pytest.fail('The custom CSL loader must not read files or access the network.')

    monkeypatch.setattr(Path, 'open', denied)
    monkeypatch.setattr(socket, 'socket', denied)
    assert csl.load_custom_style(SOURCE).source == SOURCE.encode()
    for value in (str(selected), 'https://example.invalid/style.csl'):
        with pytest.raises(ValueError):
            csl.load_custom_style(value)


def test_actual_custom_macro_sort_inline_terms_and_citation_clusters():
    records = [dict(deepcopy(BOOK), id='z', title='Zebra'),
               dict(deepcopy(BOOK), id='a', title='Alpha')]
    clusters = [[{'id': 'z'}], [{'id': 'a'}], [{'id': 'a'}, {'id': 'z'}]]
    original = deepcopy((records, clusters))
    style = csl.load_custom_style(SOURCE)
    result = csl.render(records, style=style, citations=clusters)
    assert result.entries == ('Smith plus Jones. Alpha. 2020.', 'Smith plus Jones. Zebra. 2020.')
    assert result.entry_ids == (('a',), ('z',))
    assert result.citations == ('[Smith plus Jones, 2020]', '[Smith plus Jones, 2020]',
                                '[Smith plus Jones, 2020; Smith plus Jones, 2020]')
    assert result.style == style.info and result.style_sha256 == style.sha256
    assert result.custom_style is True and result.attribution == csl.ATTRIBUTION
    assert (records, clusters) == original


def test_custom_metadata_reaches_the_processor_xml_representation():
    converted = csl._xml_json(SOURCE)
    info = next(node for node in converted['children'] if node['name'] == 'info')
    metadata = {node['name']: node['children'] for node in info['children']}
    assert metadata['title'] == ['Local research style']
    assert metadata['id'] == ['urn:uuid:4991f1d9-1dc5-4ced-995c-572015e883be']


@pytest.mark.parametrize('locale', csl.LOCALES)
def test_actual_custom_style_uses_only_local_resources(locale, monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('Custom CSL formatting must not create a network socket.')

    monkeypatch.setattr(socket, 'socket', denied)
    result = csl.render([BOOK], style=csl.load_custom_style(SOURCE), locale=locale)
    assert result.locale == locale and 'Known source' in result.bibliography


def test_custom_style_explicit_locale_uses_another_pinned_locale(monkeypatch):
    requested = []
    resource = csl._resource

    def record_resource(name):
        requested.append(name)
        return resource(name)

    monkeypatch.setattr(csl, '_resource', record_resource)
    style = csl.load_custom_style(SOURCE.replace('<layout prefix="["', '<layout locale="uk-UA" prefix="["'))
    result = csl.render([dict(BOOK, language='uk-UA')], style=style, locale='en-US')
    assert 'Known source' in result.bibliography
    assert set(requested) == {'_vendor_citeproc/citeproc.js',
                             *(f'styles/locales-{locale}.xml' for locale in csl.LOCALES)}


def test_requested_locale_overrides_an_unbundled_style_default_without_fetch(monkeypatch):
    monkeypatch.setattr(socket, 'socket', lambda *args, **kwargs: pytest.fail('No locale fetch allowed.'))
    style = csl.load_custom_style(SOURCE.replace('class="in-text"',
                                               'class="in-text" default-locale="fr-FR"'))
    assert csl.render([BOOK], style=style, locale='en-US').entries == (
        'Smith plus Jones. Known source. 2020.',)


def test_custom_note_style_and_explicit_locator_keep_source_warning():
    source = source_with('<text variable="title"/>',
                         citation='<text variable="title"/><text variable="locator" prefix=", p. "/>')
    style = csl.load_custom_style(source.replace('class="in-text"', 'class="note"'))
    result = csl.render([dict(BOOK, page='10-30')], style=style,
                        citations=[[{'id': 'known', 'locator': '7', 'label': 'page'}]])
    assert result.style.kind == 'note' and result.citations == ('Known source, p. 7',)
    assert any('supplied manually' in warning for warning in result.warnings)
    assert any('publication extent' in warning for warning in result.warnings)


def test_custom_typography_html_is_sanitized_and_javascript_remains_inert(tmp_path):
    target = tmp_path / 'not_created'
    literal = "'); require('fs').writeFileSync('" + str(target) + "','bad'); //"
    source = source_with('<text value="&lt;b onclick=&quot;attack()&quot;&gt;Bold&lt;/b&gt;"/>'
                         '<text variable="title" font-style="italic"/>')
    style = csl.load_custom_style(source)
    result = csl.render([dict(BOOK, title=literal)], style=style, output='html')
    assert '<i>' in result.bibliography and 'writeFileSync' in result.bibliography
    assert '<b onclick=' not in result.bibliography and '<script' not in result.bibliography
    assert '&lt;b onclick=' in result.bibliography  # Preserved as inert text, never an attribute.
    assert 'href=' not in result.bibliography and not target.exists()


def test_reserved_words_in_literal_output_are_not_mistaken_for_processor_keys():
    style = csl.load_custom_style(source_with('<text value="constructor prototype __proto__"/>'
                                               '<text variable="title" prefix="; "/>'))
    assert csl.render([BOOK], style=style).entries == ('constructor prototype __proto__; Known source',)


@pytest.mark.parametrize('installed', csl.available_styles(), ids=lambda style: style.id)
def test_all_bundled_independent_styles_also_pass_custom_intake(installed):
    style = csl.load_custom_style(csl._resource('styles/' + installed.filename))
    assert style.info.title == installed.title and style.info.kind == installed.kind
    assert style.sha256 == csl._HASHES['styles/' + installed.filename]


def test_installed_results_also_identify_exact_style_bytes():
    result = csl.render([BOOK], style='harvard')
    assert result.custom_style is False
    assert result.style_sha256 == csl._HASHES['styles/harvard-cite-them-right.csl']


@pytest.mark.parametrize('data', [b'', '', None, Path('unread.csl'), bytearray(b'<style/>'),
                                 b'\xff', SOURCE.encode('utf-16'), '\ud800'])
def test_invalid_source_types_and_encodings_fail(data):
    with pytest.raises(ValueError):
        csl.load_custom_style(data)


@pytest.mark.parametrize('source', [
    SOURCE.replace('encoding="UTF-8"', 'encoding="ISO-8859-1"'),
    SOURCE.replace('http://purl.org/net/xbiblio/csl', 'https://example.invalid/namespace'),
    SOURCE.replace('xmlns="http://purl.org/net/xbiblio/csl" ', ''),
    SOURCE.replace('<title>', '<unknown>').replace('</title>', '</unknown>'),
    SOURCE.replace('version="1.0" class=', 'version="1.0.2" class='),
    SOURCE.replace('class="in-text"', 'class="executable"'),
    SOURCE.replace('class="in-text"', 'class="in-text" unknown="ignored"'),
    SOURCE.replace('<title>Local research style</title>', '<title/>'),
    SOURCE.replace('<title>Local research style</title>', '<title>A</title><title>B</title>'),
    SOURCE.replace('<title>Local research style</title>', '<title><text value="Mixed"/></title>'),
    SOURCE.replace('<id>urn:uuid:4991f1d9-1dc5-4ced-995c-572015e883be</id>', ''),
    SOURCE.replace('urn:uuid:4991f1d9-1dc5-4ced-995c-572015e883be', 'not one identifier'),
    SOURCE.replace('  <info>', '<citation><layout><text value="Early"/></layout></citation><info>'),
    SOURCE.replace('  <bibliography>', '<locale href="https://example.invalid/data.xml"/><bibliography>'),
    SOURCE.replace('<layout prefix="["', '<layout locale="fr-FR" prefix="["'),
    source_with('<text variable="title" value="ambiguous"/>'),
    source_with('<group>unexpected text</group>'),
    source_with('<text value="One"/>unexpected tail'),
    source_with('<choose><else><text value="No if"/></else></choose>'),
    source_with('<choose><if><text value="No test"/></if></choose>'),
    source_with('<text variable="title"/>').replace('<layout>', '<sort/>', 1).replace('</layout>', '', 1),
    source_with('<text variable="title"/>').replace('<bibliography>', '').replace('</bibliography>', ''),
    source_with(''),
])
def test_malformed_or_unsupported_styles_fail_before_runtime(source, monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError):
        csl.load_custom_style(source)


@pytest.mark.parametrize('declaration', [
    '<!DOCTYPE style>',
    '<!DOCTYPE style SYSTEM "file:///must-not-read">',
    '<!DOCTYPE style [<!ENTITY text "expansion">]>',
    '<!DOCTYPE style [<!ENTITY external SYSTEM "https://example.invalid/external">]>',
])
def test_dtd_entities_and_external_resources_are_forbidden(declaration, monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail('Custom XML must not read external entities.')

    monkeypatch.setattr(socket, 'socket', denied)
    monkeypatch.setattr(Path, 'open', denied)
    with pytest.raises(ValueError, match='DTDs, entities or external'):
        csl.load_custom_style(SOURCE.replace('<style ', declaration + '\n<style ', 1))


def test_processing_instruction_cannot_load_an_external_stylesheet():
    source = SOURCE.replace('<style ', '<?xml-stylesheet href="https://example.invalid/evil"?>\n<style ', 1)
    with pytest.raises(ValueError, match='processing instructions'):
        csl.load_custom_style(source)


@pytest.mark.parametrize('fragment', [
    '<x:text xmlns:x="https://example.invalid/foreign" variable="title"/>',
    '<include xmlns="http://www.w3.org/2001/XInclude" href="file:///private"/>',
    '<text xmlns:x="https://example.invalid/foreign" x:variable="title"/>',
    '<text xml:base="file:///private/" variable="title"/>',
])
def test_foreign_namespaces_cannot_acquire_csl_semantics(fragment):
    with pytest.raises(ValueError, match='namespace'):
        csl.load_custom_style(source_with(fragment))


@pytest.mark.parametrize('location', ['title', 'id', 'term', 'attribute', 'comment'])
@pytest.mark.parametrize('control', ['\u202e', '\u2067', '\x85', '&#x202e;', '&#x2067;', '&#x85;'])
def test_style_controls_cannot_bypass_validation_with_character_references(location, control):
    if location == 'title':
        source = SOURCE.replace('Local research style', 'Title' + control)
    elif location == 'id':
        source = SOURCE.replace('urn:uuid:', 'urn:uuid:' + control)
    elif location == 'term':
        source = SOURCE.replace('>plus<', '>plus' + control + '<')
    elif location == 'attribute':
        source = SOURCE.replace('prefix="["', 'prefix="[' + control + '"')
    else:
        # Character references in comments are inert, not decoded XML text.
        source = SOURCE.replace('<style ', '<!-- comment' + control + ' -->\n<style ', 1)
        if control.startswith('&#'):
            assert csl.load_custom_style(source).source == source.encode()
            return
    with pytest.raises(ValueError, match='control'):
        csl.load_custom_style(source)


@pytest.mark.parametrize('original,replacement', [
    ('name="creator"', 'name="__proto__"'),
    ('macro="creator"', 'macro="constructor"'),
    ('name="and"', 'name="__proto__"'),
    ('name="and"', 'name="__PROTO__"'),
    ('name="and"', 'name="and" form="__proto__"'),
    ('name="and"', 'name="and" gender-form="constructor"'),
    ('name="and"', 'name="and" match="hasOwnProperty"'),
    ('form="short"', 'form="toString"'),
    ('variable="title"', 'variable="title constructor"'),
    ('<locale>', '<locale xml:lang="__proto__">'),
    ('class="in-text"', 'class="in-text" default-locale="constructor"'),
])
def test_prototype_sensitive_values_are_rejected_before_engine(original, replacement, monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError, match='reserved processor identifier'):
        csl.load_custom_style(SOURCE.replace(original, replacement))


@pytest.mark.parametrize('name', ['ordinal-__proto__', 'ordinal-constructor', 'ordinalX__proto__',
                                 'ordinal-100', 'ordinal-invalid'])
def test_derived_ordinal_keys_cannot_mutate_processor_prototypes(name, monkeypatch):
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError, match='ordinal terms'):
        csl.load_custom_style(SOURCE.replace('name="and"', 'name="' + name + '"'))


def test_valid_inline_ordinals_are_rendered_by_the_processor():
    source = source_with('<text variable="title"/>'
                         '<number variable="edition" form="ordinal" prefix=" (" suffix=")"/>')
    source = source.replace('<citation>', '<locale><terms><term name="ordinal">th</term>'
        '<term name="ordinal-01">st</term></terms></locale><citation>')
    style = csl.load_custom_style(source)
    assert csl.render([dict(BOOK, edition='1')], style=style).entries == ('Known source (1st)',)


@pytest.mark.parametrize('name', ['__proto__-US', 'constructor-US', 'toString-US', 'en-__proto__'])
def test_locale_components_cannot_hide_reserved_keys(name):
    with pytest.raises(ValueError, match='locale identifier'):
        csl.load_custom_style(SOURCE.replace('<locale>', '<locale xml:lang="' + name + '">'))


def test_dependent_style_error_identifies_parent_requirement_without_fetch(monkeypatch):
    monkeypatch.setattr(socket, 'socket', lambda *args, **kwargs: pytest.fail('No parent fetch allowed.'))
    dependent = ('<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0"><info>'
                 '<title>Dependent</title><id>dependent</id>'
                 '<link rel="independent-parent" href="https://example.invalid/parent.csl"/>'
                 '</info></style>')
    with pytest.raises(ValueError, match='Dependent CSL styles require an external parent'):
        csl.load_custom_style(dependent)
    with pytest.raises(ValueError, match='Dependent CSL'):
        csl.load_custom_style(SOURCE.replace('</info>',
            '<link rel="independent-parent" href="https://example.invalid/parent"/></info>'))


def test_source_byte_limit_counts_utf8_bytes_before_xml_parsing():
    with pytest.raises(ValueError, match='512 KiB'):
        csl.load_custom_style(b'x' * (csl.MAX_CUSTOM_STYLE_BYTES + 1))
    with pytest.raises(ValueError, match='512 KiB'):
        csl.load_custom_style('Ж' * (csl.MAX_CUSTOM_STYLE_BYTES // 2 + 1))


def test_xml_depth_boundary_is_enforced_before_recursive_conversion():
    body = '<group>' * 60 + '<text variable="title"/>' + '</group>' * 60
    assert csl.load_custom_style(source_with(body)).info.title == 'Local research style'
    with pytest.raises(ValueError, match='XML depth limit'):
        csl.load_custom_style(source_with('<group>' + body + '</group>'))


def test_xml_node_budget_rejects_a_broad_small_document():
    source = source_with('<text value="x"/>' * csl.MAX_CUSTOM_STYLE_NODES)
    assert len(source.encode()) < csl.MAX_CUSTOM_STYLE_BYTES
    with pytest.raises(ValueError, match='node budget'):
        csl.load_custom_style(source)


def test_xml_attribute_count_and_value_budgets():
    attrs = ' '.join(f'a{number}="x"' for number in range(65))
    with pytest.raises(ValueError, match='attribute budget'):
        csl.load_custom_style(source_with('<text ' + attrs + '/>'))
    body = '<text value="" prefix="" suffix="" form="" plural="" quotes="" display=""/>' * 4800
    assert len(source_with(body).encode()) < csl.MAX_CUSTOM_STYLE_BYTES
    with pytest.raises(ValueError, match='attribute budget'):
        csl.load_custom_style(source_with(body))
    with pytest.raises(ValueError, match='oversized custom CSL attribute'):
        csl.load_custom_style(source_with('<text value="' + 'x' * 8193 + '"/>'))


def test_xml_text_budget_includes_metadata_and_comments():
    for content in ('<summary>' + 'x' * (256 * 1024 + 1) + '</summary>',
                    '<!--' + 'x' * (256 * 1024 + 1) + '-->'):
        with pytest.raises(ValueError, match='text budget'):
            csl.load_custom_style(SOURCE.replace('</info>', content + '</info>'))


@pytest.mark.parametrize('macros,reference,error', [
    ('<macro name="a"><text macro="a"/></macro>', 'a', 'recursive macros'),
    ('<macro name="a"><text macro="b"/></macro><macro name="b"><text macro="a"/></macro>',
     'a', 'recursive macros'),
    ('<macro name="a"><text value="A"/></macro><macro name="a"><text value="B"/></macro>',
     'a', 'unique'),
    ('<macro name="a"><text value="A"/></macro>', 'missing', 'undefined macro'),
])
def test_macro_definition_errors_are_rejected(macros, reference, error):
    source = source_with('<text macro="' + reference + '"/>').replace('<citation>', macros + '<citation>')
    with pytest.raises(ValueError, match=error):
        csl.load_custom_style(source)


def test_flat_xml_cannot_hide_an_excessively_deep_macro_chain():
    macros = ''.join('<macro name="m' + str(number) + '"><text macro="m' + str(number + 1)
                     + '"/></macro>' for number in range(64))
    macros += '<macro name="m64"><text value="End"/></macro>'
    source = source_with('<text macro="m0"/>').replace('<citation>', macros + '<citation>')
    with pytest.raises(ValueError, match='macro depth limit'):
        csl.load_custom_style(source)


@pytest.mark.parametrize('change', ['source', 'hash', 'title', 'class', 'filename', 'source_type'])
def test_manually_constructed_or_changed_snapshots_are_revalidated_at_render(change, monkeypatch):
    style = csl.load_custom_style(SOURCE)
    if change == 'source':
        style = replace(style, source=SOURCE.replace('>plus<', '>and<').encode())
    elif change == 'hash':
        style = replace(style, sha256='0' * 64)
    elif change == 'title':
        style = replace(style, info=replace(style.info, title='Forged label'))
    elif change == 'class':
        style = replace(style, info=replace(style.info, kind='note'))
    elif change == 'filename':
        style = replace(style, info=replace(style.info, filename='/private.csl'))
    else:
        style = replace(style, source=SOURCE)
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError, match='snapshot'):
        csl.render([BOOK], style=style)


def test_matching_hash_cannot_bypass_style_validation_via_dataclass_constructor(monkeypatch):
    valid = csl.load_custom_style(SOURCE)
    bad = SOURCE.replace('name="and"', 'name="__proto__"').encode()
    forged = csl.CustomStyle(valid.info, bad, sha256(bad).hexdigest())
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError, match='reserved'):
        csl.render([BOOK], style=forged)


def test_custom_source_counts_toward_the_combined_input_budget(monkeypatch):
    style = csl.load_custom_style(SOURCE)
    monkeypatch.setitem(sys.modules, 'quickjs', None)
    with pytest.raises(ValueError, match='formatting byte budget'):
        csl.render([BOOK], style=style, budget=csl.RenderBudget(max_input_bytes=len(style.source)))


def test_actual_custom_runtime_cpu_and_output_budgets():
    style = csl.load_custom_style(SOURCE)
    with pytest.raises(RuntimeError, match='resource budget'):
        csl.render([BOOK], style=style, budget=csl.RenderBudget(cpu_seconds=0.000001))
    with pytest.raises(RuntimeError, match='output budget'):
        csl.render([BOOK], style=style, budget=csl.RenderBudget(max_output_bytes=8))
