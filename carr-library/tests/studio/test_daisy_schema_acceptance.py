"""Exact pinned schema aliases, exercised by the real lxml resolver and parser."""
import ast
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

from lxml import etree
import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'daisy_schema_acceptance.py'
spec = importlib.util.spec_from_file_location('reviewed_daisy_schema_acceptance', SCRIPT)
acceptance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acceptance)


def resolver_type(paths, *, recorded=None):
    """Execute the actual nested class, with only its explicit pinned input bound."""
    tree = ast.parse(SCRIPT.read_text(encoding='utf-8'))
    validate = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == 'validate')
    resolver = next(node for node in validate.body
                    if isinstance(node, ast.ClassDef) and node.name == 'Resolver')
    native = etree
    if recorded is not None:
        class ObservedResolver(etree.Resolver):
            def resolve_filename(self, filename, context):
                recorded.append(filename)
                return super().resolve_filename(filename, context)
        native = SimpleNamespace(Resolver=ObservedResolver)
    namespace = dict(acceptance.__dict__, paths=paths, etree=native)
    module = ast.Module(body=[deepcopy(resolver)], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(SCRIPT), 'exec'), namespace)
    return namespace['Resolver']


@pytest.mark.parametrize('url', [
    'file:/D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent',
    'file:///D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent',
    'D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent',
    'http://www.w3.org/TR/xhtml1/DTD/xhtml-special.ent',
])
def test_exact_pinned_windows_entity_alias_resolves_to_the_selected_file(url):
    selected = PureWindowsPath('D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent')
    recorded = []
    resolver = resolver_type({'xhtml-special.ent': selected}, recorded=recorded)()
    assert resolver.resolve(url, None, None) is not None
    assert recorded == [str(selected)]


@pytest.mark.parametrize('url', [
    'file:/D:/a/_temp/other/xhtml-special.ent',
    'file://server/D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent',
    'file://D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent',
    'file:/D:/a/_temp/carr-daisy-validation-schemas/../other/xhtml-special.ent',
    'file:/D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent?selected=true',
    'file:/D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent#entity',
    'file:/D:/a/_temp/carr-daisy-validation-schemas%2F/xhtml-special.ent',
    'file:/D:/a/_temp/carr-daisy-validation-schemas/other.ent',
    'https://www.w3.org/TR/xhtml1/DTD/xhtml-special.ent',
    'http://example.invalid/TR/xhtml1/DTD/xhtml-special.ent',
])
def test_alias_does_not_grant_other_roots_authorities_entities_or_urls(url):
    selected = PureWindowsPath('D:/a/_temp/carr-daisy-validation-schemas/xhtml-special.ent')
    recorded = []
    resolver = resolver_type({'xhtml-special.ent': selected}, recorded=recorded)()
    with pytest.raises(ValueError, match='unapproved resource'):
        resolver.resolve(url, None, None)
    assert recorded == []


def test_actual_dtd_parser_resolves_the_exact_single_slash_local_entity(tmp_path):
    schema = tmp_path / 'approved schemas'; schema.mkdir()
    entity = schema / 'xhtml-special.ent'
    entity.write_bytes(b'<!ENTITY approved "known local entity">')
    dtd = schema / 'xhtml1-strict.dtd'
    alias = entity.as_uri().replace('file:///', 'file:/', 1)
    dtd.write_text('<!ENTITY % local SYSTEM "' + alias + '">\n%local;\n'
                   '<!ELEMENT doc (#PCDATA)>\n', encoding='utf-8')
    paths = {path.name: path for path in (entity, dtd)}
    original = {name: path.read_bytes() for name, path in paths.items()}
    Resolver = resolver_type(paths)
    parser = etree.XMLParser(load_dtd=True, dtd_validation=True, no_network=True,
                             resolve_entities=False, huge_tree=False)
    parser.resolvers.add(Resolver())
    declaration = b'<!DOCTYPE doc SYSTEM "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">'
    document = etree.fromstring(declaration + b'<doc>Exact local schema</doc>', parser)
    assert document.tag == 'doc' and document.text == 'Exact local schema'
    with pytest.raises(etree.XMLSyntaxError):
        etree.fromstring(declaration + b'<doc><unapproved /></doc>',
                         acceptance.parser_for_html(Resolver, etree))
    assert {name: path.read_bytes() for name, path in paths.items()} == original


def test_actual_parser_rejects_same_named_entity_outside_the_pinned_directory(tmp_path):
    schema = tmp_path / 'approved'; schema.mkdir()
    other = tmp_path / 'outside'; other.mkdir()
    selected, foreign = schema / 'xhtml-special.ent', other / 'xhtml-special.ent'
    selected.write_bytes(b'<!ENTITY sample "approved">')
    foreign.write_bytes(b'<!ENTITY sample "foreign">')
    dtd = schema / 'xhtml1-strict.dtd'
    alias = foreign.as_uri().replace('file:///', 'file:/', 1)
    dtd.write_text('<!ENTITY % local SYSTEM "' + alias + '">\n%local;\n'
                   '<!ELEMENT doc (#PCDATA)>\n', encoding='utf-8')
    Resolver = resolver_type({selected.name: selected, dtd.name: dtd})
    with pytest.raises(ValueError, match='unapproved resource'):
        etree.fromstring(
            b'<!DOCTYPE doc SYSTEM "http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd"><doc>source</doc>',
            acceptance.parser_for_html(Resolver, etree))
    assert selected.read_bytes() == b'<!ENTITY sample "approved">'
    assert foreign.read_bytes() == b'<!ENTITY sample "foreign">'


def test_changed_pinned_schema_is_rejected_before_opening_the_publication(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    (project / 'packaging').mkdir(parents=True)
    (project / 'scripts').mkdir()
    selected = tmp_path / 'schemas'; selected.mkdir()
    path = selected / 'xhtml-special.ent'
    expected = b'<!ENTITY sample "approved">'
    path.write_bytes(expected.replace(b'approved', b'foreign!'))
    pin = {'files': {path.name: {'size': len(expected), 'sha256': sha256(expected).hexdigest()}}}
    (project / 'packaging' / 'daisy-validation-schemas.json').write_text(json.dumps(pin), encoding='utf-8')
    monkeypatch.setattr(acceptance, '__file__', str(project / 'scripts' / SCRIPT.name))
    publication = tmp_path / 'never-opened.daisy.zip'
    with pytest.raises(ValueError, match='does not match its pin'):
        acceptance.validate(publication, selected)
    assert not publication.exists()
    assert path.read_bytes() == expected.replace(b'approved', b'foreign!')
