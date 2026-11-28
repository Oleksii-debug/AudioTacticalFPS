"""Real lxml DTD resolution with closed approved local schema aliases.

The miniature DTDs are synthetic structural fixtures, not substitute DAISY
schemas. The existing native acceptance still verifies the real five DTD pins,
three publications and its invalid-XHTML control.
"""
from hashlib import sha256
import importlib.util
from pathlib import Path, PureWindowsPath
from xml.sax.saxutils import quoteattr

from lxml import etree
import pytest


def helper():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'daisy_schema_acceptance.py'
    spec = importlib.util.spec_from_file_location('_daisy_closed_schema_resolver', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def schemas(tmp_path):
    root = tmp_path / 'approved schemas \u03a9'
    root.mkdir()
    paths = {name: root / name for name in ('xhtml1-transitional.dtd', 'xhtml-special.ent', 'SMIL10.dtd')}
    paths['xhtml1-transitional.dtd'].write_bytes(
        b'<!ENTITY % special SYSTEM "xhtml-special.ent">\n%special;\n<!ELEMENT html (body)>\n')
    paths['xhtml-special.ent'].write_bytes(
        b'<!ELEMENT body (#PCDATA)>\n<!ENTITY fixture "synthetic entity">\n')
    paths['SMIL10.dtd'].write_bytes(b'<!ELEMENT smil EMPTY>\n')
    return paths


def spelling(path, kind):
    if kind == 'native':
        return str(path)
    if kind == 'canonical':
        return path.as_uri()
    if kind == 'short-file':
        return 'file:/' + path.as_uri()[len('file:///'):]
    assert kind == 'official'
    return 'http://www.w3.org/TR/xhtml1/DTD/' + path.name


def document(url, body=b'<body>plain &fixture;</body>'):
    return ('<!DOCTYPE html SYSTEM ' + quoteattr(url) + '>').encode('utf-8') + b'<html>' + body + b'</html>'


@pytest.mark.parametrize('kind', ['native', 'canonical', 'short-file', 'official'])
def test_actual_lxml_accepts_only_bound_root_and_sibling_entities_with_local_uri_spellings(schemas, kind):
    module = helper()
    before = {name: sha256(path.read_bytes()).hexdigest() for name, path in schemas.items()}
    base = module.schema_resolver(etree, schemas)
    visited = []
    class Observed(base):
        def resolve(self, url, public_id, context):
            visited.append(url)
            return super().resolve(url, public_id, context)
    parsed = etree.fromstring(document(spelling(schemas['xhtml1-transitional.dtd'], kind)),
                              module.parser_for_html(Observed, etree))
    assert parsed.tag == 'html' and len(visited) == 2
    assert any('xhtml-special.ent' in url for url in visited)
    assert b'&fixture;' in etree.tostring(parsed)  # resolve_entities remains False.
    assert {name: sha256(path.read_bytes()).hexdigest() for name, path in schemas.items()} == before


@pytest.mark.parametrize('kind', ['native', 'canonical', 'short-file', 'official'])
def test_actual_lxml_invalid_xhtml_still_fails_after_local_uri_mapping(schemas, kind):
    module = helper()
    resolver = module.schema_resolver(etree, schemas)
    with pytest.raises(etree.XMLSyntaxError):
        etree.fromstring(document(spelling(schemas['xhtml1-transitional.dtd'], kind),
                                  b'<body><unsupported-element /></body>'),
                         module.parser_for_html(resolver, etree))


def test_actual_lxml_keeps_separate_exact_smil_alias(schemas):
    module = helper()
    parser = module.parser_for_html(module.schema_resolver(etree, schemas), etree)
    result = etree.fromstring(b'<!DOCTYPE smil SYSTEM "http://www.w3.org/TR/REC-SMIL/SMIL10.dtd"><smil/>', parser)
    assert result.tag == 'smil'
    with pytest.raises(ValueError, match='unapproved resource'):
        module.schema_resolver(etree, schemas)().resolve(
            'http://www.w3.org/TR/xhtml1/DTD/SMIL10.dtd', None, None)


@pytest.mark.parametrize('kind', ['other-host', 'https', 'file-host', 'query', 'fragment',
                                 'sibling-outside', 'relative', 'percent-name'])
def test_closed_schema_resolver_never_infers_authority_from_basename_or_url_normalization(schemas, kind):
    module = helper()
    approved = schemas['xhtml-special.ent']
    urls = {
        'other-host': 'http://unapproved.invalid/xhtml-special.ent',
        'https': 'https://www.w3.org/TR/xhtml1/DTD/xhtml-special.ent',
        'file-host': 'file://unapproved.invalid' + approved.as_posix(),
        'query': approved.as_uri() + '?schema=1',
        'fragment': approved.as_uri() + '#schema',
        'sibling-outside': (approved.parent.parent / approved.name).as_uri(),
        'relative': approved.name,
        'percent-name': approved.as_uri().replace('xhtml-special', 'xhtml-%73pecial'),
    }
    with pytest.raises(ValueError, match='unapproved resource'):
        module.schema_resolver(etree, schemas)().resolve(urls[kind], None, None)


@pytest.mark.parametrize('external', ['file', 'network'])
def test_actual_lxml_cannot_escape_approved_dtd_through_an_unlisted_parameter_entity(schemas, tmp_path, external):
    module = helper()
    outside = tmp_path / 'outside.ent'
    outside.write_bytes(b'<!ELEMENT html EMPTY>')
    url = outside.as_uri() if external == 'file' else 'http://unapproved.invalid/outside.ent'
    schemas['xhtml1-transitional.dtd'].write_bytes(
        ('<!ENTITY % outside SYSTEM ' + quoteattr(url) + '>%outside;').encode('utf-8'))
    with pytest.raises(ValueError, match='unapproved resource'):
        etree.fromstring(document(spelling(schemas['xhtml1-transitional.dtd'], 'official'), b''),
                         module.parser_for_html(module.schema_resolver(etree, schemas), etree))
    assert outside.read_bytes() == b'<!ELEMENT html EMPTY>'


def test_windows_short_file_uri_maps_only_the_exact_approved_drive_path():
    module = helper()
    selected = PureWindowsPath('D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent')
    events = []
    class RecordingResolver:
        def resolve_filename(self, filename, context):
            events.append((filename, context))
            return filename
    class ResolverAPI:
        Resolver = RecordingResolver
    resolver = module.schema_resolver(ResolverAPI, {'xhtml-special.ent': selected})()
    marker = object()
    assert resolver.resolve('file:/D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent',
                            None, marker) == str(selected)
    assert events == [(str(selected), marker)]
    for url in ('file:/E:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent',
                'file://host/D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent',
                'file:/D:/a/_temp/other/xhtml-special.ent'):
        with pytest.raises(ValueError, match='unapproved resource'):
            resolver.resolve(url, None, marker)
    assert events == [(str(selected), marker)]
