"""Attach the reviewed FFmpeg files to a fresh private Windows bundle.

This command performs no downloads and launches no bundled executable. It copies
only the sealed installation inventory and the four reviewed audio-runtime
files plus their manifest. It does not collect an environment, research data,
voice model, user cache, or arbitrary adjacent file. Publisher notices remain
included; this private packaging operation is not redistribution approval.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


HERE = Path(__file__).resolve().parent


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _ordinary(path, *, may_be_missing=False):
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            if may_be_missing:
                return
            raise
        if (not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise ValueError('Choose ordinary local bundle directories without symbolic links.')


def _copy(source, target, expected):
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Sealed bundle files cannot be symbolic links or reparse points.')
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    if _digest(target) != expected:
        raise ValueError('A sealed installation source changed while being copied.')
    with target.open('r+b') as stream:
        os.fsync(stream.fileno())


def _sync_directory(path):
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def attach_runtime(bundle, audio_runtime, output):
    """Create a new sealed bundle; preserve both verified source trees."""
    sys.path.insert(0, str(HERE.parent / 'src'))
    from carr_library.audio.ffmpeg import _local_path
    from carr_library.audio.runtime import DIRECTORY, FILES, MANIFEST, verify_runtime
    bundle, runtime, output = (_local_path(value).absolute() for value in (bundle, audio_runtime, output))
    if any('..' in path.parts for path in (bundle, runtime, output)):
        raise ValueError('Use bundle paths without parent traversal.')
    _ordinary(bundle)
    _ordinary(runtime)
    _ordinary(output.parent, may_be_missing=True)
    if os.path.lexists(output):
        raise FileExistsError('Use a new output folder.')
    if any(output.is_relative_to(source) or source.is_relative_to(output) for source in (bundle, runtime)):
        raise ValueError('The output must be outside both source directories.')
    spec = importlib.util.spec_from_file_location('carr_audio_bundle_installer', HERE / 'install_windows.py')
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    installer.verify(bundle)
    manifest = json.loads((bundle / 'bundle-manifest.json').read_text(encoding='utf-8'))
    if (manifest.get('bundled_audio_runtime') or os.path.lexists(bundle / DIRECTORY)
            or any(name.startswith(DIRECTORY + '/') for name in manifest['files'])):
        raise ValueError('The source bundle already contains an audio runtime.')
    applications = [name for name in manifest['files']
                    if name.startswith('wheelhouse/universal_research_library-') and name.endswith('.whl')]
    if len(applications) != 1:
        raise ValueError('Expected one application wheel.')
    with ZipFile(bundle / applications[0]) as wheel:
        if not {'carr_library/audio/runtime.py', 'carr_library/audio/ffmpeg.py'}.issubset(wheel.namelist()):
            raise ValueError('Rebuild the application with reviewed audio runtime discovery first.')
    report = verify_runtime(runtime)
    runtime_inventory = {name: entry['sha256'] for name, entry in FILES.items()}
    runtime_inventory[MANIFEST] = _digest(runtime / MANIFEST)
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.carr-audio-bundle-', dir=output.parent) as scratch:
        stage = Path(scratch) / 'bundle'
        stage.mkdir()
        for name, expected in manifest['files'].items():
            _copy(bundle / name, stage / name, expected)
        for name, expected in runtime_inventory.items():
            _copy(runtime / name, stage / DIRECTORY / name, expected)
        checked = verify_runtime(stage / DIRECTORY)
        if {k: v for k, v in checked.items() if k != 'root'} != {k: v for k, v in report.items() if k != 'root'}:
            raise ValueError('Audio runtime identity changed while being copied.')
        manifest['bundled_audio_runtime'] = True
        manifest['files'].update({DIRECTORY + '/' + name: digest for name, digest in runtime_inventory.items()})
        raw = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode('utf-8')
        with (stage / 'bundle-manifest.json').open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        installer.verify(stage)
        # Exclusive directory ownership, then a completion manifest last. An
        # interrupted output is preserved and fails verification on retry.
        output.mkdir(exist_ok=False)
        directories = set()
        for name in sorted(manifest['files']):
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            os.link(stage / name, target)
            directories.update(p for p in target.parents if p.is_relative_to(output))
        for directory in sorted(directories, key=lambda p: -len(p.parts)):
            _sync_directory(directory)
        os.link(stage / 'bundle-manifest.json', output / 'bundle-manifest.json')
        _sync_directory(output)
        _sync_directory(output.parent)
    return dict(output=str(output), manifest_sha256=hashlib.sha256(raw).hexdigest(),
                audio_runtime={**report, 'root': str(output / DIRECTORY)},
                native_windows_execution='NOT_RUN')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--audio-runtime', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(attach_runtime(args.bundle, args.audio_runtime, args.output), indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
