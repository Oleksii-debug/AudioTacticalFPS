"""One verifier for preparing, packaging and activating optional Windows audio.

Discovery follows the running interpreter's installation, never an environment
override, current directory or arbitrary user script. A declared runtime cannot
silently fall back to a different PATH executable when validation fails.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import sys
from threading import RLock

from ..native_runtime import (_absolute, _directory, _entry, _plain, _read, _sha, _pe_x64)


DIRECTORY = 'audio-runtime'
MANIFEST = 'audio-runtime-manifest.json'
SOURCE_LOCK_SHA256 = '34992db6bd04f54e83a1ee3faa03c906b419f2c2289f8a86826106a71e163a86'
MAX_FILES = 10000
MAX_BYTES = 1536 * 1024**2
_active_lock = RLock()
_active = None


def verify_audio_runtime(runtime_dir, expected_manifest_sha256=None):
    """Verify all original publisher bytes and the enclosing package seal, if given."""
    root = _absolute(runtime_dir)
    _directory(root)
    manifest_path = root / MANIFEST
    _plain(manifest_path)
    manifest_digest = _sha(manifest_path)
    if expected_manifest_sha256 is not None and manifest_digest != expected_manifest_sha256:
        raise ValueError('Audio runtime manifest differs from the sealed application bundle.')
    manifest = _read(manifest_path)
    fields = {'version', 'target', 'use_scope', 'source_lock_sha256', 'windows_execution', 'files'}
    if (type(manifest) is not dict or set(manifest) != fields
            or type(manifest['version']) is not int or manifest['version'] != 1
            or manifest['target'] != 'Windows x64' or manifest['use_scope'] != 'private-local-installation'
            or manifest['source_lock_sha256'] != SOURCE_LOCK_SHA256
            or type(manifest['files']) is not dict or not 1 <= len(manifest['files']) <= MAX_FILES
            or not isinstance(manifest['windows_execution'], str) or len(manifest['windows_execution']) > 256):
        raise ValueError('Unsupported audio runtime manifest or source identity.')
    lock_path = root / 'carr-provenance' / 'source-lock.json'
    _directory(lock_path.parent)
    _plain(lock_path)
    if _sha(lock_path) != SOURCE_LOCK_SHA256:
        raise ValueError('Audio runtime source lock differs from the reviewed publisher package.')
    lock = _read(lock_path)
    originals = lock.get('original_files')
    if type(originals) is not dict or not originals:
        raise ValueError('The audio runtime source lock has no original file inventory.')
    for name, record in manifest['files'].items():
        if (type(record) is not dict or set(record) != {'sha256', 'size_bytes'}
                or type(record['size_bytes']) is not int or not 0 <= record['size_bytes'] <= 1024**3):
            raise ValueError('Invalid audio runtime inventory record.')
        _entry(name, record['sha256'])
    expected_originals = {name: record for name, record in manifest['files'].items()
                          if not name.startswith('carr-provenance/')}
    if expected_originals != originals:
        raise ValueError('Original audio runtime files differ from the exact publisher archive.')
    provenance = {name for name in manifest['files'] if name.startswith('carr-provenance/')}
    if not {'carr-provenance/source-lock.json', 'carr-provenance/README.txt'}.issubset(provenance) or (
            provenance - {'carr-provenance/source-lock.json', 'carr-provenance/README.txt',
                          'carr-provenance/native-version-report.json'}):
        raise ValueError('Unexpected audio runtime provenance files.')
    actual, total = set(), 0
    for path in root.rglob('*'):
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            _plain(path, directory=True)
            continue
        _plain(path)
        name = path.relative_to(root).as_posix()
        if name == MANIFEST:
            continue
        actual.add(name)
        total += details.st_size
        if total > MAX_BYTES or len(actual) > MAX_FILES or name not in manifest['files']:
            raise ValueError('Unexpected audio runtime file or size.')
        record = manifest['files'][name]
        if details.st_size != record['size_bytes'] or _sha(path) != record['sha256']:
            raise ValueError('Audio runtime checksum failed: ' + name)
    if actual != set(manifest['files']):
        raise ValueError('Audio runtime files are missing.')
    for name in ('ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe'):
        _pe_x64(root / 'bin' / name)
    return {'status': 'VERIFIED', 'release': lock['release'],
            'use_scope': manifest['use_scope'], 'source_lock_sha256': SOURCE_LOCK_SHA256,
            'manifest_sha256': manifest_digest,
            'executables': {name: manifest['files']['bin/' + name]
                            for name in ('ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe')},
            'windows_execution': manifest['windows_execution']}


def _bundle(root):
    root = _absolute(root)
    _directory(root)
    if any(child.name.casefold() == DIRECTORY and child.name != DIRECTORY for child in root.iterdir()):
        raise ValueError('Ambiguous audio runtime directory in the installation.')
    manifest_path, runtime = root / 'bundle-manifest.json', root / DIRECTORY
    has_runtime = os.path.lexists(runtime)
    if not os.path.lexists(manifest_path):
        if has_runtime:
            raise ValueError('Audio runtime has no enclosing installation manifest.')
        return None
    outer = _read(manifest_path)
    if (type(outer) is not dict or type(outer.get('schema')) is not int or outer.get('schema') != 1
            or outer.get('target') != 'Windows x64 / CPython 3.11'
            or type(outer.get('files')) is not dict or not 1 <= len(outer['files']) <= 50000):
        raise ValueError('Invalid enclosing audio installation manifest.')
    included = outer.get('bundled_audio', False)
    if type(included) is not bool:
        raise ValueError('Invalid audio installation flag.')
    inventory = {}
    for name, checksum in outer['files'].items():
        _entry(name, checksum)
        if name.casefold().startswith(DIRECTORY + '/'):
            if not name.startswith(DIRECTORY + '/'):
                raise ValueError('Ambiguous audio runtime path in the installation.')
            inventory[name.removeprefix(DIRECTORY + '/')] = checksum
    if not included:
        if has_runtime or inventory:
            raise ValueError('Unannounced audio runtime files are present in the installation.')
        return None
    if not has_runtime or MANIFEST not in inventory:
        raise ValueError('The declared audio runtime is missing.')
    report = verify_audio_runtime(runtime, expected_manifest_sha256=inventory[MANIFEST])
    inner = _read(runtime / MANIFEST)
    expected = {name: item['sha256'] for name, item in inner['files'].items()}
    expected[MANIFEST] = report['manifest_sha256']
    if inventory != expected:
        raise ValueError('Audio runtime inventory differs from the enclosing installation.')
    return runtime


def discover_bundled_audio():
    candidates = []
    for prefix in (sys.prefix, sys.base_prefix):
        path = _absolute(prefix)
        for root in (path, path.parent, path.parent.parent):
            if os.path.lexists(root / 'bundle-manifest.json') or os.path.lexists(root / DIRECTORY):
                if root not in candidates:
                    candidates.append(root)
                break
    if len(candidates) > 1:
        raise ValueError('Interpreter prefixes identify different audio runtime bundles.')
    return candidates[0] if candidates else None


@dataclass
class AudioRuntimeHandle:
    directory: Path
    previous_path: str | None
    installed_path: str
    closed: bool = False

    def close(self):
        global _active
        with _active_lock:
            if self.closed:
                return
            self.closed = True
            if _active is None or _active['directory'] != self.directory:
                return
            _active['users'] -= 1
            if _active['users']:
                return
            if os.environ.get('PATH') == self.installed_path:
                if self.previous_path is None:
                    os.environ.pop('PATH', None)
                else:
                    os.environ['PATH'] = self.previous_path
            else:
                # Preserve unrelated changes while removing only our leading entry.
                current = os.environ.get('PATH', '')
                prefix = str(self.directory) + os.pathsep
                if current.startswith(prefix):
                    os.environ['PATH'] = current[len(prefix):]
                elif current == str(self.directory):
                    os.environ.pop('PATH', None)
            _active = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def activate_bundled_audio_runtime(bundle_root=None):
    """Verify an optional bundle, then prepend only its verified audio bin folder.

    Development without a bundle returns None and may use explicitly installed
    system tools. A declared missing/tampered bundle always raises. On non-Windows
    hosts inventory can be verified, but Windows executables are never activated.
    The caller retains the returned handle and closes it during shutdown.
    """
    global _active
    root = _absolute(bundle_root) if bundle_root is not None else discover_bundled_audio()
    if root is None:
        return None
    runtime = _bundle(root)
    if runtime is None or sys.platform != 'win32':
        return None
    directory = runtime / 'bin'
    with _active_lock:
        if _active is not None:
            if _active['directory'] != directory:
                raise ValueError('A different bundled audio runtime is already active.')
            _active['users'] += 1
        else:
            previous = os.environ.get('PATH')
            installed = str(directory) + (os.pathsep + previous if previous is not None else '')
            inner = _read(runtime / MANIFEST)
            _active = {'directory': directory, 'previous': previous, 'installed': installed,
                       'users': 1, 'executables': {name: inner['files']['bin/' + name]
                       for name in ('ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe')}}
            os.environ['PATH'] = installed
        return AudioRuntimeHandle(directory, _active['previous'], _active['installed'])


def resolve_active_audio_tool(name):
    """Resolve a sealed executable directly, never through a mutable PATH.

    Recheck bytes before every invocation so removing or replacing an activated
    runtime cannot silently select a system executable. None means no bundle is
    active, which permits an explicitly installed development runtime.
    """
    executable = name.removesuffix('.exe') + '.exe'
    if executable not in ('ffmpeg.exe', 'ffprobe.exe', 'ffplay.exe'):
        return None
    with _active_lock:
        if _active is None:
            return None
        directory = _active['directory']
        _directory(directory)
        path = directory / executable
        details = _plain(path)
        record = _active['executables'][executable]
        if details.st_size != record['size_bytes'] or _sha(path) != record['sha256']:
            raise ValueError('Activated audio executable differs from its reviewed bytes: ' + executable)
        return str(path)
