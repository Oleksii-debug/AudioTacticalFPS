"""Explicit local setup for the existing pinned Windows FFmpeg FULL runtime.

Adapted from the canonical source-only setup flow. A plan never downloads or
prepares a runtime. --interactive asks for YES; --download is explicit consent.
The runtime stays outside the installation and no engine setting is changed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import ssl
import struct
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from uuid import uuid4

from carr_library.audio import runtime
from carr_library.audio.jobs import checked_directory, publish_noreplace, verified_reader
from carr_library.audio.model_manifest import local_path
from carr_library.installation_lease import installed_entrypoint

HERE = Path(__file__).absolute().parent
MAX_LOCK_BYTES = 64 * 1024
RESERVE_BYTES = 64 * 1024 * 1024
DOWNLOAD_SECONDS = 600
DOWNLOAD_HOSTS = frozenset(('github.com', 'release-assets.githubusercontent.com',
                            'objects.githubusercontent.com'))


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('The audio source lock contains a duplicate field.')
        result[key] = value
    return result


def _installation_root():
    if HERE.name == 'audio-setup' and HERE.parent.name == 'tools':
        return local_path(HERE.parent.parent)
    return local_path(HERE.parent)


def inputs():
    """Check the existing lock against the current runtime's compiled pins."""
    path = HERE / 'audio-runtime' / 'source-lock.json'
    with verified_reader(path) as stream:
        size = os.fstat(stream.fileno()).st_size
        if not 0 < size <= MAX_LOCK_BYTES:
            raise ValueError('The audio source lock exceeds its byte budget.')
        raw = stream.read(MAX_LOCK_BYTES + 1)
        if len(raw) != size or len(raw) > MAX_LOCK_BYTES:
            raise ValueError('The audio source lock changed beyond its byte budget.')
    value = json.loads(raw, object_pairs_hook=_unique)
    expected = {'url': runtime.FFMPEG_ARCHIVE_URL,
                'size_bytes': runtime.FFMPEG_ARCHIVE_BYTES,
                'sha256': runtime.FFMPEG_ARCHIVE_SHA256}
    if (type(value) is not dict or type(value.get('version')) is not int or value['version'] != 1
            or value.get('archive') != expected
            or value.get('upstream_source', {}).get('commit') != runtime.FFMPEG_SOURCE_COMMIT):
        raise ValueError('The source lock differs from the existing reviewed FULL runtime.')
    prefix = value.get('package_root')
    expected_files = {}
    for relative, size, checksum in runtime.ARCHIVE_MEMBERS.values():
        parts = PurePosixPath(relative).parts
        if not parts or parts[0] != prefix:
            raise ValueError('The source lock has a different archive layout.')
        expected_files[PurePosixPath(*parts[1:]).as_posix()] = {
            'size_bytes': size, 'sha256': checksum}
    if value.get('original_files') != expected_files:
        raise ValueError('The source lock does not retain the complete original inventory.')
    return {'name': PurePosixPath(urllib.parse.urlsplit(expected['url']).path).name,
            'url': expected['url'], 'bytes': expected['size_bytes'],
            'sha256': expected['sha256'], 'source_lock_sha256': hashlib.sha256(raw).hexdigest()}


def _load_prepare():
    path = local_path(HERE / 'prepare_ffmpeg_runtime.py')
    runtime.regular_file(path)
    if path.stat().st_size > 128 * 1024:
        raise ValueError('The fixed audio preparer exceeds its source byte budget.')
    spec = importlib.util.spec_from_file_location('carr_local_full_audio_prepare', path)
    if spec is None or spec.loader is None:
        raise RuntimeError('The existing audio preparer cannot be loaded.')
    module = importlib.util.module_from_spec(spec)
    previous_path, previous_bytecode = list(sys.path), sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous_path
        sys.dont_write_bytecode = previous_bytecode
    return module


def _default_output():
    base = os.environ.get('LOCALAPPDATA')
    if not base:
        raise ValueError('LOCALAPPDATA is unavailable. Choose a new folder with --output.')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')
    return Path(base) / 'Resonance' / 'runtimes' / ('ffmpeg-full-' + stamp + '-' + uuid4().hex[:8])


def _external_output(value):
    candidate = Path(value)
    if not candidate.is_absolute() or '..' in candidate.parts:
        raise ValueError('Choose an absolute local output path without parent traversal.')
    output = local_path(candidate, must_exist=False)
    installed = _installation_root()
    if output.is_relative_to(installed) or installed.is_relative_to(output):
        raise ValueError('The audio runtime must stay outside the application installation.')
    if os.path.lexists(output):
        raise FileExistsError('Choose a new output folder; existing data is never overwritten.')
    existing = output.parent
    while not existing.exists():
        existing = existing.parent
    checked_directory(existing)
    return output


def _remote_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or parsed.hostname not in DOWNLOAD_HOSTS
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError('The fixed audio download redirected outside its reviewed HTTPS hosts.')
    return value


class _Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 3
    max_repeats = 2

    def redirect_request(self, request, fp, code, message, headers, newurl):
        _remote_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def _opener():
    # Do not load ambient proxy credentials or SSLKEYLOGFILE for this fixed
    # public archive. The normal certificate and hostname checks remain enabled.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_default_certs()
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _Redirects(),
        urllib.request.HTTPSHandler(context=context))


def _archive_identity(path, record):
    path = local_path(path)
    runtime.regular_file(path)
    if path.stat().st_size != record['bytes'] or runtime.file_sha256(path) != record['sha256']:
        raise ValueError('The archive differs from the pinned FULL runtime.')
    return path


def _download(record, directory):
    """Publish one size/hash-pinned archive without replacing any existing file."""
    directory = checked_directory(directory)
    if record != inputs():
        raise ValueError('The reviewed audio download plan changed.')
    destination = directory / record['name']
    if os.path.lexists(destination):
        raise FileExistsError('The archive destination already exists.')
    _remote_url(record['url'])
    request = urllib.request.Request(record['url'],
        headers={'User-Agent': 'CARR-explicit-full-audio-setup/1', 'Accept-Encoding': 'identity'})
    descriptor, name = tempfile.mkstemp(prefix='.ffmpeg-', suffix='.partial', dir=directory)
    stage, owned = Path(name), os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, 'wb') as output:
            checksum, count = hashlib.sha256(), 0
            deadline = time.monotonic() + DOWNLOAD_SECONDS
            with _opener().open(request, timeout=15) as response:
                _remote_url(response.geturl())
                if getattr(response, 'status', 200) != 200:
                    raise ValueError('The audio archive server did not return a complete response.')
                while True:
                    if time.monotonic() >= deadline:
                        raise TimeoutError('The explicit audio download exceeded its time budget.')
                    # HTTPResponse.read may aggregate arbitrarily many slow
                    # socket reads. read1 returns after one buffered read, so
                    # the global deadline is revisited within the 15 s socket
                    # timeout, including a peer that sends bytes very slowly.
                    block = response.read1(min(1024 * 1024, record['bytes'] - count + 1))
                    if not block:
                        break
                    count += len(block)
                    if count > record['bytes']:
                        raise ValueError('The audio download exceeded its pinned byte count.')
                    if shutil.disk_usage(directory).free < len(block) + RESERVE_BYTES:
                        raise OSError('The audio download stopped to preserve the free disk reserve.')
                    checksum.update(block)
                    output.write(block)
                if time.monotonic() >= deadline:
                    raise TimeoutError('The explicit audio download exceeded its time budget.')
            if count != record['bytes'] or checksum.hexdigest() != record['sha256']:
                raise ValueError('The audio download failed its pinned size or SHA-256 check.')
            output.flush()
            os.fsync(output.fileno())
        identity = stage.lstat()
        if (identity.st_dev, identity.st_ino) != (owned.st_dev, owned.st_ino):
            raise ValueError('The owned audio download stage was replaced.')
        _archive_identity(stage, record)
        publish_noreplace(stage, destination)
        _archive_identity(destination, record)
        return destination
    finally:
        if os.path.lexists(stage):
            current = stage.lstat()
            if (current.st_dev, current.st_ino) == (owned.st_dev, owned.st_ino):
                stage.unlink()


def _require_windows():
    if sys.platform != 'win32' or struct.calcsize('P') != 8:
        raise ValueError('This reviewed local runtime is for Windows x64.')


def _prepare(output, record, *, archive=None):
    _require_windows()
    output = _external_output(output)
    if record != inputs():
        raise ValueError('The audio plan changed after confirmation.')
    selected = _archive_identity(archive, record) if archive is not None else None
    preparer = _load_prepare()
    output.parent.mkdir(parents=True, exist_ok=True)
    parent = checked_directory(output.parent)
    needed = 2 * sum(row[1] for row in runtime.ARCHIVE_MEMBERS.values()) + RESERVE_BYTES
    if selected is None:
        needed += record['bytes']
    if shutil.disk_usage(parent).free < needed:
        raise OSError('Not enough free space for the archive, independent preparation and retained runtime.')
    output.mkdir(mode=0o700, exist_ok=False)
    checked_directory(output)
    if selected is None:
        print('Downloading the reviewed public FULL archive...', flush=True)
        selected = _download(record, output)
    print('Verifying and preparing the existing local runtime...', flush=True)
    target = output / 'runtime'
    # No binary is executed by setup, and no library or engine registration is
    # changed. The existing preparer retains all pinned licenses and documents.
    preparer.prepare(selected, target, verify_native=False)
    verified = runtime.verify_runtime(target)
    return {'runtime': str(target), 'ffmpeg': str(target / 'ffmpeg.exe'),
            'ffprobe': str(target / 'ffprobe.exe'), 'archive_sha256': record['sha256'],
            'source_lock_sha256': record['source_lock_sha256'],
            'files': len(verified['files']), 'native_execution': 'NOT_RUN',
            'registration_changed': False}


def _show_plan(record, output, archive):
    print('Resonance: local FFmpeg FULL setup')
    print('Version: ' + runtime.FFMPEG_VERSION + '; archive SHA-256: ' + record['sha256'])
    print('New external runtime folder: ' + str(output))
    if archive is None:
        print('Explicit download: ' + record['url'])
        print('Download size: ' + str(record['bytes']) + ' bytes.')
    else:
        print('Existing local archive: ' + str(archive) + '; no download.')
    print('Original licenses and documents remain with the local runtime.')
    print('No documents, voices, credentials or library settings are sent or registered.')
    print('This local preparation does not authorize redistribution of the audio runtime.')


@installed_entrypoint
def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New folder outside the application installation.')
    parser.add_argument('--archive', type=Path, help='Existing exact FULL archive; interactive preparation uses no network.')
    consent = parser.add_mutually_exclusive_group()
    consent.add_argument('--interactive', action='store_true', help='Require YES before download or preparation.')
    consent.add_argument('--download', action='store_true', help='Explicitly download and prepare the reviewed archive.')
    args = parser.parse_args(argv)
    try:
        if args.archive is not None and args.download:
            raise ValueError('Use --interactive with an existing archive; --download selects the reviewed remote archive.')
        record = inputs()
        output = _external_output(args.output if args.output is not None else _default_output())
        archive = local_path(args.archive) if args.archive is not None else None
        _show_plan(record, output, archive)
        if not args.download:
            if not args.interactive:
                print('Plan only: no archive is downloaded and no runtime folder is created.')
                print('Use --interactive to confirm this plan or --download for explicit unattended setup.')
                return 0
            if input('Type YES to prepare this exact local runtime: ').strip() != 'YES':
                print('Cancelled before download or runtime preparation.')
                return 0
        report = _prepare(output, record, archive=archive)
        print('The pinned files were prepared and verified. No audio program was executed.')
        print('Open Audio Studio, Production, then Choose FFmpeg executable:')
        print(report['ffmpeg'])
        print('Keep the entire runtime folder. FFprobe is beside FFmpeg:')
        print(report['ffprobe'])
        return 0
    except EOFError:
        print('Cancelled: no confirmation was received.')
        return 0
    except KeyboardInterrupt:
        print('\nInterrupted. Engine settings were not changed; retain any created folder for review.')
        return 130
    except Exception as error:
        print('Local audio setup failed: ' + type(error).__name__ + ': ' + str(error), file=sys.stderr)
        print('No engine settings were changed. Retain any created files and choose a new output folder.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
