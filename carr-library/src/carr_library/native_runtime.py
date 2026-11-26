"""Verify and activate the optional private Windows C++ runtime supplement.

Only the reviewed MSVCP DLLs are added. CPython's existing VCRUNTIME and the
separate OCR runtime are never replaced. Discovery follows interpreter prefixes,
never the current directory, PATH, an environment override, or the network.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys


DIRECTORY = 'native-runtime'
MANIFEST = 'native-manifest.json'
SOURCE_LOCK_SHA256 = '3785f3f70973df00b4bde67191e67d094fbb0d58b9f2065dcc919a6e1df1a3ad'
DLLS = {
    'msvcp140.dll': (652136, '59d325227321d52b9a523ea401ce6787a5d59f78229a0ff3a6f124e5d3445685'),
    'msvcp140_1.dll': (35696, '1c6522f4d005539109aa040f063659704bfda94a224142f50942573cfbcf7ec9'),
}
HOST_DLLS = {
    'vcruntime140.dll': 'd5e4d9a3e835fa679450145d6a7d94e36573a509317111904d9b3712c30d9066',
    'vcruntime140_1.dll': '1f2d41c4aa5db0bc33ebf7b66d72943a817d7ce6cbe880502a9403823633093f',
}
FILES = {
    'README.txt', 'bin/msvcp140.dll', 'bin/msvcp140_1.dll',
    'licenses/LICENSE.TXT', 'licenses/LICENSE.RTF', 'licenses/LICENSE.DOCX',
    'provenance/source-lock.json', 'provenance/pe-compatibility.json',
}
MAX_MANIFEST = 8 * 1024 * 1024
MAX_BYTES = 4 * 1024 * 1024


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate native runtime manifest key.')
        result[key] = value
    return result


def _absolute(value):
    path = Path(value)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Native runtime paths must be absolute and contain no parent traversal.')
    return path


def _plain(path, *, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('Native runtime paths must be ordinary local files and directories.')
    return info


def _directory(path):
    # Do not resolve links before inspection: that would hide their existence.
    for component in (path, *path.parents):
        _plain(component, directory=True)


def _read(path, limit=MAX_MANIFEST):
    info = _plain(path)
    if not 0 < info.st_size <= limit:
        raise ValueError('Native runtime metadata exceeds its size limit.')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) != info.st_size:
        raise ValueError('Native runtime metadata changed while being read.')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)


def _sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _entry(name, digest):
    if not isinstance(name, str) or not isinstance(digest, str):
        raise ValueError('Invalid native runtime inventory entry.')
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or path.as_posix() != name or '..' in path.parts
            or '\\' in name or ':' in name or any(ord(c) < 32 for c in name)
            or any(part.endswith((' ', '.')) for part in path.parts)
            or not re.fullmatch('[0-9a-f]{64}', digest)):
        raise ValueError('Invalid native runtime inventory entry.')


def _pe_x64(path):
    with path.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise ValueError('Native runtime DLL is not a Windows executable.')
        offset = struct.unpack_from('<I', header, 60)[0]
        if not 64 <= offset <= 1024 * 1024:
            raise ValueError('Invalid native runtime PE header.')
        stream.seek(offset)
        signature = stream.read(6)
    if signature != b'PE\0\0\x64\x86':
        raise ValueError('Native runtime DLLs must target Windows x64.')


def verify_native_runtime(runtime_dir):
    """Verify the standalone supplement; does not register or execute any DLL."""
    root = _absolute(runtime_dir)
    _directory(root)
    value = _read(root / MANIFEST)
    required = {'schema', 'target', 'use_scope', 'source_lock_sha256', 'files'}
    if (type(value) is not dict or set(value) != required
            or type(value['schema']) is not int or value['schema'] != 1
            or value['target'] != 'Windows x64'
            or value['use_scope'] != 'private-local-installation'
            or value['source_lock_sha256'] != SOURCE_LOCK_SHA256
            or type(value['files']) is not dict or set(value['files']) != FILES):
        raise ValueError('Unsupported native runtime supplement or source identity.')
    for name, digest in value['files'].items():
        _entry(name, digest)
    actual, total = set(), 0
    for path in root.rglob('*'):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            _plain(path, directory=True)
            continue
        _plain(path)
        name = path.relative_to(root).as_posix()
        if name == MANIFEST:
            continue
        total += info.st_size
        if total > MAX_BYTES or name not in value['files']:
            raise ValueError('Unexpected native runtime file or size.')
        actual.add(name)
        if _sha(path) != value['files'][name]:
            raise ValueError('Native runtime checksum failed: ' + name)
    if actual != FILES:
        raise ValueError('Native runtime files are missing.')
    lock_path = root / 'provenance' / 'source-lock.json'
    if _sha(lock_path) != SOURCE_LOCK_SHA256:
        raise ValueError('Native runtime source lock changed.')
    source = _read(lock_path)
    for name, (size, digest) in DLLS.items():
        path = root / 'bin' / name
        if path.stat().st_size != size or _sha(path) != digest:
            raise ValueError('Native runtime DLL differs from the reviewed source package: ' + name)
        record = source['selected_original_files']['Library/bin/' + name]
        if record['destination'] != 'bin/' + name or record['sha256'] != digest or record['size'] != size:
            raise ValueError('Native runtime package/file identity differs.')
        _pe_x64(path)
    return {'target': value['target'], 'use_scope': value['use_scope'],
            'dlls': sorted(DLLS), 'files': len(FILES), 'bytes': total,
            'manifest_sha256': _sha(root / MANIFEST),
            'source_lock_sha256': SOURCE_LOCK_SHA256, 'native_execution': 'NOT_RUN'}


def _bundle(root):
    root = _absolute(root)
    _directory(root)
    manifest_path = root / 'bundle-manifest.json'
    runtime = root / DIRECTORY
    has_runtime = os.path.lexists(runtime)
    if not os.path.lexists(manifest_path):
        if has_runtime:
            raise ValueError('Native runtime has no enclosing installation manifest.')
        return None
    outer = _read(manifest_path)
    if (type(outer) is not dict or type(outer.get('schema')) is not int
            or outer.get('schema') != 1
            or outer.get('target') != 'Windows x64 / CPython 3.11'
            or type(outer.get('files')) is not dict
            or not 1 <= len(outer['files']) <= 50000):
        raise ValueError('Invalid enclosing installation manifest.')
    included = outer.get('bundled_native_runtime', False)
    if type(included) is not bool:
        raise ValueError('Invalid native runtime installation flag.')
    inventory = {}
    for name, digest in outer['files'].items():
        _entry(name, digest)
        if name.casefold().startswith(DIRECTORY + '/'):
            if not name.startswith(DIRECTORY + '/'):
                raise ValueError('Ambiguous native runtime installation path.')
            inventory[name.removeprefix(DIRECTORY + '/')] = digest
    if not included:
        if has_runtime or inventory:
            raise ValueError('Unannounced native runtime files are present in the installation.')
        return None
    if not has_runtime:
        raise ValueError('The declared native runtime supplement is missing.')
    report = verify_native_runtime(runtime)
    inner = _read(runtime / MANIFEST)
    expected = dict(inner['files'], **{MANIFEST: report['manifest_sha256']})
    if inventory != expected:
        raise ValueError('Native runtime inventory differs from the enclosing installation.')
    return runtime


def _discover():
    candidates = []
    for prefix in (sys.prefix, sys.base_prefix):
        path = _absolute(prefix)
        # The supported layouts are bundle/runtime and bundle/.venv. A second
        # parent permits a Scripts-level prefix without inspecting distant roots.
        for root in (path, path.parent, path.parent.parent):
            if (os.path.lexists(root / 'bundle-manifest.json')
                    or os.path.lexists(root / DIRECTORY)):
                if root not in candidates:
                    candidates.append(root)
                break
    if len(candidates) > 1:
        raise ValueError('Interpreter prefixes identify different native runtime bundles.')
    return candidates[0] if candidates else None


def _check_host(root):
    host = root / 'runtime'
    _directory(host)
    if os.path.normcase(str(_absolute(sys.base_prefix))) != os.path.normcase(str(host)):
        raise ValueError('The native runtime supplement requires the pinned bundled Python.')
    for name, expected in HOST_DLLS.items():
        _plain(host / name)
        if _sha(host / name) != expected:
            raise ValueError('The bundled Python runtime identity changed: ' + name)


def _check_loaded_host():
    """Reject an already-loaded VCRUNTIME with unreviewed bytes on Windows."""
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel.GetModuleHandleW.restype = wintypes.HMODULE
    kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    for name, expected in HOST_DLLS.items():
        handle = kernel.GetModuleHandleW(name)
        if not handle:
            continue
        path = ctypes.create_unicode_buffer(32768)
        length = kernel.GetModuleFileNameW(handle, path, len(path))
        if not 0 < length < len(path):
            raise RuntimeError('Cannot identify the loaded Microsoft runtime.')
        actual = Path(path.value)
        _plain(actual)
        if _sha(actual) != expected:
            raise ValueError('An incompatible Microsoft runtime is already loaded: ' + name)


def activate_bundled_runtime(bundle_root=None):
    """Activate a verified optional bundle and return its own directory handle.

    Returns None in source mode, when the bundle declares no supplement, or on
    a non-Windows host after inventory validation. A declared but missing or
    altered supplement always raises. Call before importing wx or ONNX Runtime.
    The caller retains and closes its own handle. Each activation gets an
    independent handle, so a nested diagnostic cannot close the desktop's one.
    """
    root = _absolute(bundle_root) if bundle_root is not None else _discover()
    if root is None:
        return None
    runtime = _bundle(root)
    if runtime is None or sys.platform != 'win32':
        return None
    if struct.calcsize('P') != 8 or not hasattr(os, 'add_dll_directory'):
        raise RuntimeError('The native runtime supplement requires Windows x64 Python 3.8 or later.')
    _check_host(root)
    _check_loaded_host()
    return os.add_dll_directory(str(runtime / 'bin'))
