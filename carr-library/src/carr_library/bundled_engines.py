"""Verify optional engines from the installed, sealed offline bundle.

Audio discovery delegates to the canonical audio-runtime resolver, which also
supports the explicitly sealed historical audio/ layout. The citation-runtime/
contract remains unchanged across launch entries. Missing optional components remain optional; damaged included files
must not silently fall back to a different executable on PATH.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys

from .durable_state import local_path, regular

COMPONENTS = {
    'audio': ('bundled_audio', {'bin/ffmpeg.exe', 'bin/ffprobe.exe', 'LICENSE', 'README.txt'}),
    'citation-runtime': ('bundled_citation_runtime', {'node.exe', 'LICENSE', 'README.md'}),
}


def _unique(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError('Duplicate bundled engine provenance key.')
        value[name] = item
    return value


def _verified_file(root, name, expected, *, maximum=512 * 1024**2):
    path = local_path(root / name, root=root, exists=True)
    before = regular(path)
    if not 0 < before.st_size <= maximum:
        raise ValueError('Bundled engine file exceeds its size allowance.')
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    with path.open('rb') as stream:
        value = hashlib.file_digest(stream, 'sha256').hexdigest()
    if value != expected or identity(regular(path)) != identity(before):
        raise ValueError('Bundled engine file differs from its sealed identity: ' + name)
    return path


def verify_optional_runtime(root, manifest, component, *, execute=False):
    """Verify the complete component; optional version probes use bounded IPC."""
    if component not in COMPONENTS:
        raise ValueError('Unknown bundled engine component.')
    flag, required = COMPONENTS[component]
    files = manifest['files']
    inventory = {name[len(component) + 1:]: digest for name, digest in files.items()
                 if name.startswith(component + '/')}
    if not manifest.get(flag, False):
        if inventory:
            raise ValueError('Unannounced bundled engine inventory.')
        return {'status': 'NOT_INCLUDED'}
    if set(inventory) != required | {'provenance.json'}:
        raise ValueError('Incomplete or unexpected bundled engine inventory.')
    root = local_path(root, exists=True)
    provenance = _verified_file(root, component + '/provenance.json', inventory['provenance.json'], maximum=1024**2)
    lock = json.loads(provenance.read_bytes(), object_pairs_hook=_unique)
    if component == 'audio':
        records = lock.get('output_files', {})
        if (lock.get('schema') != 1 or lock.get('engine') != 'FFmpeg'
                or set(records) != required):
            raise ValueError('Unsupported bundled audio provenance.')
        pinned = {name: record.get('sha256') for name, record in records.items()}
        version = lock.get('engine_version')
        programs = {'ffmpeg': 'bin/ffmpeg.exe', 'ffprobe': 'bin/ffprobe.exe'}
    else:
        pinned = lock.get('files', {})
        if set(pinned) != required:
            raise ValueError('Unsupported bundled citation provenance.')
        version = lock.get('version')
        programs = {'node': 'node.exe'}
    if not isinstance(version, str) or not re.fullmatch('[0-9][A-Za-z0-9.+_-]{0,100}', version):
        raise ValueError('Invalid bundled engine version.')
    paths = {}
    for name in required:
        if inventory[name] != pinned.get(name):
            raise ValueError('Bundled engine provenance differs from its sealed inventory.')
        paths[name] = _verified_file(root, component + '/' + name, inventory[name])
    result = {'status': 'PASS', 'version': version, 'execution': 'NOT_RUN',
              'programs': {name: str(paths[relative]) for name, relative in programs.items()}}
    if execute:
        from .audio.models import AudioBudget
        from .audio.process import run_process
        versions = {}
        for name, executable in result['programs'].items():
            arguments = [executable, '--version'] if name == 'node' else [executable, '-hide_banner', '-version']
            output = run_process(arguments, budget=AudioBudget(timeout=30, max_log_bytes=65536)).stdout.decode('utf-8', 'strict')
            expected = 'v' + version if name == 'node' else name + ' version ' + version
            if not output.splitlines() or not output.splitlines()[0].startswith(expected):
                raise ValueError('Bundled engine reported a different version.')
            versions[name] = output.splitlines()[0]
        result.update(execution='PASS', versions=versions)
    return result


def bundled_programs(component):
    """Verify one included component once and return its exact program paths."""
    if component == 'audio':
        from .audio_runtime import bundled_audio_tools
        return bundled_audio_tools() or {}
    if component not in COMPONENTS:
        raise ValueError('Unknown bundled engine component.')
    root = Path(sys.prefix).absolute().parent
    if not (root / 'bundle-manifest.json').exists():
        return {}
    from .native_checks import _manifest
    manifest = _manifest(root)
    result = verify_optional_runtime(root, manifest, component)
    return result.get('programs', {})


def bundled_executable(name):
    """Find an included engine only inside this installed Python environment."""
    component = {'ffmpeg': 'audio', 'ffprobe': 'audio', 'node': 'citation-runtime'}.get(name)
    if component is None:
        raise ValueError('Unknown optional executable.')
    return bundled_programs(component).get(name)


def executable_on_path(name):
    """Search explicit absolute PATH directories without implicit CWD lookup.

    Python 3.11 does not append PATHEXT for an absolute candidate on Windows;
    these installed native engines therefore use their explicit .exe suffix.
    """
    if name not in ('ffmpeg', 'ffprobe', 'espeak', 'espeak-ng', 'node'):
        raise ValueError('Unknown local executable.')
    filename = name + ('.exe' if os.name == 'nt' else '')
    for directory in os.get_exec_path():
        if not directory or directory.startswith(('\\\\', '//')) or not Path(directory).is_absolute():
            continue
        candidate = shutil.which(str(Path(directory) / filename))
        if candidate is not None:
            return candidate
    return None
