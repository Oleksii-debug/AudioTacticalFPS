"""Refresh a verified owner's private offline bundle into a new directory.

Reuse already acquired locked dependencies/models, verify the new application
against its exact Git commit, and copy explicit local audio/citation runtimes.
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
                                  verify_dependency_closure, verify_native_launcher, sha256)
from install_windows import verify
from prepare_windows_audio import load_lock, regular, local, _pe64
from create_delivery import _read_manifest, _manifest

HERE = Path(__file__).resolve().parent


def refresh(source, wheel, output, source_commit, *, parent_manifest_sha256,
            audio=None, citations=None, extra_wheelhouse=None, launcher_dir=None):
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
    launcher_payload = None
    if launcher_dir is not None or previous.get("bundled_launcher"):
        if not previous.get("bundled_python"):
            raise ValueError("native launcher requires the complete bundled Python runtime")
        launcher_payload = verify_native_launcher(
            launcher_dir if launcher_dir is not None else source, source_commit,
            receipt_name="build-evidence.json" if launcher_dir is not None else "native-launcher-build.json")
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
    audio_files = {}
    if audio is not None:
        audio = local(audio, exists=True)
        audio_lock, _ = load_lock(HERE / 'windows-audio.lock.json')
        for name, record in audio_lock['output_files'].items():
            path = local(audio / name, exists=True)
            if regular(path).st_size != record['size'] or sha256(path) != record['sha256']:
                raise ValueError('Audio runtime differs from the pinned build.')
            if path.suffix == '.exe':
                _pe64(path)
            audio_files[name] = path
    node_files = {}
    if citations is not None:
        citations = local(citations, exists=True)
        node_lock = json.loads((HERE / 'windows-citations.lock.json').read_bytes())
        for name, expected in node_lock['files'].items():
            path = local(citations / name, exists=True)
            regular(path)
            if sha256(path) != expected:
                raise ValueError('Citation runtime differs from the pinned Node build.')
            node_files[name] = path
        _pe64(node_files['node.exe'])
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.private-bundle-', dir=output.parent) as temporary:
        stage = Path(temporary)
        for name, expected in previous['files'].items():
            if name in {"ResearchLibrary.exe", "native-launcher-build.json"}:
                continue
            if name.startswith(('audio/', 'citation-runtime/')):
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
        shutil.copyfile(HERE.parent / 'docs' / 'RUNNING.md', stage / 'README.txt')
        (stage / 'docs').mkdir(exist_ok=True)
        for name in ('TRUSTED_CLIENT.md', 'STUDIO_CLIP_EDITING.md', 'MICROPHONE_RECORDING.md',
                     'STUDIO_EPUB_PUBLISHING.md', 'STUDIO_DAISY_PUBLISHING.md', 'EPUB_RECORDED_READING.md',
                     'PRONUNCIATION_REVIEW.md', 'AUDIOBOOK_CHAPTERS.md', 'runtime_resources.md', 'translation_workstation.md', 'BOOK_AUDIO_BATCH.md'):
            shutil.copyfile(HERE.parent / 'docs' / name, stage / 'docs' / name)
        shutil.copyfile(HERE.parent / 'docs' / 'START_V3_UK.txt', stage / 'START_V3_UK.txt')
        (stage / 'runtime-sbom.cdx.json').write_bytes(sbom_bytes)
        for origin in (HERE / 'wheel-notices').iterdir():
            regular(origin)
            target = stage / 'notices' / 'wheels' / origin.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
        for directory, files in (('audio', audio_files), ('citation-runtime', node_files)):
            for name, origin in files.items():
                target = stage / directory / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(origin, target)
                expected = (audio_lock['output_files'][name]['sha256'] if directory == 'audio'
                            else node_lock['files'][name])
                if sha256(target) != expected:
                    raise ValueError('A local runtime changed during private bundle assembly.')
            if files:
                lock_name = 'windows-audio.lock.json' if directory == 'audio' else 'windows-citations.lock.json'
                shutil.copyfile(HERE / lock_name, stage / directory / 'provenance.json')
        if launcher_payload is not None:
            (stage / "ResearchLibrary.exe").write_bytes(launcher_payload[0])
            (stage / "native-launcher-build.json").write_bytes(launcher_payload[1])
        (stage / 'application-source.json').write_text(json.dumps(identity, indent=2) + '\n', encoding='utf-8')
        manifest = {key: value for key, value in previous.items() if key not in ('files', 'source_commit')}
        manifest.update(source_commit=source_commit, source_tree=identity['source_tree'],
                        bundled_launcher=launcher_payload is not None,
                        bundled_audio=bool(audio_files),
                        bundled_citation_runtime=bool(node_files), use_scope='private-personal-installation',
                        parent_source_commit=previous['source_commit'],
                        parent_manifest_sha256=parent_manifest_sha256,
                        files={p.relative_to(stage).as_posix(): sha256(p) for p in sorted(stage.rglob('*')) if p.is_file()})
        (stage / 'bundle-manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        verify(stage)
        output.mkdir(mode=0o700)
        for path in stage.iterdir():
            shutil.move(str(path), output / path.name)
    return {'output': str(output), 'source_commit': source_commit,
            'manifest_sha256': sha256(output / 'bundle-manifest.json')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for argument in ('source', 'wheel', 'output', 'source-commit', 'parent-manifest-sha256'):
        parser.add_argument('--' + argument, required=True)
    parser.add_argument('--audio')
    parser.add_argument('--citations')
    parser.add_argument('--extra-wheelhouse')
    parser.add_argument('--launcher-dir', help='Optional new exact-source native launcher build')
    args = parser.parse_args()
    print(json.dumps(refresh(args.source, args.wheel, args.output, args.source_commit,
                             parent_manifest_sha256=args.parent_manifest_sha256,
                             audio=args.audio, citations=args.citations,
                             extra_wheelhouse=args.extra_wheelhouse, launcher_dir=args.launcher_dir)))


if __name__ == '__main__':
    main()
