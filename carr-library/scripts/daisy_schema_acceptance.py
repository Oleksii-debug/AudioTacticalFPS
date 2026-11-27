"""Offline structural validation with explicitly supplied, hash-pinned DTDs.

Requires the separate lxml validation tool. Does not download/install anything,
resolve arbitrary entities or certify full DAISY rules/reader interoperability.
"""
import argparse
import importlib.util
import os
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse
from zipfile import ZipFile


def validate(publication, schemas):
    from lxml import etree
    from carr_library.durable_state import local_path, regular
    from carr_library.audio import daisy, publishing
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
    for name, path in paths.items():
        if sha256(path.read_bytes()).hexdigest() != identities[name]:
            raise ValueError('A validation schema changed during the conformance check: ' + name)
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


def acceptance(schemas, *, require_installed=False):
    """Actual current producer fixtures; the reused DTD checker stays offline."""
    from carr_library.native_runtime import activate_bundled_runtime
    specification = importlib.util.spec_from_file_location('current_daisy_acceptance_support',
        Path(__file__).with_name('publication_acceptance_support.py'))
    fixture = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(fixture)
    produce, runtime = fixture.produce, fixture.runtime
    native_handle = activate_bundled_runtime()
    try:
        ffmpeg, runtime_evidence, unchanged = runtime(require_installed=require_installed)
        with TemporaryDirectory(prefix='carr-daisy-conformance-') as temporary:
            root = Path(temporary).resolve(strict=True)
            results = []
            for record in produce(root, 'daisy', ffmpeg):
                result = validate(record['path'], schemas)
                if result['publication_sha256'] != record['receipt']['artifact']['sha256']:
                    raise AssertionError('DTD validation did not bind the saved publication receipt bytes.')
                result['case'] = record['case']
                results.append(result)
            unchanged()
            return {'schema': 1, 'status': 'PASS', **runtime_evidence, 'publications': results,
                'audio_fixture': 'Public synthetic PCM through actual current Audio Studio and FFmpeg',
                'full_daisy_rule_conformance': 'NOT_RUN', 'reader_interoperability': 'NOT_RUN',
                'speech_quality': 'NOT_RUN', 'native_menu': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    finally:
        if native_handle is not None:
            native_handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication', type=Path,
                        help='Existing DAISY archive; omit to render the four current Studio acceptance fixtures')
    parser.add_argument('--schemas', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--require-installed', action='store_true',
                        help='Require exact installed application modules and sealed bundled audio')
    args = parser.parse_args()
    if args.publication is not None and args.require_installed:
        parser.error('--require-installed applies to the actual render acceptance, not an existing unqualified archive')
    result = (validate(args.publication, args.schemas) if args.publication is not None
              else acceptance(args.schemas, require_installed=args.require_installed))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
