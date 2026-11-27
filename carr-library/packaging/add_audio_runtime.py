"""Attach the reviewed local FFmpeg payload to a new, sealed Windows bundle.

Only caller-supplied, verified files are copied. No network or native program is
used, and source installation files, model data and user data are never changed.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
from carr_library import audio_runtime as runtime
from carr_library.native_runtime import _absolute, _directory, _entry, _plain, _unique


def _manifest_snapshot(path):
    info = _plain(path)
    limit = 8 * 1024 * 1024
    if not 0 < info.st_size <= limit:
        raise ValueError('Installation manifest exceeds its bounded size.')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) != info.st_size:
        raise ValueError('Installation manifest changed while being read.')
    value = json.loads(raw, object_pairs_hook=_unique)
    if type(value) is not dict:
        raise ValueError('Expected an installation manifest object.')
    return raw, value


def _installer():
    spec = importlib.util.spec_from_file_location('carr_audio_bundle_installer', HERE / 'install_windows.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _copy_file(source, target, expected):
    info = _plain(source)
    _directory(source.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest, count = hashlib.sha256(), 0
    with source.open('rb') as incoming, target.open('xb') as outgoing:
        while chunk := incoming.read(1024 * 1024):
            count += len(chunk)
            if count > info.st_size:
                raise ValueError('Source bundle file grew during copy.')
            digest.update(chunk)
            outgoing.write(chunk)
    if count != info.st_size or digest.hexdigest() != expected:
        raise ValueError('Source bundle or audio file changed while being copied.')


def _directory_identity(path):
    info = _plain(path, directory=True)
    return info.st_dev, info.st_ino


def _owned_directory(path, identity):
    try:
        _directory(path)
        return _directory_identity(path) == identity
    except (OSError, ValueError):
        return False


def attach_runtime(bundle, audio_runtime, output):
    """Publish a fresh bundle with its acceptance manifest written last."""
    bundle, audio_runtime, output = map(_absolute, (bundle, audio_runtime, output))
    _directory(bundle)
    _directory(audio_runtime)
    _directory(output.parent)
    if os.path.lexists(output):
        raise FileExistsError('Use a new output folder.')
    for source in (bundle, audio_runtime):
        if output.is_relative_to(source) or source.is_relative_to(output):
            raise ValueError('The output must be outside both source folders.')
    manifest_path = bundle / 'bundle-manifest.json'
    original, manifest = _manifest_snapshot(manifest_path)
    installer = _installer()
    installer.verify(bundle)
    if _manifest_snapshot(manifest_path)[0] != original:
        raise ValueError('Source installation manifest changed during verification.')
    if (manifest.get('bundled_python') is not True or manifest.get('bundled_native_runtime') is not True
            or manifest.get('target') != 'Windows x64 / CPython 3.11'
            or not all(isinstance(manifest.get(field), str)
                       and re.fullmatch('[0-9a-f]{40}', manifest[field])
                       for field in ('source_commit', 'source_tree'))):
        raise ValueError('The source must be a native-supplemented bundle with exact source identity.')
    if (runtime.bundled_audio_tools(bundle) is not None or manifest.get('bundled_audio_runtime', False)
            or os.path.lexists(bundle / runtime.DIRECTORY)):
        raise ValueError('The source bundle already contains an audio runtime.')
    for name, digest in manifest['files'].items():
        _entry(name, digest)
    if manifest['files']['install_windows.py'] != hashlib.sha256((HERE / 'install_windows.py').read_bytes()).hexdigest():
        raise ValueError('Source bundle installer differs from the packaging source.')
    applications = [name for name in manifest['files']
                    if name.startswith('wheelhouse/universal_research_library-') and name.endswith('.whl')]
    if len(applications) != 1:
        raise ValueError('Expected one application wheel.')
    with ZipFile(bundle / applications[0]) as wheel:
        name = 'carr_library/audio_runtime.py'
        if name not in wheel.namelist() or wheel.read(name) != (HERE.parent / 'src' / name).read_bytes():
            raise ValueError('Application wheel lacks the current reviewed audio runtime verifier.')
    audio_report = runtime.verify_audio_runtime(audio_runtime)
    inner_raw, inner = _manifest_snapshot(audio_runtime / runtime.MANIFEST)
    if hashlib.sha256(inner_raw).hexdigest() != audio_report['manifest_sha256']:
        raise ValueError('Audio runtime manifest changed after verification.')
    audio_files = dict(inner['files'], **{runtime.MANIFEST: audio_report['manifest_sha256']})
    with TemporaryDirectory(prefix='carr-audio-bundle-', dir=output.parent) as scratch:
        stage = Path(scratch) / 'bundle'
        stage.mkdir()
        # Environments, caches, databases and other unsealed local files are not
        # installation inputs and must never be collected into a release.
        for name, expected in manifest['files'].items():
            _copy_file(bundle / name, stage / name, expected)
        for name, expected in audio_files.items():
            _entry(name, expected)
            _copy_file(audio_runtime / name, stage / runtime.DIRECTORY / name, expected)
        copied = runtime.verify_audio_runtime(stage / runtime.DIRECTORY)
        if {k: v for k, v in copied.items() if k != 'tools'} != {k: v for k, v in audio_report.items() if k != 'tools'}:
            raise ValueError('Copied audio runtime differs from its verified source.')
        if _manifest_snapshot(manifest_path)[0] != original:
            raise ValueError('Source installation manifest changed during copy.')
        manifest['bundled_audio_runtime'] = True
        manifest['files'].update({runtime.DIRECTORY + '/' + name: digest for name, digest in audio_files.items()})
        raw = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode('utf-8')
        (stage / 'bundle-manifest.json').write_bytes(raw)
        installer.verify(stage)
        runtime.bundled_audio_tools(stage)
        output.mkdir(exist_ok=False)
        output_identity = _directory_identity(output)

        def publish(path):
            if not _owned_directory(output, output_identity):
                raise ValueError('Audio bundle output directory changed during publication.')
            shutil.move(str(path), output / path.name)
            if not _owned_directory(output, output_identity):
                raise ValueError('Audio bundle output directory changed during publication.')

        try:
            for path in stage.iterdir():
                if path.name != 'bundle-manifest.json':
                    publish(path)
            publish(stage / 'bundle-manifest.json')
        except BaseException:
            # Match the delivery helper's ownership rule: a pathname can now
            # belong to another operation, even though this call created it.
            if _owned_directory(output, output_identity):
                shutil.rmtree(output)
            raise
    audio_report['tools'] = {name: str(output / runtime.DIRECTORY / path) for name, path in runtime.TOOLS.items()}
    return {'output': str(output), 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
            'source_manifest_sha256': hashlib.sha256(original).hexdigest(),
            'source_commit': manifest['source_commit'], 'source_tree': manifest['source_tree'],
            'base_files': len(manifest['files']) - len(audio_files), 'audio_files': len(audio_files),
            'audio_runtime': audio_report, 'native_windows_execution': 'NOT_RUN'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--audio-runtime', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(attach_runtime(args.bundle, args.audio_runtime, args.output), indent=2))


if __name__ == '__main__':
    main()
