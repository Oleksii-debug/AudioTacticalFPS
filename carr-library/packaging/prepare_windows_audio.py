"""Prepare a pinned private local Windows FFmpeg runtime; never execute it.

Default operation is offline. --download is an explicit build/setup operation
and accepts only the exact publisher archive in the reviewed lock. No package
manager, installer, PowerShell script, or automatic model download is run.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
from tempfile import TemporaryDirectory
import urllib.parse
import urllib.request
import zipfile


MAX_LOCK = 1024 * 1024
MAX_ARCHIVE = 512 * 1024**2
MAX_UNPACKED = 768 * 1024**2
MAX_ENTRIES = 4096
DOWNLOAD_HOSTS = frozenset(('github.com', 'release-assets.githubusercontent.com'))
REQUIRED_FILES = frozenset(('bin/ffmpeg.exe', 'bin/ffprobe.exe', 'LICENSE', 'README.txt'))


def regular(path, *, directory=False):
    info = Path(path).lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise ValueError('Audio packaging requires regular local files and directories without links.')
    return info


def local(path, *, exists=False):
    value = str(path)
    if value.startswith(('//', '\\\\')) or '\x00' in value or '://' in value:
        raise ValueError('Choose a local packaging path.')
    path = Path(path).absolute()
    for parent in reversed((path, *path.parents)):
        if os.path.lexists(parent):
            regular(parent, directory=parent != path or parent.is_dir())
    return path.resolve(strict=exists)


def relative(value):
    if not isinstance(value, str) or not value or any(c in value for c in ('\\', ':', '\x00')):
        raise ValueError('Invalid relative path in audio lock or ZIP.')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ('', '.', '..') for part in value.split('/')):
        raise ValueError('Unsafe relative path in audio lock or ZIP.')
    return path


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while block := stream.read(1024**2):
            result.update(block)
    return result.hexdigest()


def _unique(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('Duplicate audio lock property.')
        result[name] = value
    return result


def load_lock(path):
    path = local(path, exists=True)
    if regular(path).st_size > MAX_LOCK:
        raise ValueError('Audio runtime lock is too large.')
    raw = path.read_bytes()
    lock = json.loads(raw, object_pairs_hook=_unique)
    if (not isinstance(lock, dict) or lock.get('schema') != 1 or lock.get('target') != 'win_amd64'
            or lock.get('engine') != 'FFmpeg' or set(lock.get('output_files', ())) != REQUIRED_FILES):
        raise ValueError('Unsupported audio runtime lock.')
    artifact = lock.get('artifact', {})
    relative(artifact.get('filename'))
    if '/' in artifact['filename'] or artifact.get('format') != 'zip':
        raise ValueError('Expected a simple ZIP archive filename.')
    _record(artifact, MAX_ARCHIVE)
    _url(artifact.get('url'))
    expected_url = 'https://github.com/GyanD/codexffmpeg/releases/download/'
    if not artifact['url'].startswith(expected_url):
        raise ValueError('Audio archive must use the reviewed publisher release endpoint.')
    total = 0
    members = set()
    for name, entry in lock['output_files'].items():
        relative(name)
        relative(entry.get('member'))
        _record(entry, MAX_UNPACKED)
        if entry['member'].casefold() in members:
            raise ValueError('Duplicate selected ZIP member.')
        members.add(entry['member'].casefold())
        total += entry['size']
    if total > MAX_UNPACKED:
        raise ValueError('Audio payload exceeds unpacked budget.')
    deployment = lock.get('deployment', {})
    if (deployment.get('download_default') is not False
            or deployment.get('public_binary_distribution') is not False):
        raise ValueError('This helper only prepares an explicitly enabled private local runtime.')
    return lock, hashlib.sha256(raw).hexdigest()


def _record(record, maximum):
    if (not isinstance(record, dict) or type(record.get('size')) is not int
            or not 0 < record['size'] <= maximum
            or not isinstance(record.get('sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', record['sha256'])):
        raise ValueError('Invalid size or SHA256 in audio runtime lock.')


def _url(value):
    if not isinstance(value, str):
        raise ValueError('Expected the pinned HTTPS audio archive URL.')
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in DOWNLOAD_HOSTS
            or parsed.username or parsed.password or parsed.fragment
            or parsed.port not in (None, 443)):
        raise ValueError('Audio downloads require the approved HTTPS publisher or release-asset host.')
    return value


class ApprovedRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def validate_artifact(path, record):
    if regular(path).st_size != record['size'] or digest(path) != record['sha256']:
        raise ValueError('Audio archive differs from its reviewed size or SHA256.')


def cached_artifact(cache, record, *, download=False):
    cache = local(cache)
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    local(cache, exists=True)
    destination = cache / str(relative(record['filename']))
    if os.path.lexists(destination):
        validate_artifact(destination, record)
        return destination
    if not download:
        raise FileNotFoundError('Missing offline audio archive: ' + str(destination))
    opener = urllib.request.build_opener(ApprovedRedirects())
    with TemporaryDirectory(prefix='.audio-download-', dir=cache) as temporary:
        staged = Path(temporary) / 'archive.zip'
        count = 0
        request = urllib.request.Request(_url(record['url']), headers={'User-Agent': 'Universal-Research-Library-runtime-setup/1'})
        with opener.open(request, timeout=60) as response, staged.open('xb') as stream:
            _url(response.url)
            while block := response.read(1024**2):
                count += len(block)
                if count > record['size'] or count > MAX_ARCHIVE:
                    raise ValueError('Audio archive download exceeds its locked byte count.')
                stream.write(block)
            stream.flush()
            os.fsync(stream.fileno())
        validate_artifact(staged, record)
        try:
            os.link(staged, destination)
        except FileExistsError:
            validate_artifact(destination, record)
    validate_artifact(destination, record)
    return destination


def _pe64(path):
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise ValueError('Locked audio binary is not a PE executable.')
        offset, = struct.unpack_from('<I', header, 0x3C)
        if offset > 16 * 1024**2:
            raise ValueError('Invalid PE header offset.')
        stream.seek(offset)
        pe = stream.read(26)
        if len(pe) != 26 or pe[:4] != b'PE\x00\x00' or struct.unpack_from('<H', pe, 4)[0] != 0x8664:
            raise ValueError('Audio binary is not the reviewed Windows AMD64 architecture.')
        if struct.unpack_from('<H', pe, 24)[0] != 0x20B:
            raise ValueError('Audio binary must use a PE32+ optional header.')


def _publish_directory(staged, destination):
    """No-replace directory publication on the two supported build platforms."""
    if os.name == 'nt':
        os.rename(staged, destination)  # Windows rename refuses an existing target.
    elif sys.platform.startswith('linux'):
        library = ctypes.CDLL(None, use_errno=True)
        function = getattr(library, 'renameat2', None)
        if function is None:
            raise RuntimeError('This host lacks atomic no-replace directory publication.')
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(-100, os.fsencode(staged), -100, os.fsencode(destination), 1) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(destination))
    else:
        raise RuntimeError('Safe runtime preparation is supported on Windows and Linux build hosts.')


def prepare(lock_path, cache, output, *, download=False):
    lock, lock_hash = load_lock(lock_path)
    output = local(output)
    if os.path.lexists(output):
        raise FileExistsError('Audio runtime output must be a new directory.')
    archive_path = cached_artifact(cache, lock['artifact'], download=download)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    local(output.parent, exists=True)
    with TemporaryDirectory(prefix='.audio-runtime-', dir=output.parent) as temporary:
        staged = Path(temporary) / 'runtime'
        staged.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ENTRIES:
                raise ValueError('Audio ZIP exceeds its member count allowance.')
            seen, selected = set(), {}
            total = 0
            for member in members:
                # ZipInfo normalizes backslashes on Windows and truncates NUL.
                # Validate the original archive spelling before either can hide
                # a rejected member from the complete-inventory check.
                raw_name = member.orig_filename
                if raw_name != member.filename:
                    raise ValueError('Unsafe relative path in audio ZIP.')
                name = raw_name.rstrip('/') if member.is_dir() else raw_name
                relative(name)
                if name.casefold() in seen:
                    raise ValueError('Audio ZIP has duplicate or case-colliding paths.')
                seen.add(name.casefold())
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise ValueError('Audio ZIP contains a link or special file.')
                if member.flag_bits & 1:
                    raise ValueError('Encrypted ZIP entries are not supported.')
                if member.file_size > MAX_UNPACKED:
                    raise ValueError('Oversized audio ZIP member.')
                total += member.file_size
                if total > MAX_UNPACKED:
                    raise ValueError('Audio ZIP exceeds its total unpacked allowance.')
                selected[member.filename] = member
            for name, entry in lock['output_files'].items():
                member = selected.get(entry['member'])
                if member is None or member.is_dir() or member.file_size != entry['size']:
                    raise ValueError('Audio ZIP is missing a locked regular member.')
                destination = staged.joinpath(*relative(name).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                checksum, count = hashlib.sha256(), 0
                with archive.open(member) as source, destination.open('xb') as sink:
                    while block := source.read(1024**2):
                        count += len(block)
                        if count > entry['size']:
                            raise ValueError('Audio ZIP entry exceeds its locked size.')
                        checksum.update(block)
                        sink.write(block)
                    sink.flush()
                    os.fsync(sink.fileno())
                if count != entry['size'] or checksum.hexdigest() != entry['sha256']:
                    raise ValueError('Extracted audio member differs from its locked SHA256.')
                if name.endswith('.exe'):
                    _pe64(destination)
        manifest = {'schema': 1, 'target': lock['target'], 'engine_version': lock['engine_version'],
                    'lock_sha256': lock_hash, 'artifact': lock['artifact'], 'source': lock['source'],
                    'license': lock['license'], 'deployment': lock['deployment'],
                    'files': {name: {'size': entry['size'], 'sha256': entry['sha256']}
                              for name, entry in lock['output_files'].items()},
                    'execution_test': 'NOT_RUN by extraction helper'}
        record = staged / 'runtime-manifest.json'
        with record.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        local(output.parent, exists=True)
        _publish_directory(staged, output)
    return {'status': 'VERIFIED_AND_PREPARED', 'output': str(output), 'bin_dir': str(output / 'bin'),
            'lock_sha256': lock_hash, 'files': len(lock['output_files']),
            'execution_test': 'NOT_RUN', 'public_binary_distribution': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lock', type=Path, default=Path(__file__).with_name('windows-audio.lock.json'))
    parser.add_argument('--cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--download', action='store_true', help='Explicitly acquire the locked publisher ZIP over HTTPS')
    args = parser.parse_args(argv)
    try:
        print(json.dumps(prepare(args.lock, args.cache, args.output, download=args.download), sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
        print('Audio runtime preparation failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
