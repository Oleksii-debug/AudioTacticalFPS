"""Independent offline DTD validation for current DAISY 2.02 publications.

lxml is a separately selected CI validator, not an application dependency.
No download, arbitrary entity lookup, reader or NVDA qualification is claimed.
"""
import argparse
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from zipfile import ZipFile


SCHEMA_NAMES = {'SMIL10.dtd', 'xhtml-lat1.ent', 'xhtml-special.ent',
                'xhtml-symbol.ent', 'xhtml1-transitional.dtd'}


def fixture_module():
    specification = importlib.util.spec_from_file_location('carr_publication_acceptance_fixture',
        Path(__file__).with_name('epubcheck_acceptance.py'))
    if specification is None or specification.loader is None:
        raise RuntimeError('The explicit shared acceptance fixture is missing.')
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def schema_paths(schemas):
    from carr_library.audio.jobs import checked_directory, regular_file, read_json, sha256_file
    root = checked_directory(schemas)
    manifest = read_json(Path(__file__).resolve().parents[1] / 'packaging' /
                         'daisy-validation-schemas.json', limit=32768)
    if type(manifest) is not dict or manifest.get('schema') != 1 or set(manifest.get('files', {})) != SCHEMA_NAMES:
        raise ValueError('The reviewed DAISY validation schema inventory changed.')
    paths, identities = {}, {}
    for name, expected in manifest['files'].items():
        path = root / name
        if (type(expected) is not dict or set(expected) != {'url', 'size', 'sha256'}
                or type(expected['size']) is not int or not 0 < expected['size'] <= 128 * 1024
                or not isinstance(expected['sha256'], str)
                or re.fullmatch('[0-9a-f]{64}', expected['sha256']) is None
                or regular_file(path).st_size != expected['size']
                or sha256_file(path) != expected['sha256']):
            raise ValueError('The explicitly selected validation schema failed its pin: ' + name)
        paths[name], identities[name] = path, expected['sha256']
    return paths, identities


def local_resolver(etree, paths):
    """Allow only the five explicitly pinned files and their exact DTD aliases."""
    allowed = {}
    for name, path in paths.items():
        for alias in (str(path), path.as_posix(), path.as_uri(), name):
            allowed[alias] = path
        url = ('http://www.w3.org/TR/REC-SMIL/SMIL10.dtd' if name == 'SMIL10.dtd'
               else 'http://www.w3.org/TR/xhtml1/DTD/' + name)
        allowed[url] = path

    class Resolver(etree.Resolver):
        def resolve(self, url, public_id, context):
            if url not in allowed:
                raise ValueError('The schema validator refused an unapproved external resource.')
            return self.resolve_filename(str(allowed[url]), context)
    return Resolver


def parser_for_html(resolver, etree):
    parser = etree.XMLParser(load_dtd=True, dtd_validation=True, no_network=True,
                             resolve_entities=False, huge_tree=False)
    parser.resolvers.add(resolver())
    return parser


def validate(publication, schemas):
    from lxml import etree
    from carr_library.audio import daisy, publishing
    from carr_library.audio.jobs import regular_file, sha256_file
    paths, identities = schema_paths(schemas)
    Resolver = local_resolver(etree, paths)
    regular_file(publication)
    provenance = daisy.inspect_daisy(publication)
    before = sha256_file(publication)
    results = []
    with ZipFile(publication) as archive:
        for name in ('ncc.html', 'content.html', 'source.html', 'book.smil'):
            parser = parser_for_html(Resolver, etree)
            etree.fromstring(archive.read(name), parser)
            results.append({'file': name, 'dtd_valid': True})
        original = archive.read('ncc.html')
        invalid = original.replace(b'<body>', b'<body><unsupported-element />', 1)
        if invalid == original:
            raise AssertionError('The independent invalid-XHTML fixture did not change the document.')
        try:
            etree.fromstring(invalid, parser_for_html(Resolver, etree))
        except etree.XMLSyntaxError:
            rejected = True
        else:
            raise AssertionError('The deliberately invalid XHTML passed actual DTD validation.')
    if sha256_file(publication) != before:
        raise ValueError('The DAISY archive changed during independent structural validation.')
    for name, path in paths.items():
        if sha256_file(path) != identities[name]:
            raise ValueError('A pinned schema changed during the conformance check.')
    return {'schema': 1, 'status': 'PASS', 'publication_sha256': before,
        'publisher_module_sha256': sha256_file(publishing.__file__),
        'daisy_module_sha256': sha256_file(daisy.__file__),
        'lxml_version': list(etree.LXML_VERSION), 'libxml_version': list(etree.LIBXML_VERSION),
        'schema_sha256': identities, 'documents': results, 'invalid_xhtml_rejected': rejected,
        'original_script_sha256': provenance['source']['source_script_sha256'],
        'full_daisy_rule_conformance': 'NOT_RUN', 'reader_interoperability': 'NOT_RUN', 'nvda': 'NOT_RUN'}


def acceptance(schemas, *, require_installed=False, bundle_root=None):
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime(bundle_root)
    try:
        fixture = fixture_module()
        binary, modules, audio = fixture._application(require_installed=require_installed,
                                                      bundle_root=bundle_root)
        results = []
        with TemporaryDirectory(prefix='resonance-daisy-conformance-') as temporary:
            for name, output, receipt, provenance in fixture.acceptance_cases(
                    Path(temporary).resolve(), 'daisy-2.02', ffmpeg=binary):
                result = validate(output, schemas)
                if result['publication_sha256'] != receipt['artifact']['sha256']:
                    raise AssertionError('Independent validation did not bind the actual export receipt.')
                result['case'] = name
                results.append(result)
        return {'schema': 1, 'status': 'PASS', 'publications': results,
                'application_module_sha256': modules, 'audio_runtime': audio,
                'audio_fixture': 'Synthetic PCM tones through actual current production and FFmpeg',
                'full_daisy_rule_conformance': 'NOT_RUN', 'reader_interoperability': 'NOT_RUN',
                'speech_quality': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    finally:
        if handle is not None:
            handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication', type=Path,
                        help='Existing DAISY archive; omit to render the current three acceptance fixtures')
    parser.add_argument('--schemas', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--require-installed', action='store_true')
    parser.add_argument('--bundle-root', type=Path)
    args = parser.parse_args()
    if args.publication is not None and (args.require_installed or args.bundle_root is not None):
        parser.error('Installed acceptance requires actual fixture rendering, not only an existing archive.')
    schemas = args.schemas.resolve(strict=True)
    result = (validate(args.publication.resolve(strict=True), schemas) if args.publication is not None
              else acceptance(schemas, require_installed=args.require_installed,
                              bundle_root=args.bundle_root.resolve(strict=True) if args.bundle_root else None))
    fixture_module()._write_report(args.report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
