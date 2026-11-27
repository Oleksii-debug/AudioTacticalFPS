"""Actual installed-package acceptance for eight explicitly supplied public fixtures.

No download, fixture fabrication, fake engine or optional skip is performed.
Supply unprotected synthetic documents and their expected text/hash inventory.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import carr_library
from carr_library.audio.jobs import checked_directory, regular_file, verified_reader
from carr_library.audio.model_manifest import digest_file, local_path
from carr_library.audio.paths import filename_component
from carr_library.conversion import ConversionService
from carr_library.conversion.calibre import CALIBRE_SUFFIXES, CalibreSelection
from carr_library.operations import ResourceBroker
from carr_library.operations.models import ResourceLimits

SCHEMA = 'carr-calibre-fixtures/1'
MAX_MANIFEST_BYTES = 64 * 1024


def fixtures(path):
    path = local_path(path)
    info = regular_file(path)
    if info.st_nlink != 1 or not 0 < info.st_size <= MAX_MANIFEST_BYTES:
        raise ValueError('Use a bounded regular fixture inventory.')
    with verified_reader(path) as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) != info.st_size:
        raise ValueError('The fixture inventory changed.')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate fixture inventory key.')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    if (type(value) is not dict or set(value) != {'schema', 'files'} or value['schema'] != SCHEMA
            or type(value['files']) is not list or len(value['files']) != len(CALIBRE_SUFFIXES)):
        raise ValueError('Provide one reviewed fixture for each of the eight required formats.')
    result, seen = [], set()
    for row in value['files']:
        if (type(row) is not dict or set(row) != {'format', 'path', 'sha256', 'contains'}
                or type(row['format']) is not str or row['format'] not in CALIBRE_SUFFIXES
                or row['format'] in seen or type(row['path']) is not str
                or type(row['sha256']) is not str or len(row['sha256']) != 64
                or any(character not in '0123456789abcdef' for character in row['sha256'])
                or type(row['contains']) is not list or not 1 <= len(row['contains']) <= 16
                or any(type(text) is not str or not text.strip() or '\x00' in text
                       or len(text.encode('utf-8')) > 4096 for text in row['contains'])):
            raise ValueError('Invalid bounded fixture assertion.')
        relative = PurePosixPath(row['path'])
        if (relative.is_absolute() or '/'.join(relative.parts) != row['path']
                or '\\' in row['path'] or any(part in ('', '.', '..') for part in relative.parts)
                or relative.suffix != row['format']):
            raise ValueError('Use one relative local fixture with its declared suffix.')
        for part in relative.parts:
            filename_component(part)
        source = path.parent.joinpath(*relative.parts)
        checked_directory(source.parent)
        info = regular_file(source)
        if info.st_nlink != 1 or not 0 < info.st_size <= 64 * 1024**2:
            raise ValueError('Fixture bytes exceed the current conversion allowance.')
        if digest_file(source, max_bytes=info.st_size) != row['sha256']:
            raise ValueError('A selected fixture checksum does not match its inventory.')
        seen.add(row['format'])
        result.append((dict(row), source))
    if seen != CALIBRE_SUFFIXES:
        raise ValueError('The native gate must contain all eight exact formats.')
    return tuple(result), sha256(raw).hexdigest()


def qualify(executable, manifest):
    chosen = CalibreSelection.capture(executable)
    selected = chosen.snapshot()
    rows, manifest_hash = fixtures(manifest)
    results, versions = [], set()
    with TemporaryDirectory(prefix='carr-calibre-qualified-') as temporary:
        stage = Path(temporary)
        output, library = stage / 'reading', stage / 'library'
        output.mkdir()
        library.mkdir()
        broker = ResourceBroker.for_app(library, limits=ResourceLimits(
            cpu_slots=2, memory_bytes=3 * 1024**3, disk_bytes=512 * 1024**2,
            min_free_disk_bytes=32 * 1024**2))
        converter = ConversionService(output, input_roots=tuple(path for _, path in rows),
                                      calibre=executable)
        for index, (row, source) in enumerate(rows, 1):
            result = converter.extract(source, f'{index:02d}-reading.txt', broker=broker)
            if (result.report['source_sha256'] != row['sha256']
                    or digest_file(source, max_bytes=64 * 1024**2) != row['sha256']
                    or result.report['source_format'] != row['format']
                    or result.report['engine'] != 'selected-calibre-' + row['format'][1:]
                    or result.report['original_modified'] is not False
                    or result.report['verified_for_quotation'] is not False):
                raise AssertionError('Native conversion lost the selected fixture identity.')
            with verified_reader(result.path) as stream:
                raw = stream.read(32 * 1024**2 + 1)
            if len(raw) > 32 * 1024**2:
                raise AssertionError('Native text exceeded its fixed allowance.')
            text = raw.decode('utf-8', 'strict')
            if not all(needle in text for needle in row['contains']):
                raise AssertionError('Native converted text failed an exact fixture assertion.')
            version = result.report['segments'][0]['logical_locator']['converter']
            versions.add(version)
            results.append(dict(format=row['format'], source_sha256=row['sha256'],
                                text_sha256=sha256(raw).hexdigest(), status='PASS'))
        if chosen.snapshot() != selected:
            raise AssertionError('Selected native core changed during qualification.')
    return dict(status='PASS', native_conversions=8, fixtures=results,
        fixture_inventory_sha256=manifest_hash, versions=sorted(versions),
        selected_core_sha256=sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest(),
        arbitrary_document_fidelity='NOT_QUALIFIED', scanned_djvu_ocr='NOT_RUN',
        decryption_or_drm_removal='NOT_SUPPORTED')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--calibre', type=Path, required=True,
                        help='Explicit self-contained installed ebook-convert path')
    parser.add_argument('--fixtures', type=Path, required=True,
                        help='Bounded inventory of eight public unprotected fixtures')
    args = parser.parse_args(argv)
    package = Path(carr_library.__file__).resolve()
    if package.is_relative_to(Path(__file__).resolve().parents[1] / 'src'):
        raise RuntimeError('Run the native gate with the installed wheel and isolated Python.')
    report = qualify(args.calibre, args.fixtures)
    report['installed_package'] = True
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
