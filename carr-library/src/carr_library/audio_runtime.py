"""Verify and discover the optional, sealed Windows FFmpeg tools.

The library never downloads or executes a tool during discovery. Only the
reviewed bundle can be added to PATH; user-selected absolute tool paths remain
the responsibility of the existing audio adapters. Nothing is loaded into the
Python process, and no global network or provider setting is changed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import sys

from .native_runtime import _absolute, _directory, _entry, _plain, _read, _sha, _pe_x64, _unique


DIRECTORY = 'audio-runtime'
MANIFEST = 'audio-manifest.json'
PACKAGE_ID = 'ffmpeg-win64-lgpl-shared-20261126'
SOURCE_LOCK_SHA256 = 'fba184226c559a39684810ce34b823b86b75a3708711b599dfbd437c5511f80a'
MAX_FILES = 2048
MAX_BYTES = 512 * 1024 * 1024
TOOLS = {'ffmpeg': 'bin/ffmpeg.exe', 'ffprobe': 'bin/ffprobe.exe', 'ffplay': 'bin/ffplay.exe'}


def _read_json(path, expected_hash=None):
    """Parse and identify one bounded byte snapshot, never two pathname reads."""
    info = _plain(path)
    limit = 8 * 1024 * 1024
    if not 0 < info.st_size <= limit:
        raise ValueError('Audio runtime metadata exceeds its size limit.')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) != info.st_size:
        raise ValueError('Audio runtime metadata changed while being read.')
    digest = hashlib.sha256(raw).hexdigest()
    if expected_hash is not None and digest != expected_hash:
        raise ValueError('Audio runtime source lock differs from the reviewed package.')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique), digest, len(raw)


def _source_lock(path):
    lock, _digest, size = _read_json(path, SOURCE_LOCK_SHA256)
    # Integrity and execution evidence do not imply redistribution rights.
    # This exact package remains owner-local until the transitive notices,
    # corresponding sources and replacement obligations are independently reviewed.
    review = lock.get('review') if type(lock) is dict else None
    if (type(review) is not dict
            or lock.get('use_scope') != 'private-owner-local-installation'
            or review.get('public_distribution_clearance') is not False
            or review.get('third_party_complete_source_bundle') is not False):
        raise ValueError('Audio runtime distribution review is missing or differs from the reviewed owner-local profile.')
    return lock, size


def _locked_inventory(root):
    lock_path = root / 'provenance/source-lock.json'
    lock, lock_size = _source_lock(lock_path)
    if (type(lock) is not dict or type(lock.get('schema')) is not int or lock['schema'] != 1
            or lock.get('package_id') != PACKAGE_ID or lock.get('target') != 'Windows x64'
            or lock.get('executables') != TOOLS):
        raise ValueError('Unsupported audio runtime source lock.')
    entries = {}
    for section in ('archive_files', 'extra_files'):
        records = lock.get(section)
        if type(records) is not dict:
            raise ValueError('Audio runtime source inventory is missing.')
        for name, record in records.items():
            if (type(record) is not dict or set(record) != {'size', 'sha256'}
                    or type(record['size']) is not int or not 0 <= record['size'] <= MAX_BYTES
                    or name in entries):
                raise ValueError('Invalid audio runtime source inventory.')
            _entry(name, record['sha256'])
            entries[name] = record
    entries['provenance/source-lock.json'] = {'size': lock_size,
                                            'sha256': SOURCE_LOCK_SHA256}
    if (not 1 <= len(entries) <= MAX_FILES or not set(TOOLS.values()) <= entries.keys()
            or len({name.casefold() for name in entries}) != len(entries)
            or MANIFEST in entries or sum(r['size'] for r in entries.values()) > MAX_BYTES):
        raise ValueError('Audio runtime inventory has missing tools, collisions or excessive size.')
    return lock, entries


def _inspect_audio_runtime(runtime_dir):
    root = _absolute(runtime_dir)
    _directory(root)
    lock, entries = _locked_inventory(root)
    manifest, manifest_hash, _manifest_size = _read_json(root / MANIFEST)
    expected = {name: record['sha256'] for name, record in entries.items()}
    if (type(manifest) is not dict
            or set(manifest) != {'schema', 'target', 'package_id', 'source_lock_sha256', 'files'}
            or type(manifest['schema']) is not int or manifest['schema'] != 1
            or manifest['target'] != 'Windows x64' or manifest['package_id'] != PACKAGE_ID
            or manifest['source_lock_sha256'] != SOURCE_LOCK_SHA256
            or manifest['files'] != expected):
        raise ValueError('Audio runtime manifest differs from its reviewed source inventory.')
    actual, total, visited = set(), 0, 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        visited += len(dirs) + len(files)
        if visited > MAX_FILES * 2:
            raise ValueError('Audio runtime contains too many filesystem entries.')
        for name in dirs:
            _plain(Path(directory) / name, directory=True)
        for name in files:
            path = Path(directory) / name
            info = _plain(path)
            relative = path.relative_to(root).as_posix()
            if relative == MANIFEST:
                continue
            record = entries.get(relative)
            if record is None or info.st_size != record['size']:
                raise ValueError('Unexpected audio runtime file or size: ' + relative)
            if _sha(path) != record['sha256']:
                raise ValueError('Audio runtime checksum failed: ' + relative)
            if relative.startswith('bin/') and path.suffix.lower() in {'.dll', '.exe'}:
                _pe_x64(path)
            actual.add(relative)
            total += info.st_size
    if actual != entries.keys():
        raise ValueError('Required audio runtime files are missing.')
    if _sha(root / MANIFEST) != manifest_hash:
        raise ValueError('Audio runtime manifest changed during verification.')
    report = {'target': 'Windows x64', 'package_id': PACKAGE_ID, 'files': len(actual),
              'bytes': total, 'version': lock.get('version'),
              'source_lock_sha256': SOURCE_LOCK_SHA256, 'manifest_sha256': manifest_hash,
              'tools': {name: str(root / value) for name, value in TOOLS.items()},
              'native_execution': 'NOT_RUN',
              'use_scope': lock['use_scope'],
              'public_distribution_clearance': lock['review']['public_distribution_clearance'],
              'third_party_complete_source_bundle': lock['review']['third_party_complete_source_bundle']}
    return report, expected


def verify_audio_runtime(runtime_dir):
    """Check every byte and PE architecture without executing the runtime."""
    return _inspect_audio_runtime(runtime_dir)[0]


def _discover():
    candidates = []
    for prefix in (sys.prefix, sys.base_prefix):
        path = _absolute(prefix)
        for root in (path, path.parent, path.parent.parent):
            runtime = root / DIRECTORY
            # A sibling source checkout can legitimately be named
            # audio-runtime. Only an installation seal or runtime-specific
            # layout identifies an implicit bundle; explicit roots stay strict.
            marked = any(os.path.lexists(runtime / name) for name in
                         (MANIFEST, 'bin', 'provenance/source-lock.json'))
            if os.path.lexists(root / 'bundle-manifest.json') or marked:
                if root not in candidates:
                    candidates.append(root)
                break
    if len(candidates) > 1:
        raise ValueError('Interpreter prefixes identify different audio runtime bundles.')
    return candidates[0] if candidates else None


def bundled_audio_tools(bundle_root=None):
    """Return verified absolute paths, or None for an installation without audio.

    A declared, altered or unannounced runtime fails closed. Discovery ignores
    CWD, PATH and environment overrides. This function also supports a packaging
    host inspecting Windows files; it makes no native-execution claim.
    """
    root = _absolute(bundle_root) if bundle_root is not None else _discover()
    if root is None:
        return None
    _directory(root)
    runtime = root / DIRECTORY
    has_runtime = os.path.lexists(runtime)
    if not os.path.lexists(root / 'bundle-manifest.json'):
        if has_runtime:
            raise ValueError('Audio runtime has no enclosing bundle manifest.')
        return None
    outer = _read(root / 'bundle-manifest.json')
    if (type(outer) is not dict or type(outer.get('schema')) is not int or outer.get('schema') != 1
            or outer.get('target') != 'Windows x64 / CPython 3.11'
            or type(outer.get('files')) is not dict or not 1 <= len(outer['files']) <= 50000):
        raise ValueError('Invalid enclosing audio installation manifest.')
    included = outer.get('bundled_audio_runtime', False)
    if type(included) is not bool:
        raise ValueError('Invalid bundled audio runtime flag.')
    inventory = {}
    for name, digest in outer['files'].items():
        _entry(name, digest)
        if name.casefold().startswith(DIRECTORY + '/'):
            if not name.startswith(DIRECTORY + '/'):
                raise ValueError('Ambiguous audio runtime installation path.')
            inventory[name[len(DIRECTORY) + 1:]] = digest
    if not included:
        if has_runtime or inventory:
            raise ValueError('Unannounced audio runtime is present in the installation.')
        return None
    if not has_runtime:
        raise ValueError('The declared audio runtime is missing.')
    report, verified_inventory = _inspect_audio_runtime(runtime)
    expected = dict(verified_inventory, **{MANIFEST: report['manifest_sha256']})
    if inventory != expected:
        raise ValueError('Audio runtime identity differs from the enclosing bundle.')
    return report['tools']


def activate_bundled_audio_runtime(bundle_root=None):
    """Add only a verified Windows bundle to this process's child-tool PATH.

    No DLL is loaded, no external application setting is overwritten and no
    environment-selected tool is trusted. Explicit paths in the audio adapter
    still take precedence. Returns the exact tool paths, or None on other hosts.
    """
    tools = bundled_audio_tools(bundle_root)
    if tools is None or sys.platform != 'win32':
        return None
    if struct.calcsize('P') != 8:
        raise RuntimeError('The bundled audio tools require Windows x64.')
    binary_dir = str(Path(tools['ffmpeg']).parent)
    previous = os.environ.get('PATH', '').split(os.pathsep)
    keep = [p for p in previous if p and os.path.normcase(p) != os.path.normcase(binary_dir)]
    os.environ['PATH'] = os.pathsep.join([binary_dir, *keep])
    return tools
