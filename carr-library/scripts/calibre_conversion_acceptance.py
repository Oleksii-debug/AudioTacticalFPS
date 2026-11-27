"""Actual installed-package acceptance for eight explicitly supplied public fixtures.

The companion generator authors real unprotected fixtures. Every one must be
converted by the selected executable through the actual installed service.
No fake engine, optional skip, download or encrypted-document bypass is used.
"""
import argparse
from hashlib import sha256
import importlib
import json
import os
import sysconfig
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

import carr_library
from carr_library.conversion.office import checked_directory, regular_file, verified_reader
from carr_library.durable_state import local_path
from carr_library.conversion import ConversionService, ConversionBudget, ConversionError, ConversionUnavailable
from carr_library.conversion.service import _digest

from carr_library.conversion.calibre import CALIBRE_SUFFIXES, CalibreSelection

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
            if (':' in part or part.endswith((' ', '.')) or any(ord(character) < 32 for character in part)
                    or part.split('.')[0].casefold() in {'con', 'prn', 'aux', 'nul',
                        *(f'com{number}' for number in range(1, 10)), *(f'lpt{number}' for number in range(1, 10))}):
                raise ValueError('A fixture component is not a portable ordinary filename.')
        source = path.parent.joinpath(*relative.parts)
        checked_directory(source.parent)
        info = regular_file(source)
        if info.st_nlink != 1 or not 0 < info.st_size <= 64 * 1024**2:
            raise ValueError('Fixture bytes exceed the current conversion allowance.')
        if _digest(source, maximum=info.st_size) != row['sha256']:
            raise ValueError('A selected fixture checksum does not match its inventory.')
        seen.add(row['format'])
        result.append((dict(row), source))
    if seen != CALIBRE_SUFFIXES:
        raise ValueError('The native gate must contain all eight exact formats.')
    return tuple(result), sha256(raw).hexdigest()


MODULES = (
    'carr_library', 'carr_library.conversion.calibre', 'carr_library.conversion.service',
    'carr_library.conversion.worker', 'carr_library.conversion.office',
    'carr_library.conversion.formats', 'carr_library.conversion.models',
    'carr_library.audio.process', 'carr_library.audio.cleanup', 'carr_library.durable_state',
)


def installed_sources():
    checkout = Path(__file__).resolve().parents[1] / 'src'
    paths = {Path(value).resolve() for key, value in sysconfig.get_paths().items()
             if key in ('purelib', 'platlib')}
    result = {}
    for name in MODULES:
        module = importlib.import_module(name)
        path = Path(module.__file__).resolve()
        if not any(path.is_relative_to(base) for base in paths) or path.is_relative_to(checkout):
            raise AssertionError('Native acceptance requires the normally imported installed package: ' + name)
        relative = Path(*name.split('.'))
        source = checkout / relative / '__init__.py' if path.name == '__init__.py' else checkout / relative.with_suffix('.py')
        raw = path.read_bytes()
        if raw != source.read_bytes():
            raise AssertionError('Installed native acceptance source differs from this checkout: ' + name)
        result[name] = {'path': str(path), 'sha256': sha256(raw).hexdigest()}
    return result


def generation_evidence(manifest, inventory_hash, selected):
    path = manifest.parent / 'generation.json'
    info = regular_file(path)
    if not 0 < info.st_size <= MAX_MANIFEST_BYTES:
        raise ValueError('The fixture generation evidence exceeds its budget.')
    with verified_reader(path) as stream:
        raw = stream.read(info.st_size + 1)
    if len(raw) != info.st_size:
        raise ValueError('The fixture generation evidence changed during capture.')
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate fixture generation evidence key.')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    expected = {'schema', 'status', 'fixture_inventory_sha256', 'source_text', 'source_html_sha256',
        'fixture_text_license', 'generator_sha256', 'selected_version', 'selected_core_sha256',
        'formats', 'references', 'negative_control', 'drm_removal', 'downloaded_documents',
        'distributed_with_application'}
    if (type(value) is not dict or set(value) != expected
            or value['schema'] != 'carr-calibre-authored-fixtures/1'
            or value['status'] != 'GENERATED_NOT_QUALIFIED'
            or value['fixture_inventory_sha256'] != inventory_hash
            or value['fixture_text_license'] != 'CC0-1.0'
            or value['generator_sha256'] != sha256(Path(__file__).with_name('create_calibre_fixtures.py').read_bytes()).hexdigest()
            or value['selected_core_sha256'] != sha256(json.dumps(selected, sort_keys=True,
                ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
            or value['selected_version'] not in ('ebook-convert (calibre 9.19.0)', 'ebook-convert.exe (calibre 9.19.0)')
            or value['drm_removal'] is not False or value['downloaded_documents'] is not False
            or value['distributed_with_application'] is not False):
        raise ValueError('Fixture generation evidence does not bind the current authored inputs and selected tool.')
    control = value['negative_control']
    if (type(control) is not dict or set(control) != {'path', 'sha256', 'kind'}
            or control['path'] != 'scanned-no-text.djvu'
            or control['kind'] != 'valid-image-without-text-layer'
            or type(control['sha256']) is not str or len(control['sha256']) != 64
            or any(character not in '0123456789abcdef' for character in control['sha256'])):
        raise ValueError('Invalid bounded authored negative control.')
    return value, sha256(raw).hexdigest()


def qualify(executable, manifest):
    before_origins = installed_sources()
    chosen = CalibreSelection.capture(executable)
    selected = chosen.snapshot()
    rows, manifest_hash = fixtures(manifest)
    generated, generation_hash = generation_evidence(Path(manifest), manifest_hash, selected)
    results, versions = [], set()
    with TemporaryDirectory(prefix='carr-calibre-qualified-') as temporary:
        stage = Path(temporary)
        output = stage / 'reading'
        output.mkdir()
        # These settings deliberately point at an unselected sentinel. Only the
        # explicit private native environment may reach the selected converter.
        sentinel = stage / 'unselected-user-config'
        sentinel.mkdir()
        sentinel_file = sentinel / 'keep.txt'
        sentinel_file.write_bytes(b'Private configuration was never selected.')
        names = ('CALIBRE_CONFIG_DIRECTORY', 'CALIBRE_DEVELOP_FROM', 'CALIBRE_OVERRIDE_DATABASE_PATH',
                 'CALIBRE_CACHE_DIRECTORY', 'SSLKEYLOGFILE')
        previous = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ[name] = str(sentinel)
            budget = ConversionBudget(max_source_bytes=64 * 1024**2, max_text_bytes=4 * 1024**2,
                                      memory_bytes=4 * 1024**3, timeout=120)
            converter = ConversionService(output, input_roots=tuple(path for _, path in rows),
                                          budget=budget, calibre=chosen)
            for index, (row, source) in enumerate(rows, 1):
                result = converter.extract(source, f'{index:02d}-reading.txt')
                if (result.report['source_sha256'] != row['sha256']
                        or _digest(source, maximum=64 * 1024**2) != row['sha256']
                        or result.report['source_format'] != row['format']
                        or result.report['engine'] != 'selected-calibre-' + row['format'][1:]
                        or result.report['original_modified'] is not False
                        or result.report['verified_for_quotation'] is not False
                        or result.cleanup_warning is not None):
                    raise AssertionError('Native conversion lost identity or did not complete its cleanup.')
                with verified_reader(result.path) as stream:
                    raw = stream.read(budget.max_text_bytes + 1)
                if len(raw) > budget.max_text_bytes:
                    raise AssertionError('Native text exceeded its fixed allowance.')
                text = raw.decode('utf-8', 'strict')
                if not all(needle in text for needle in row['contains']):
                    raise AssertionError('Native converted text failed an exact fixture assertion: ' + row['format'])
                version = result.report['metadata']['source_locators'][0]['converter']
                if version != generated['selected_version']:
                    raise AssertionError('Native result used another selected converter version.')
                versions.add(version)
                if json.loads(result.report_path.read_bytes()) != result.report:
                    raise AssertionError('The saved provenance does not match the returned completion.')
                results.append(dict(format=row['format'], source_sha256=row['sha256'],
                                    text_sha256=sha256(raw).hexdigest(), status='PASS'))
                try:
                    converter.extract(source, result.path)
                except FileExistsError:
                    pass
                else:
                    raise AssertionError('The native route overwrote an existing derivative.')
            source = rows[0][1]
            try:
                ConversionService(output, input_roots=(source,), budget=budget).extract(source, 'unselected.txt')
            except ConversionUnavailable:
                pass
            else:
                raise AssertionError('A legacy book activated a converter without explicit selection.')
            control = Path(manifest).parent / generated['negative_control']['path']
            if _digest(control, maximum=64 * 1024**2) != generated['negative_control']['sha256']:
                raise AssertionError('The authored no-text control changed.')
            try:
                ConversionService(output, input_roots=(control,), budget=budget, calibre=chosen).extract(
                    control, 'no-text-control.txt')
            except ConversionError:
                pass
            else:
                raise AssertionError('The selected book route inferred OCR for an image-only DjVu.')
            if (output / 'no-text-control.txt').exists() or (output / 'no-text-control.txt.provenance.json').exists():
                raise AssertionError('The refused image-only DjVu published a result.')
            if sentinel_file.read_bytes() != b'Private configuration was never selected.' or sorted(
                    path.name for path in sentinel.iterdir()) != ['keep.txt']:
                raise AssertionError('Native conversion touched unselected user configuration.')
            if chosen.snapshot() != selected:
                raise AssertionError('Selected native core changed during qualification.')
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    after_origins = installed_sources()
    if after_origins != before_origins:
        raise AssertionError('Installed native sources changed during qualification.')
    return dict(status='PASS', native_conversions=8, fixtures=results,
        fixture_inventory_sha256=manifest_hash, generation_evidence_sha256=generation_hash,
        versions=sorted(versions), installed_sources_before=before_origins,
        installed_sources_after=after_origins,
        negative_controls={'no_implicit_selection': 'PASS', 'image_only_djvu_refused': 'PASS',
                           'no_clobber': 'PASS', 'private_configuration': 'PASS'},
        selected_core_sha256=sha256(json.dumps(selected, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest(),
        fixture_scope='source-authored books; uncompressed CHM; DjVu embedded text with JPEG page',
        arbitrary_document_fidelity='NOT_QUALIFIED', scanned_djvu_ocr='NOT_RUN',
        decryption_or_drm_removal='NOT_SUPPORTED', distributed_runtime=False)


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
