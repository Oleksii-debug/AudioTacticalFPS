"""Owned installed entry for the canonical source-only local audio setup.

The existing acquisition and preparation helpers keep their consent, network,
license and publication rules. This adapter supplies only installation lifetime
coordination and immutable, inventory-checked Python source snapshots.
"""
from __future__ import annotations

from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import re
import sys

from .installation_lease import installed_entrypoint, plain


TOOL_FILES = (
    'setup_local_audio.py', 'acquire_audio_runtime.py', 'prepare_audio_runtime.py',
    'audio-runtime/README.txt',
    'audio-runtime/licenses/BUILD_RECIPES_LICENSE',
    'audio-runtime/licenses/COPYING.GPLv2',
    'audio-runtime/licenses/COPYING.GPLv3',
    'audio-runtime/licenses/COPYING.LGPLv2.1',
    'audio-runtime/licenses/COPYING.LGPLv3',
    'audio-runtime/licenses/LICENSE.md',
    'audio-runtime/provenance/build-configuration.txt',
    'audio-runtime/provenance/checksums.sha256',
    'audio-runtime/provenance/dependency-source-index.json',
    'audio-runtime/source-lock.json',
)


def _sources(program):
    from .portable_entry import _read, _unique

    manifest_path = program / 'bundle-manifest.json'
    raw = _read(manifest_path, 16 * 1024**2)
    manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique)
    if (type(manifest) is not dict or type(manifest.get('schema')) is not int
            or manifest['schema'] != 1 or type(manifest.get('files')) is not dict):
        raise ValueError('The installed local audio setup has no valid source inventory.')
    snapshots, total = {}, 0
    for name in TOOL_FILES:
        relative = 'tools/audio-setup/' + name
        expected = manifest['files'].get(relative)
        if type(expected) is not str or not re.fullmatch('[0-9a-f]{64}', expected):
            raise ValueError('A local audio setup source is missing from the installation inventory.')
        value = _read(program.joinpath(*relative.split('/')), 4 * 1024**2)
        total += len(value)
        if total > 16 * 1024**2 or sha256(value).hexdigest() != expected:
            raise ValueError('The local audio setup source differs from its installed inventory.')
        snapshots[name] = value
    if _read(manifest_path, 16 * 1024**2) != raw:
        raise ValueError('The local audio setup inventory changed during review.')
    return snapshots


def _module(name, path, source):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None:
        raise RuntimeError('The selected local audio helper could not be loaded.')
    module = importlib.util.module_from_spec(spec)
    previous_path = list(sys.path)
    previous_bytecode = sys.dont_write_bytecode
    try:
        sys.dont_write_bytecode = True
        # Do not ask SourceFileLoader to reopen mutable code after verification.
        exec(compile(source, str(path), 'exec'), module.__dict__)
    finally:
        sys.path[:] = previous_path
        sys.dont_write_bytecode = previous_bytecode
    return module


def _setup(program):
    sources = _sources(program)
    folder = program / 'tools/audio-setup'
    setup = _module('carr_bundled_local_audio_setup', folder / 'setup_local_audio.py',
                    sources['setup_local_audio.py'])

    def selected_helper(name):
        if name not in {'acquire_audio_runtime', 'prepare_audio_runtime'}:
            raise ValueError('Unknown local audio setup helper.')
        filename = name + '.py'
        return _module('carr_local_audio_' + name, folder / filename, sources[filename])

    # Retain the existing helper's complete workflow. Only its fixed source
    # loader consumes already-reviewed bytes instead of opening those again.
    setup._load_helper = selected_helper
    return setup


@installed_entrypoint
def main(argv=None):
    environment = Path(sys.prefix).absolute()
    if environment.name.casefold() != '.venv':
        raise RuntimeError('Run local audio setup from the selected installed application.')
    plain(environment, directory=True)
    plain(environment / '.installation-complete')
    return _setup(environment.parent).main(argv)


if __name__ == '__main__':
    raise SystemExit(main())
