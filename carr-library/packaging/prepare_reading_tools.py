"""Build reviewed optional reading-tool packs from already downloaded local assets.

This is a qualification/install utility, not an application downloader. It never
runs pip or native code. The caller explicitly supplies reviewed local archives
and a previously built x64 Hunspell DLL. Core package inventories are unchanged.
"""
from contextlib import contextmanager
from hashlib import sha256
import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import struct
import tarfile
import zipfile

from carr_library.audio.jobs import file_identity, regular_file, verified_reader
from carr_library.audio.model_manifest import LocalModelManifest, local_path
from carr_library.reader.preparation import (
    WHEELS, LANGUAGES, HUNSPELL_REVISION, HUNSPELL_VERSION, MAX_RUNTIME_FILES,
)

SOURCE_SHA256 = '9c650ace31d4d8cc1da925a9507c3de1d50f1a54857af2f72429f517d5d53467'
SOURCE_BYTES = 1167432
FIXTURE_AFF = ('SET UTF-8\nTRY esiarntolcdugmphbyfvkwzxjq\n'
               'REP 2\nREP helo hello\nREP wrld world\n'
               'SFX S Y 1\nSFX S 0 s .\n').encode('utf-8')
FIXTURE_DIC = '6\nhello\nworld/S\nbook/S\nreading\nПривіт\nслово\n'.encode('utf-8')


@contextmanager
def checked(path, *, digest=None, maximum=16 * 1024 * 1024, exact_size=None):
    path = local_path(path)
    info = regular_file(path)
    if not 0 < info.st_size <= maximum or exact_size is not None and info.st_size != exact_size:
        raise ValueError('Optional source asset size is invalid.')
    with verified_reader(path, expected_identity=file_identity(info)) as stream:
        actual = sha256()
        while chunk := stream.read(1024 * 1024):
            actual.update(chunk)
        if digest is not None and actual.hexdigest() != digest:
            raise ValueError('Optional source asset does not match the reviewed publisher hash.')
        stream.seek(0)
        yield stream


def write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(raw)


def seal(root, engine, model_id, revision, license_name, settings):
    files = {path.relative_to(root).as_posix(): sha256(path.read_bytes()).hexdigest()
             for path in sorted(root.rglob('*')) if path.is_file()}
    if not 1 <= len(files) <= MAX_RUNTIME_FILES:
        raise ValueError('Optional runtime has an invalid file inventory.')
    value = dict(schema_version=1, engine=engine, model_id=model_id, revision=revision,
                 license=license_name, license_accepted=True, trusted_weights=True,
                 personal_noncommercial=False, settings=settings, files=files)
    path = root / 'manifest.json'
    write(path, (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8'))
    LocalModelManifest.load(path, engine=engine).verify(require_complete=True, max_asset_bytes=128 * 1024 * 1024)
    return path


def build_normalizer(wheelhouse, output, *, reviewed=False):
    if not reviewed:
        raise ValueError('Review exact code/data licenses and source hashes before building the optional pack.')
    output = local_path(output, must_exist=False)
    output.mkdir(mode=0o700)
    try:
        count, total = 0, 0
        for filename, expected in WHEELS.items():
            with checked(Path(wheelhouse) / filename, digest=expected) as stream:
                with zipfile.ZipFile(stream) as archive:
                    if len(archive.infolist()) > 4096:
                        raise ValueError('Optional wheel inventory exceeds its bound.')
                    names = set()
                    for entry in archive.infolist():
                        name = entry.filename
                        path = PurePosixPath(name)
                        if (path.is_absolute() or path.as_posix() != name.rstrip('/')
                                or '..' in path.parts or '\\' in name or ':' in name
                                or name in names or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                            raise ValueError('Unsafe optional wheel member.')
                        names.add(name)
                        if entry.is_dir():
                            continue
                        first, basename = path.parts[0], path.name
                        metadata = first.endswith('.dist-info') and (
                            basename == 'METADATA' or 'LICENSE' in basename.upper()
                            or 'COPYING' in basename.upper() or 'NOTICE' in basename.upper()
                            or 'licenses' in path.parts)
                        code = first in ('num2words', 'babel') and (
                            basename.endswith('.py') or name == 'babel/global.dat')
                        locale = name in {'babel/locale-data/' + item + '.dat' for item in ('root', *LANGUAGES)}
                        notice = first == 'babel' and ('LICENSE' in basename.upper() or 'COPYING' in basename.upper())
                        if not (metadata or code or locale or notice):
                            continue
                        count += 1
                        total += entry.file_size
                        if count > MAX_RUNTIME_FILES - 2 or entry.file_size > 8 * 1024 * 1024 or total > 32 * 1024 * 1024:
                            raise ValueError('Optional library selection exceeds its byte/file bound.')
                        raw = archive.read(entry)
                        if len(raw) != entry.file_size:
                            raise ValueError('Optional wheel changed during extraction.')
                        write(output / 'packages' / Path(*path.parts), raw)
        for required in ('num2words/__init__.py', 'babel/__init__.py', 'babel/global.dat',
                         'babel/locale-data/root.dat', 'babel/locale-data/en.dat',
                         'babel/locale-data/uk.dat', 'babel/locale-data/sk.dat'):
            if not (output / 'packages' / required).is_file():
                raise ValueError('The reviewed optional library pack is incomplete.')
        write(output / 'SOURCE.json', (json.dumps({'wheels': WHEELS, 'selection': list(LANGUAGES),
            'cli_selected': False, 'host_site_packages_fallback': False}, sort_keys=True) + '\n').encode('utf-8'))
        revision = sha256(json.dumps(WHEELS, sort_keys=True).encode('utf-8')).hexdigest()
        return seal(output, 'reading-preparation', 'num2words-0.5.14-babel-2.18.0-en-uk-sk',
                    revision, 'LGPL-2.1-or-later AND BSD-3-Clause; included Unicode notices',
                    dict(packages='packages', num2words_version='0.5.14', babel_version='2.18.0',
                         languages=list(LANGUAGES), source_wheels=WHEELS))
    except BaseException:
        shutil.rmtree(output)
        raise


def build_hunspell_fixture(dll, source_archive, output, *, reviewed=False):
    """A real native API fixture, deliberately not a production language dictionary."""
    if not reviewed:
        raise ValueError('Review the pinned source/library license and synthetic dictionary before use.')
    output = local_path(output, must_exist=False)
    output.mkdir(mode=0o700)
    try:
        with checked(source_archive, digest=SOURCE_SHA256, exact_size=SOURCE_BYTES) as stream:
            with tarfile.open(fileobj=stream, mode='r:gz') as archive:
                for name in ('COPYING', 'COPYING.LESSER', 'COPYING.MPL', 'AUTHORS', 'license.hunspell', 'license.myspell'):
                    member = archive.getmember('hunspell-1.7.6/' + name)
                    if not member.isfile() or not 0 < member.size < 128 * 1024:
                        raise ValueError('Pinned Hunspell source notices are invalid.')
                    incoming = archive.extractfile(member)
                    if incoming is None:
                        raise ValueError('Pinned Hunspell source notice is missing.')
                    with incoming:
                        raw = incoming.read(128 * 1024)
                    if len(raw) != member.size:
                        raise ValueError('Pinned Hunspell source notice is incomplete.')
                    write(output / 'licenses' / name, raw)
        with checked(dll, maximum=16 * 1024 * 1024) as stream:
            raw = stream.read()
        if len(raw) < 64 or raw[:2] != b'MZ':
            raise ValueError('The selected Hunspell library must be a Windows x64 DLL.')
        offset = struct.unpack_from('<I', raw, 60)[0]
        if not 64 <= offset <= len(raw) - 6 or raw[offset:offset + 6] != b'PE\0\0\x64\x86':
            raise ValueError('The selected Hunspell library must target Windows x64.')
        write(output / 'bin/hunspell-1.7.dll', raw)
        write(output / 'dictionary/fixture.aff', FIXTURE_AFF)
        write(output / 'dictionary/fixture.dic', FIXTURE_DIC)
        write(output / 'dictionary/NOTICE.txt',
              b'Public synthetic integration dictionary. CC0-1.0. No language-quality or real-corpus claim.\n'
              b'https://creativecommons.org/publicdomain/zero/1.0/\n')
        write(output / 'SOURCE.json', (json.dumps(dict(
            source_revision=HUNSPELL_REVISION, source_sha256=SOURCE_SHA256,
            source_bytes=SOURCE_BYTES, library_version=HUNSPELL_VERSION,
            fixture='public-synthetic-integration-only', real_corpus=False), sort_keys=True) + '\n').encode('utf-8'))
        dictionary_revision = sha256(b'aff\0' + FIXTURE_AFF + b'dic\0' + FIXTURE_DIC).hexdigest()
        return seal(output, 'hunspell', 'hunspell-1.7.6-public-synthetic-fixture', HUNSPELL_REVISION,
            'LGPL-2.1-or-later AND CC0-1.0', dict(dll='bin/hunspell-1.7.dll',
                aff='dictionary/fixture.aff', dic='dictionary/fixture.dic', language='en',
                library_version=HUNSPELL_VERSION, source_revision=HUNSPELL_REVISION,
                dictionary_id='public-synthetic-integration-fixture',
                dictionary_revision=dictionary_revision, dictionary_license='CC0-1.0'))
    except BaseException:
        shutil.rmtree(output)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--wheelhouse')
    parser.add_argument('--hunspell-dll')
    parser.add_argument('--hunspell-source')
    parser.add_argument('--reviewed-assets', action='store_true')
    args = parser.parse_args()
    if bool(args.wheelhouse) == bool(args.hunspell_dll) or bool(args.hunspell_dll) != bool(args.hunspell_source):
        parser.error('Choose wheelhouse OR both hunspell-dll and hunspell-source.')
    if args.wheelhouse:
        result = build_normalizer(args.wheelhouse, args.output, reviewed=args.reviewed_assets)
    else:
        result = build_hunspell_fixture(args.hunspell_dll, args.hunspell_source, args.output,
                                       reviewed=args.reviewed_assets)
    print(json.dumps({'manifest': str(result), 'sha256': sha256(result.read_bytes()).hexdigest()},
                     ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
