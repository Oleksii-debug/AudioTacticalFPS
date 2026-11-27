"""Installed entry point for the existing exact Windows audio preparer.

Only an explicit caller requests acquisition. The archive, every original file,
notices and source lock retain their reviewed identity. This prepares a local
installation; it supplies no transfer/distribution clearance.
"""
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile, ZIP_DEFLATED, ZIP_STORED

from . import runtime as audio_runtime
from .runtime import SOURCE_LOCK_SHA256
from .render import check_cancelled, run_process, _require_local_volume
from ..native_runtime import _directory, _plain as _checked_plain

SOURCE_LOCK = Path(__file__).with_name('runtime-source-lock.json')
NOTICE = Path(__file__).with_name('runtime-setup-README.txt')
MANIFEST = 'audio-runtime-manifest.json'
MAX_FILES = 10000
MAX_UNPACKED_BYTES = 1536 * 1024**2
_DEVICE = re.compile(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', re.I)


def _progress(callback, phase, completed=0, total=None):
    if callback is not None:
        callback(phase, completed, total)


def verify_native_versions(root, *, cancelled=None, progress=None):
    """Execute only fixed version commands after their source bytes were checked."""
    if sys.platform != 'win32':
        return {}
    lock = load_lock()
    versions = {}
    for index, name in enumerate(('ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe')):
        check_cancelled(cancelled)
        path = _plain(Path(root) / 'bin' / name)
        record = lock['original_files']['bin/' + name]
        if path.stat().st_size != record['size_bytes'] or digest(path, cancelled=cancelled) != record['sha256']:
            raise ValueError('Native audio executable differs from the reviewed publisher bytes.')
        _progress(progress, 'Checking actual Windows executable versions', index, 3)
        out, _ = run_process([str(path), '-version'], cancelled=cancelled,
                             timeout_seconds=30, maximum_log_bytes=65536)
        if 'version ' + lock['release'] not in out:
            raise ValueError('Native audio executable version differs from the source lock.')
        versions[name] = out
    _progress(progress, 'Checked actual Windows executable versions', 3, 3)
    return versions


def digest(path, *, cancelled=None):
    result = sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            check_cancelled(cancelled)
            result.update(block)
    return result.hexdigest()


def load_lock():
    if SOURCE_LOCK.is_symlink() or digest(SOURCE_LOCK) != SOURCE_LOCK_SHA256:
        raise ValueError('The reviewed audio runtime source lock changed.')
    return json.loads(SOURCE_LOCK.read_text(encoding='utf-8'))


def _plain(path, *, directory=False):
    path = Path(path).absolute()
    _require_local_volume(path)
    if str(path).startswith(('\\\\', '//')) or '..' in path.parts:
        raise ValueError('Audio setup requires an ordinary absolute local path.')
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
        if (parsed.scheme != 'https' or parsed.username or parsed.password
                or parsed.port not in (None, 443)) or not (
                host == 'github.com' or host.endswith('.githubusercontent.com')):
            raise ValueError('Runtime download redirected to an unapproved destination.')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def download_archive(destination, *, cancelled=None, progress=None):
    check_cancelled(cancelled)
    lock = load_lock()
    wanted = lock['archive']
    destination = Path(destination).absolute()
    _plain(destination.parent, directory=True)
    if os.path.lexists(destination):
        raise FileExistsError('Choose a new runtime download path.')
    request = Request(wanted['url'], headers={'User-Agent': 'UniversalResearchLibrary-ExplicitRuntimeSetup/1'})
    result, size = sha256(), 0
    with build_opener(_ApprovedRedirects()).open(request, timeout=10) as response:
        with Path(destination).open('xb') as stream:
            while True:
                check_cancelled(cancelled)
                block = response.read(1024 * 1024)
                if not block:
                    break
                check_cancelled(cancelled)
                size += len(block)
                if size > wanted['size_bytes']:
                    raise ValueError('Audio runtime download exceeded its pinned size.')
                result.update(block)
                stream.write(block)
                _progress(progress, 'Downloading the exact reviewed archive', size, wanted['size_bytes'])
            stream.flush()
            os.fsync(stream.fileno())
    check_cancelled(cancelled)
    if size != wanted['size_bytes'] or result.hexdigest() != wanted['sha256']:
        raise ValueError('Audio runtime download checksum or size does not match the publisher pin.')
    return Path(destination)


def verify_runtime(directory, expected_manifest_sha256=None, *, cancelled=None, progress=None):
    _plain(Path(directory).absolute(), directory=True)
    return audio_runtime.verify_audio_runtime(Path(directory).absolute(), expected_manifest_sha256,
                                cancelled=cancelled, progress=progress)


def prepare(archive, output, *, cancelled=None, progress=None):
    """Prepare one complete locked runtime; never select or transfer it."""
    check_cancelled(cancelled)
    lock = load_lock()
    archive = _plain(archive)
    target = Path(output).absolute()
    _plain(target.parent, directory=True)
    if os.path.lexists(target):
        raise FileExistsError('Choose a new audio runtime output directory.')
    wanted = lock['archive']
    _progress(progress, 'Checking the archive identity', 0, wanted['size_bytes'])
    if archive.stat().st_size != wanted['size_bytes'] or digest(archive, cancelled=cancelled) != wanted['sha256']:
        raise ValueError('Audio archive differs from the exact reviewed publisher bytes.')
    with tempfile.TemporaryDirectory(prefix='.carr-audio-runtime-', dir=target.parent) as temporary:
        stage = Path(temporary) / 'runtime'
        stage.mkdir()
        with ZipFile(archive) as zipped:
            entries = _members(zipped, lock)
            extracted = 0
            expanded = sum(member.file_size for member, _ in entries)
            for member, relative in entries:
                check_cancelled(cancelled)
                destination = stage / relative
                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                size = 0
                with zipped.open(member, 'r') as src, destination.open('xb') as dst:
                    for block in iter(lambda: src.read(1024 * 1024), b''):
                        check_cancelled(cancelled)
                        size += len(block)
                        if size > member.file_size:
                            raise ValueError('Extracted audio runtime entry exceeds its declared size.')
                        dst.write(block)
                        extracted += len(block)
                        _progress(progress, 'Extracting reviewed local runtime files', extracted, expanded)
                    dst.flush()
                    os.fsync(dst.fileno())
                if size != member.file_size:
                    raise ValueError('Truncated audio runtime entry.')
        for name in ('ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe'):
            _pe_x64(stage / 'bin' / name)
        provenance = stage / 'carr-provenance'
        provenance.mkdir()
        shutil.copyfile(SOURCE_LOCK, provenance/'source-lock.json')
        shutil.copyfile(NOTICE, provenance/'README.txt')
        execution = 'NOT_RUN'
        if sys.platform == 'win32':
            versions = verify_native_versions(stage, cancelled=cancelled, progress=progress)
            (provenance/'native-version-report.json').write_text(json.dumps(versions, indent=2)+'\n', encoding='utf-8')
            execution = 'PASS: exact pinned native executables started on Windows'
        files = {p.relative_to(stage).as_posix(): {'size_bytes': p.stat().st_size, 'sha256': digest(p, cancelled=cancelled)}
                 for p in stage.rglob('*') if p.is_file()}
        manifest = dict(version=1, target='Windows x64', use_scope='private-local-installation',
                        source_lock_sha256=SOURCE_LOCK_SHA256, windows_execution=execution, files=files)
        (stage/MANIFEST).write_text(json.dumps(manifest, sort_keys=True, indent=2)+'\n', encoding='utf-8')
        for file in stage.rglob('*'):
            if file.is_file():
                with file.open('r+b') as stream:
                    os.fsync(stream.fileno())
        verify_runtime(stage, cancelled=cancelled, progress=progress)
        check_cancelled(cancelled)
        # os.rename must not replace even an empty concurrent output directory.
        # Reserve it exclusively, then publish manifest last after its files.
        target.mkdir(mode=0o700)
        try:
            for item in stage.rglob('*'):
                check_cancelled(cancelled)
                relative = item.relative_to(stage)
                if relative.as_posix() == MANIFEST:
                    continue
                destination = target / relative
                if item.is_dir():
                    destination.mkdir(exist_ok=True)
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.link(item, destination)
            check_cancelled(cancelled)
            os.link(stage/MANIFEST, target/MANIFEST)
        except BaseException:
            # Remove only the new directory this operation reserved. A caller's
            # pre-existing target was rejected before this block.
            shutil.rmtree(target)
            raise
    return verify_runtime(target, cancelled=cancelled, progress=progress)

