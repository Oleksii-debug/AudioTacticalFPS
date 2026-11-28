"""Independent EPUBCheck 5.3.0 for the actual EPUB layout formatter.

Uses the already pinned checker archive and existing checker invocation helper.
No acquisition, audio engine, provider, Office application or user document is
opened. These original synthetic sources prove conformance, not language quality.
"""
import argparse
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
import sys
import sysconfig
from tempfile import TemporaryDirectory
from zipfile import ZipFile


def digest(path):
    result = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--java', required=True, type=Path)
    parser.add_argument('--lock', required=True, type=Path)
    parser.add_argument('--source-root', required=True, type=Path)
    parser.add_argument('--require-installed', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    from carr_library.assistant import translation_epub_layout_worker as formatter
    from carr_library.assistant import translation_epub_layout, translation_layout, translation_partition
    from carr_library.extract import epub_ordered
    modules = (formatter, translation_epub_layout, translation_layout, translation_partition, epub_ordered)
    source_root = args.source_root.resolve(strict=True)
    installed = Path(sysconfig.get_path('platlib')).resolve(strict=True)
    origins = {}
    for module in modules:
        actual = Path(module.__file__).resolve(strict=True)
        expected = source_root.joinpath(*module.__name__.split('.')).with_suffix('.py')
        if digest(actual) != digest(expected):
            raise RuntimeError('The formatter module differs from the selected source.')
        if args.require_installed and (not actual.is_relative_to(installed) or actual.is_relative_to(source_root)):
            raise RuntimeError('The conformance probe requires the exact installed formatter.')
        origins[module.__name__] = {'path': str(actual), 'sha256': digest(actual)}
    helper_path = Path(__file__).with_name('epubcheck_acceptance.py').resolve(strict=True)
    fixture_path = Path(__file__).with_name('translation_layout_fixtures.py').resolve(strict=True)
    helper_sha, fixture_sha = digest(helper_path), digest(fixture_path)
    checker = load(helper_path, 'current_epubcheck_invocation')
    fixture = load(fixture_path, 'formatted_epub_original_sources')
    archive = args.archive.resolve(strict=True)
    pin = json.loads(args.lock.read_text(encoding='utf-8'))
    if (pin['version'] != '5.3.0' or archive.stat().st_size != pin['size']
            or digest(archive) != pin['sha256']):
        raise ValueError('Supply the existing exact pinned EPUBCheck 5.3.0 archive.')
    java = args.java.resolve(strict=True)
    java_sha = digest(java)
    rows = []
    with TemporaryDirectory(prefix='resonance-epub-layout-conformance-') as temporary:
        root = Path(temporary).resolve()
        tools = root / 'validator'
        tools.mkdir()
        with ZipFile(archive) as zipped:
            for entry in zipped.infolist():
                destination = tools / entry.filename
                if (not destination.resolve().is_relative_to(tools)
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError('The pinned validator archive has an invalid member.')
            zipped.extractall(tools)
        for notice in pin['notices']:
            if not (tools / notice).is_file():
                raise ValueError('A pinned validator license notice is missing.')
        jar = tools / pin['jar']
        sources = (
            ('table-style-media', fixture.epub_bytes()),
            ('preformatted', fixture.epub_bytes(chapters=1,
                first_body='<h1 id="heading">Original title.</h1><pre>Line one.\nLine two.</pre>')),
            ('whole-block-link', fixture.epub_bytes(chapters=1,
                first_body='<h1 id="heading">Original title.</h1><p><a href="#heading">'
                           '<strong>One complete </strong><em>linked block.</em></a></p>')),
        )
        for name, source in sources:
            mapping = formatter.describe(source)
            replacements = [
                {'ordinal': unit['ordinal'], 'source_sha256': unit['sha256'],
                 'text': 'Synthetic translated text: ' + record['text'][unit['start']:unit['end']]}
                for record in mapping['records'] for unit in record['units']]
            data, receipt = formatter.render(source, replacements, 'uk')
            publication = root / (name + '.epub')
            publication.write_bytes(data)
            result = checker.validate(java, jar, publication, root / (name + '.json'))
            if result['sha256'] != receipt['output_sha256']:
                raise RuntimeError('The validated EPUB differs from the actual formatting receipt.')
            rows.append({**result, 'source_sha256': sha256(source).hexdigest(),
                         'mapping_sha256': receipt['mapping_sha256'],
                         'changed_members': receipt['changed_members']})
        invalid = root / 'invalid-mimetype.epub'
        with ZipFile(publication) as original, ZipFile(invalid, 'x') as destination:
            for entry in original.infolist():
                destination.writestr(entry, b'application/not-epub' if entry.filename == 'mimetype'
                                     else original.read(entry))
        rejected = checker.validate(java, jar, invalid, root / 'invalid.json', expected_valid=False)
    if (digest(archive) != pin['sha256'] or digest(java) != java_sha
            or digest(helper_path) != helper_sha or digest(fixture_path) != fixture_sha
            or any(digest(item['path']) != item['sha256'] for item in origins.values())):
        raise RuntimeError('Conformance inputs changed during the independent checker run.')
    report = {'schema': 'carr-formatted-epub-conformance/1', 'status': 'PASS',
        'validator_version': '5.3.0', 'validator_archive_sha256': pin['sha256'],
        'validator_helper_sha256': helper_sha, 'java_sha256': java_sha,
        'fixture_sha256': fixture_sha, 'source_origins': origins, 'publications': rows,
        'invalid_mimetype_rejected': rejected, 'actual_formatter': 'PASS',
        'real_translation_service': 'NOT_RUN', 'translation_quality': 'NOT_RUN',
        'visual_layout': 'NOT_RUN', 'reader_interoperability': 'NOT_RUN', 'nvda': 'NOT_RUN'}
    if args.output is not None:
        from carr_library.speech._io import new_output
        with new_output(args.output) as target:
            target.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2), flush=True)
    return 0


if __name__ == '__main__':
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    try:
        raise SystemExit(main())
    finally:
        if handle is not None:
            handle.close()
