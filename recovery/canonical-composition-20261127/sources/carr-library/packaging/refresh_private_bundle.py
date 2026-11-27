"""Refresh a verified owner's private offline bundle into a new directory.

Reuse already acquired locked dependencies/models, verify the new application
against its exact Git commit, and copy explicitly selected local runtimes.
Canonical audio-runtime and the verified legacy audio layout are alternatives.
Omitted optional runtimes are not inherited into the new program directory.
No downloads, installers, public release, or changes to the original bundle.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory

from build_windows_bundle import (read_lock, wheel_metadata, verify_application_source,
                                  verify_dependency_closure, sha256, copy_audio_setup)
from install_windows import verify
from prepare_windows_audio import regular, local
from optional_runtimes import admit, copy_admitted
from create_delivery import _read_manifest, _manifest

HERE = Path(__file__).resolve().parent


def refresh(source, wheel, output, source_commit, *, parent_manifest_sha256,
            audio=None, audio_runtime=None, citations=None, extra_wheelhouse=None):
    if audio is not None and audio_runtime is not None:
        raise ValueError('Choose either the canonical audio runtime or the verified legacy audio layout.')
    source, wheel, output = local(source, exists=True), local(wheel, exists=True), local(output)
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Choose a new bundle directory outside the previous one.')
    if (not isinstance(parent_manifest_sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', parent_manifest_sha256)):
        raise ValueError('An independently recorded parent manifest SHA256 is required.')
    raw = _read_manifest(source, lambda: False)
    if hashlib.sha256(raw).hexdigest() != parent_manifest_sha256:
        raise ValueError('The parent bundle manifest differs from its recorded SHA256.')
    previous = _manifest(raw, json.loads(raw).get('source_commit'))
    verify(source, manifest_bytes=raw)
    identity = verify_application_source(wheel, source_commit)
    lock = read_lock(HERE / ('windows-semantic-cp311.lock' if previous.get('bundled_model') else 'windows-cp311.lock'))
    dependencies = {}
    selected = {}
    wheelhouses = [source / 'wheelhouse']
    if extra_wheelhouse is not None:
        wheelhouses.append(local(extra_wheelhouse, exists=True))
    for directory in wheelhouses:
        regular(directory, directory=True)
        for path in directory.iterdir():
            regular(path)
            if path.suffix != '.whl':
                raise ValueError('A wheelhouse must contain only regular wheel files.')
            name, version, metadata = wheel_metadata(path)
            if name == 'universal-research-library' and directory == wheelhouses[0]:
                continue
            digest = sha256(path)
            if name not in lock or str(version) != lock[name][0] or digest not in lock[name][1]:
                raise ValueError('A dependency does not match the current lock: ' + name)
            if name in dependencies:
                raise ValueError('More than one wheel supplied for dependency: ' + name)
            dependencies[name] = (path, digest)
            selected[name] = (path, version, metadata)
    if set(dependencies) != set(lock):
        raise ValueError('The private bundle and extra wheelhouse lack required locked dependencies.')
    name, version, metadata = wheel_metadata(wheel)
    if name != 'universal-research-library' or name in selected:
        raise ValueError('Expected exactly one application wheel.')
    selected[name] = (wheel, version, metadata)
    verify_dependency_closure(selected)
    sbom_path = HERE / ('windows-semantic-sbom.cdx.json' if previous.get('bundled_model') else 'windows-sbom.cdx.json')
    sbom_bytes = sbom_path.read_bytes()
    sbom = json.loads(sbom_bytes)
    components = sbom.get('components', [])
    if (sbom.get('bomFormat') != 'CycloneDX' or sbom.get('specVersion') != '1.5'
            or len(components) != len(lock)
            or {(item['name'], item['version']) for item in components}
            != {(name, version) for name, (version, _) in lock.items()}):
        raise ValueError('The current runtime SBOM differs from the dependency lock.')
    # Capture the existing legacy contracts once; their provenance bytes are
    # copied from the admitted snapshot, never re-read after checking inputs.
    legacy_audio = admit(audio, 'audio', locks=HERE)
    legacy_citations = admit(citations, 'citation-runtime', locks=HERE)
    canonical_audio = None
    if audio_runtime is not None:
        from carr_library import audio_runtime as canonical_runtime
        audio_runtime = local(audio_runtime, exists=True)
        report, files = canonical_runtime._inspect_audio_runtime(audio_runtime)
        canonical_audio = (canonical_runtime, report,
            {**files, canonical_runtime.MANIFEST: report['manifest_sha256']})
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.private-bundle-', dir=output.parent) as temporary:
        stage = Path(temporary)
        for name, expected in previous['files'].items():
            if (name.casefold().startswith(('audio/', 'audio-runtime/', 'citation-runtime/', 'tools/audio-setup/'))
                    or name.casefold() == 'setuplocalaudio.cmd'):
                continue
            if name.startswith('wheelhouse/'):
                continue
            origin = local(source / name, exists=True)
            regular(origin)
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
            if sha256(target) != expected:
                raise ValueError('The old bundle changed during refresh.')
        (stage / 'wheelhouse').mkdir(exist_ok=True)
        for path, expected in dependencies.values():
            target = stage / 'wheelhouse' / path.name
            shutil.copyfile(path, target)
            if sha256(target) != expected:
                raise ValueError('A locked dependency changed during private bundle assembly.')
        shutil.copyfile(wheel, stage / 'wheelhouse' / wheel.name)
        if sha256(stage / 'wheelhouse' / wheel.name) != identity['wheel_sha256']:
            raise ValueError('Application wheel changed after exact source verification.')
        requirements = []
        for path in sorted((stage / 'wheelhouse').iterdir()):
            name, version, _ = wheel_metadata(path)
            requirements.append(f'{name}=={version} --hash=sha256:{sha256(path)}')
        (stage / 'install.lock').write_text('\n'.join(requirements) + '\n', encoding='utf-8')
        for name in ('install_windows.py', 'Install.cmd', 'ResearchLibrary.cmd', 'CheckInstallation.cmd'):
            shutil.copyfile(HERE / name, stage / name)
        copy_audio_setup(stage, packaging_root=HERE)
        shutil.copyfile(HERE.parent / 'docs' / 'RUNNING.md', stage / 'README.txt')
        shutil.copyfile(HERE.parent / 'docs' / 'START_UK.txt', stage / 'START_UK.txt')
        shutil.copyfile(HERE.parent / 'docs' / 'START_V3_UK.txt', stage / 'START_V3_UK.txt')
        (stage / 'runtime-sbom.cdx.json').write_bytes(sbom_bytes)
        for origin in (HERE / 'wheel-notices').iterdir():
            regular(origin)
            target = stage / 'notices' / 'wheels' / origin.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
        copy_admitted(legacy_audio, stage)
        copy_admitted(legacy_citations, stage)
        if canonical_audio is not None:
            runtime, report, files = canonical_audio
            destination = stage / runtime.DIRECTORY
            destination.mkdir(exist_ok=False)
            for name, expected in files.items():
                origin = local(audio_runtime / name, exists=True)
                regular(origin)
                target = destination / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, target)
                if sha256(target) != expected:
                    raise ValueError('The canonical audio runtime changed during private bundle assembly.')
            copied, copied_files = runtime._inspect_audio_runtime(destination)
            if ({key: value for key, value in copied.items() if key != 'tools'}
                    != {key: value for key, value in report.items() if key != 'tools'}
                    or {**copied_files, runtime.MANIFEST: copied['manifest_sha256']} != files):
                raise ValueError('The copied canonical audio runtime differs from its admitted identity.')
        (stage / 'application-source.json').write_text(json.dumps(identity, indent=2) + '\n', encoding='utf-8')
        manifest = {key: value for key, value in previous.items()
                    if key not in ('files', 'source_commit', 'source_tree', 'bundled_audio',
                                   'bundled_audio_runtime', 'bundled_citation_runtime')}
        manifest.update(source_commit=source_commit, source_tree=identity['source_tree'],
                        bundled_audio=legacy_audio is not None,
                        bundled_audio_runtime=canonical_audio is not None,
                        bundled_citation_runtime=legacy_citations is not None, use_scope='private-personal-installation',
                        parent_source_commit=previous['source_commit'],
                        parent_manifest_sha256=parent_manifest_sha256,
                        files={p.relative_to(stage).as_posix(): sha256(p) for p in sorted(stage.rglob('*')) if p.is_file()})
        raw = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode('utf-8')
        # Keep the complete source/inventory namespace contract on refresh too.
        _manifest(raw, source_commit)
        (stage / 'bundle-manifest.json').write_bytes(raw)
        verify(stage, manifest_bytes=raw)
        output.mkdir(mode=0o700)
        for path in stage.iterdir():
            if path.name != 'bundle-manifest.json':
                shutil.move(str(path), output / path.name)
        shutil.move(str(stage / 'bundle-manifest.json'), output / 'bundle-manifest.json')
    return {'output': str(output), 'source_commit': source_commit,
            'manifest_sha256': sha256(output / 'bundle-manifest.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('source', 'wheel', 'output', 'source-commit', 'parent-manifest-sha256'):
        parser.add_argument('--' + argument, required=True)
    audio_choice = parser.add_mutually_exclusive_group()
    audio_choice.add_argument('--audio', help='Explicit verified legacy audio layout.')
    audio_choice.add_argument('--audio-runtime', help='Explicit canonical prepared audio-runtime directory.')
    parser.add_argument('--citations')
    parser.add_argument('--extra-wheelhouse')
    args = parser.parse_args()
    print(json.dumps(refresh(args.source, args.wheel, args.output, args.source_commit,
                             parent_manifest_sha256=args.parent_manifest_sha256,
                             audio=args.audio, audio_runtime=args.audio_runtime, citations=args.citations,
                             extra_wheelhouse=args.extra_wheelhouse)))


if __name__ == '__main__':
    main()
