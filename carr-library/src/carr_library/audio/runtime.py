"""Optional reviewed FFmpeg supplement for a private Windows installation.

No download, registry changes or PATH mutation. The selected executable and
publisher notice bytes are pinned in application code. A manifest cannot approve
substitute executables by changing its own checksums.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys

DIRECTORY = 'audio-runtime'
MANIFEST = 'audio-runtime.json'
ARCHIVE_SHA256 = 'b9bbbb3acece000cbeec994f856649354e52c773807841e78908374b24d658fd'
FILES = {
    'bin/ffmpeg.exe': {'size': 110590976, 'sha256': 'ed8f66cb877087a96f5bbb40a2944f0e764f2ad8870d5cb647360d912e62bc07'},
    'bin/ffprobe.exe': {'size': 110374912, 'sha256': 'df0199f606faa88e9ad192732a7c3987acfb647663534c297772e17730611a08'},
    'LICENSE': {'size': 35147, 'sha256': '8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903'},
    'README.txt': {'size': 43002, 'sha256': '693a04eb542211fceb84169b073578311c0fed751d716ff90806e0818c71e780'},
}


def manifest():
    return dict(schema=1, version='9.1', target='Windows x64',
                use_scope='private-local-installation', archive_sha256=ARCHIVE_SHA256,
                files={name: dict(entry) for name, entry in FILES.items()})


def _plain(path, *, directory=False):
    value = path.lstat()
    if (stat.S_ISLNK(value.st_mode)
            or getattr(value, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode))):
        raise ValueError('The audio runtime must contain ordinary local files and directories.')
    return value


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('Duplicate audio runtime manifest field.')
        value[key] = item
    return value


def verify_runtime(directory):
    from .ffmpeg import _local_path
    root = _local_path(directory).absolute()
    for component in (root, *root.parents):
        _plain(component, directory=True)
    info = _plain(root / MANIFEST)
    if not 0 < info.st_size <= 32768:
        raise ValueError('Invalid audio runtime manifest size.')
    with (root / MANIFEST).open('rb') as stream:
        raw = stream.read(32769)
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    if (type(value) is not dict or type(value.get('schema')) is not int
            or type(value.get('files')) is not dict
            or any(type(entry) is not dict or type(entry.get('size')) is not int
                   for entry in value['files'].values()) or value != manifest()):
        raise ValueError('The audio runtime identity differs from the reviewed package.')
    actual = set()
    for path in root.rglob('*'):
        if path.is_dir():
            _plain(path, directory=True)
            if path.relative_to(root).as_posix() != 'bin':
                raise ValueError('Unexpected directory in the audio runtime.')
            continue
        info = _plain(path)
        name = path.relative_to(root).as_posix()
        if name == MANIFEST:
            continue
        entry = FILES.get(name)
        if entry is None or info.st_size != entry['size'] or _sha(path) != entry['sha256']:
            raise ValueError('Audio runtime checksum or inventory mismatch: ' + name)
        actual.add(name)
        if name.endswith('.exe'):
            with path.open('rb') as stream:
                header = stream.read(64)
                if len(header) != 64 or header[:2] != b'MZ':
                    raise ValueError('An audio runtime executable has no valid PE header.')
                offset = struct.unpack_from('<I', header, 60)[0]
                if not 64 <= offset <= 1024 * 1024:
                    raise ValueError('Invalid audio runtime PE header offset.')
                stream.seek(offset)
                if stream.read(6) != b'PE\0\0\x64\x86':
                    raise ValueError('The audio runtime must target Windows x64.')
    if actual != set(FILES):
        raise ValueError('The audio runtime is incomplete.')
    return dict(status='PASS', version='9.1', target='Windows x64',
                archive_sha256=ARCHIVE_SHA256, files=len(actual), root=str(root))


def discover_runtime():
    """Discover from interpreter installation prefixes, never CWD or documents."""
    if sys.platform != 'win32':
        return None
    seen = set()
    for prefix in (Path(sys.prefix), Path(sys.base_prefix)):
        for root in (prefix, prefix.parent):
            if root in seen or not root.is_absolute():
                continue
            seen.add(root)
            candidate = root / DIRECTORY
            if os.path.lexists(candidate):
                verify_runtime(candidate)
                return candidate / 'bin'
    return None
