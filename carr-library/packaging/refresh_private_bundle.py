"""Refresh a verified owner's private offline bundle into a new directory.

Reuse already acquired locked dependencies/models, verify the new application
against its exact Git commit, and copy explicit local audio/citation runtimes.
No downloads, installers, public release, or changes to the original bundle.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from build_windows_bundle import read_lock, wheel_metadata, verify_application_source, sha256
from install_windows import verify
from prepare_windows_audio import load_lock, regular, local, _pe64

HERE = Path(__file__).resolve().parent


def refresh(source, wheel, output, source_commit, *, audio=None, citations=None):
    source, wheel, output = local(source, exists=True), local(wheel, exists=True), local(output)
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Choose a new bundle directory outside the previous one.')
    verify(source)
    previous = json.loads((source / 'bundle-manifest.json').read_bytes())
    identity = verify_application_source(wheel, source_commit)
    lock = read_lock(HERE / ('windows-semantic-cp311.lock' if previous.get('bundled_model') else 'windows-cp311.lock'))
    dependencies = {}
    for path in (source / 'wheelhouse').iterdir():
        name, version, _ = wheel_metadata(path)
        if name == 'universal-research-library':
            continue
        if name not in lock or str(version) != lock[name][0] or sha256(path) not in lock[name][1]:
            raise ValueError('The prior dependency no longer matches the current lock: ' + name)
        dependencies[name] = path
    if set(dependencies) != set(lock):
        raise ValueError('The prior private bundle is missing required locked dependencies.')
    audio_files = {}
    if audio is not None:
        audio = local(audio, exists=True)
        audio_lock, _ = load_lock(HERE / 'windows-audio.lock.json')
        for name, record in audio_lock['output_files'].items():
            path = audio / name
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
            path = citations / name
            regular(path)
            if sha256(path) != expected:
                raise ValueError('Citation runtime differs from the pinned Node build.')
            node_files[name] = path
        _pe64(node_files['node.exe'])
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix='.private-bundle-', dir=output.parent) as temporary:
        stage = Path(temporary)
        for name, expected in previous['files'].items():
            if name.startswith(('audio/', 'citation-runtime/')):
                continue
            if name.startswith('wheelhouse/') and name not in {'wheelhouse/' + p.name for p in dependencies.values()}:
                continue
            origin = local(source / name, exists=True)
            regular(origin)
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin, target)
            if sha256(target) != expected:
                raise ValueError('The old bundle changed during refresh.')
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
        shutil.copyfile(HERE.parent / 'docs' / 'START_V3_UK.txt', stage / 'START_V3_UK.txt')
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
        (stage / 'application-source.json').write_text(json.dumps(identity, indent=2) + '\n', encoding='utf-8')
        manifest = {key: value for key, value in previous.items() if key not in ('files', 'source_commit')}
        manifest.update(source_commit=source_commit, bundled_audio=bool(audio_files),
                        bundled_citation_runtime=bool(node_files), use_scope='private-personal-installation',
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
    for argument in ('source', 'wheel', 'output', 'source-commit'):
        parser.add_argument('--' + argument, required=True)
    parser.add_argument('--audio')
    parser.add_argument('--citations')
    args = parser.parse_args()
    print(json.dumps(refresh(args.source, args.wheel, args.output, args.source_commit,
                             audio=args.audio, citations=args.citations)))


if __name__ == '__main__':
    main()
