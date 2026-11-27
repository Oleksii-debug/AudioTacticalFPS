"""Offline structural validation with explicitly supplied, hash-pinned DTDs.

Requires the separate lxml validation tool. Does not download/install anything,
resolve arbitrary entities or certify full DAISY rules/reader interoperability.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlparse
from zipfile import ZipFile


def validate(publication, schemas):
    from lxml import etree
    from carr_library.durable_state import local_path, regular
    from carr_library.studio import daisy, publishing
    schemas = local_path(schemas, exists=True)
    manifest = json.loads((Path(__file__).resolve().parents[1] / 'packaging' /
                           'daisy-validation-schemas.json').read_text(encoding='utf-8'))
    paths, identities = {}, {}
    for name, expected in manifest['files'].items():
        path = local_path(schemas / name, root=schemas, exists=True)
        if regular(path).st_size != expected['size'] or sha256(path.read_bytes()).hexdigest() != expected['sha256']:
            raise ValueError('The explicitly selected validation schema does not match its pin: ' + name)
        paths[name], identities[name] = path, expected['sha256']
    class Resolver(etree.Resolver):
        def resolve(self, url, public_id, context):
            parsed = urlparse(url)
            name = Path(parsed.path).name
            allowed = {str(path) for path in paths.values()} | {
                'http://www.w3.org/TR/xhtml1/DTD/' + name for name in paths if name != 'SMIL10.dtd'} | {
                'http://www.w3.org/TR/REC-SMIL/SMIL10.dtd'}
            if url not in allowed or name not in paths:
                raise ValueError('The schema validator refused an unapproved resource: ' + url)
            return self.resolve_filename(str(paths[name]), context)
    publication = local_path(publication, exists=True)
    provenance = daisy.inspect_daisy(publication)
    before = publishing._digest(publication, lambda: None, 32 * 1024**3)
    results = []
    with ZipFile(publication) as archive:
        for name in ('ncc.html', 'content.html', 'source.html', 'book.smil'):
            parser = etree.XMLParser(load_dtd=True, dtd_validation=True, no_network=True,
                                     resolve_entities=False, huge_tree=False)
            parser.resolvers.add(Resolver())
            etree.fromstring(archive.read(name), parser)
            results.append({'file': name, 'dtd_valid': True})
        # A well-formed but structurally invalid XHTML must actually fail.
        invalid = archive.read('ncc.html').replace(b'<body>', b'<body><unsupported-element />', 1)
        try:
            etree.fromstring(invalid, parser_for_html(Resolver, etree))
        except etree.XMLSyntaxError:
            rejected = True
        else:
            raise AssertionError('The deliberately invalid XHTML passed DTD validation.')
    if publishing._digest(publication, lambda: None, 32 * 1024**3) != before:
        raise ValueError('The DAISY archive changed during structural validation.')
    return {'schema': 1, 'status': 'PASS', 'publication_sha256': before['sha256'],
        'publisher_module_sha256': sha256(Path(publishing.__file__).read_bytes()).hexdigest(),
        'daisy_module_sha256': sha256(Path(daisy.__file__).read_bytes()).hexdigest(),
        'lxml_version': list(etree.LXML_VERSION), 'libxml_version': list(etree.LIBXML_VERSION),
        'schema_sha256': identities, 'documents': results, 'invalid_xhtml_rejected': rejected,
        'original_script_sha256': provenance['source']['source_script_sha256'],
        'full_daisy_rule_conformance': 'NOT_RUN', 'reader_interoperability': 'NOT_RUN', 'nvda': 'NOT_RUN'}


def parser_for_html(resolver, etree):
    parser = etree.XMLParser(load_dtd=True, dtd_validation=True, no_network=True,
                             resolve_entities=False, huge_tree=False)
    parser.resolvers.add(resolver())
    return parser


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication', required=True, type=Path)
    parser.add_argument('--schemas', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    result = validate(args.publication, args.schemas)
    with args.report.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
