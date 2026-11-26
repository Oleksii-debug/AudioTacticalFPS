"""Explicit, hash-locked private Windows FFmpeg setup; no automatic downloads."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED


HERE = Path(__file__).resolve().parent
SOURCE_LOCK = HERE / 'audio-runtime' / 'source-lock.json'
sys.path.insert(0, str(HERE.parent / 'src'))
from carr_library.audio.runtime import SOURCE_LOCK_SHA256, verify_audio_runtime
from carr_library.native_runtime import _directory, _plain as _checked_plain
MANIFEST = 'audio-runtime-manifest.json'
MAX_FILES = 10000
MAX_UNPACKED_BYTES = 1536 * 1024**2
_DEVICE = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)


def digest(path):
    result = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def load_lock():
    if SOURCE_LOCK.is_symlink() or digest(SOURCE_LOCK) != SOURCE_LOCK_SHA256:
        raise ValueError('The reviewed audio runtime source lock changed.')
    return json.loads(SOURCE_LOCK.read_text(encoding='utf-8'))


def _plain(path, *, directory=False):
    path = Path(path).absolute()
    _directory(path if directory else path.parent)
    _checked_plain(path, directory=directory)
    return path


def _relative(value):
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value or value.startswith('/'):
        raise ValueError('Invalid archive path.')
    path = PurePosixPath(value)
    pieces = value.rstrip('/').split('/')
    if len(pieces) > 16 or any(part in ('', '.', '..') or ':' in part or _DEVICE.match(part)
                               or part.endswith((' ', '.')) or any(ord(c) < 32 for c in part)
                               for part in pieces):
        raise ValueError('Unsafe archive path.')
    if path.is_absolute():
        raise ValueError('Absolute archive paths are forbidden.')
    return path


def _members(archive, lock):
    entries, names, total = [], set(), 0
    expected_root = lock['package_root']
    for member in archive.infolist():
        if len(entries) >= MAX_FILES:
            raise ValueError('Audio runtime archive has too many files.')
        path = _relative(member.filename)
        if path.parts[0] != expected_root:
            raise ValueError('Unexpected runtime archive root.')
        normalized = unicodedata.normalize('NFC', member.filename.rstrip('/')).casefold()
        if normalized in names:
            raise ValueError('Duplicate or case-colliding archive entry.')
        names.add(normalized)
        mode = member.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or (member.external_attr & 0x400):
            raise ValueError('Runtime archive links and special files are forbidden.')
        if member.flag_bits & 1 or member.compress_type not in (ZIP_STORED, ZIP_DEFLATED):
            raise ValueError('Unsupported encrypted/compressed runtime archive entry.')
        if member.file_size < 0 or member.file_size > 1024**3:
            raise ValueError('Runtime archive member exceeds its size bound.')
        total += member.file_size
        if total > MAX_UNPACKED_BYTES:
            raise ValueError('Runtime archive exceeds its expanded size bound.')
        if member.is_dir():
            if member.file_size:
                raise ValueError('Directory entry contains unexpected data.')
        elif len(path.parts) == 1:
            raise ValueError('Runtime root must be a directory.')
        entries.append((member, Path(*path.parts[1:])))
    files = {relative.as_posix() for member, relative in entries if not member.is_dir()}
    if not {'bin/ffmpeg.exe', 'bin/ffprobe.exe', 'bin/ffplay.exe'}.issubset(files):
        raise ValueError('Required native audio executables are missing.')
    if not any(name.casefold() in ('license', 'license.txt') for name in files):
        raise ValueError('Publisher license must be preserved with the audio runtime.')
    return entries


def _pe_x64(path):
    with path.open('rb') as stream:
        initial = stream.read(64)
        if len(initial) != 64 or initial[:2] != b'MZ':
            raise ValueError('Expected a Windows PE executable.')
        offset = struct.unpack_from('<I', initial, 60)[0]
        if not 64 <= offset <= 1024 * 1024:
            raise ValueError('Invalid Windows executable header.')
        stream.seek(offset)
        header = stream.read(6)
        if header[:4] != b'PE\x00\x00' or len(header) != 6 or struct.unpack_from('<H', header, 4)[0] != 0x8664:
            raise ValueError('Audio runtime must contain Windows x64 executables.')


class _ApprovedRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urlsplit(newurl)
        host = (parsed.hostname or '').lower()
        if parsed.scheme != 'https' or parsed.username or parsed.password or not (
                host == 'github.com' or host.endswith('.githubusercontent.com')):
            raise ValueError('Runtime download redirected to an unapproved destination.')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download_archive(destination):
    lock = load_lock()
    wanted = lock['archive']
    request = Request(wanted['url'], headers={'User-Agent': 'UniversalResearchLibrary-ExplicitRuntimeSetup/1'})
    result, size = sha256(), 0
    with build_opener(_ApprovedRedirects()).open(request, timeout=60) as response:
        with Path(destination).open('xb') as stream:
            for block in iter(lambda: response.read(1024 * 1024), b''):
                size += len(block)
                if size > wanted['size_bytes']:
                    raise ValueError('Audio runtime download exceeded its pinned size.')
                result.update(block)
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
    if size != wanted['size_bytes'] or result.hexdigest() != wanted['sha256']:
        raise ValueError('Audio runtime download checksum or size does not match the publisher pin.')
    return Path(destination)


def verify_runtime(directory, expected_manifest_sha256=None):
    return verify_audio_runtime(Path(directory).absolute(), expected_manifest_sha256)


def prepare(archive, output):
    lock = load_lock()
    archive = _plain(archive)
    target = Path(output).absolute()
    _plain(target.parent, directory=True)
    if os.path.lexists(target):
        raise FileExistsError('Choose a new audio runtime output directory.')
    wanted = lock['archive']
    if archive.stat().st_size != wanted['size_bytes'] or digest(archive) != wanted['sha256']:
        raise ValueError('Audio archive differs from the exact reviewed publisher bytes.')
    with tempfile.TemporaryDirectory(prefix='.carr-audio-runtime-', dir=target.parent) as temporary:
        stage = Path(temporary) / 'runtime'
        stage.mkdir()
        with ZipFile(archive) as zipped:
            entries = _members(zipped, lock)
            for member, relative in entries:
                destination = stage / relative
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with zipped.open(member, 'r') as src, destination.open('xb') as dst:
                    for block in iter(lambda: src.read(1024 * 1024), b''):
                        size += len(block)
                        if size > member.file_size:
                            raise ValueError('Extracted audio runtime entry exceeds its declared size.')
                        dst.write(block)
                    dst.flush()
                    os.fsync(dst.fileno())
                if size != member.file_size:
                    raise ValueError('Truncated audio runtime entry.')
        for name in ('ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe'):
            _pe_x64(stage / 'bin' / name)
        provenance = stage / 'carr-provenance'
        provenance.mkdir()
        shutil.copyfile(SOURCE_LOCK, provenance/'source-lock.json')
        shutil.copyfile(HERE/'audio-runtime'/'README.txt', provenance/'README.txt')
        execution = 'NOT_RUN'
        if sys.platform == 'win32':
            versions = {}
            for name in ('ffmpeg.exe', 'ffprobe.exe'):
                completed = subprocess.run([str(stage/'bin'/name), '-version'], check=True,
                                           capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
                text = completed.stdout.decode('utf-8', errors='replace')
                if 'version ' + lock['release'] not in text or len(text) > 65536:
                    raise ValueError('Native audio executable version differs from the source lock.')
                versions[name] = text
            (provenance/'native-version-report.json').write_text(json.dumps(versions, indent=2)+'\n', encoding='utf-8')
            execution = 'PASS: exact pinned native executables started on Windows'
        files = {p.relative_to(stage).as_posix(): {'size_bytes': p.stat().st_size, 'sha256': digest(p)}
                 for p in stage.rglob('*') if p.is_file()}
        manifest = dict(version=1, target='Windows x64', use_scope='private-local-installation',
                        source_lock_sha256=SOURCE_LOCK_SHA256, windows_execution=execution, files=files)
        (stage/MANIFEST).write_text(json.dumps(manifest, sort_keys=True, indent=2)+'\n', encoding='utf-8')
        for file in stage.rglob('*'):
            if file.is_file():
                with file.open('r+b') as stream:
                    os.fsync(stream.fileno())
        verify_runtime(stage)
        # os.rename must not replace even an empty concurrent output directory.
        # Reserve it exclusively, then publish manifest last after its files.
        target.mkdir(mode=0o700)
        for item in stage.rglob('*'):
            relative = item.relative_to(stage)
            if relative.as_posix() == MANIFEST:
                continue
            destination = target / relative
            if item.is_dir():
                destination.mkdir(exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.link(item, destination)
        os.link(stage/MANIFEST, target/MANIFEST)
    return verify_runtime(target)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--archive', type=Path, help='Already downloaded exact full-build ZIP; no network access')
    source.add_argument('--download', action='store_true', help='Explicitly download only the exact pinned public runtime')
    parser.add_argument('--output', type=Path, required=True, help='New private runtime directory')
    args = parser.parse_args(argv)
    if args.download:
        with tempfile.TemporaryDirectory(prefix='carr-audio-download-') as temp:
            archive = download_archive(Path(temp)/'ffmpeg.zip')
            result = prepare(archive, args.output)
    else:
        result = prepare(args.archive, args.output)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError) as error:
        print(f'Audio runtime was not prepared: {error}', file=sys.stderr)
        raise SystemExit(1)
