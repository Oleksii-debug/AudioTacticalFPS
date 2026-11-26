"""Audio runtime sealing, tamper rejection and installer inventory with actual files."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path

import pytest

from carr_library.audio import runtime


ROOT = Path(__file__).resolve().parents[2]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'packaging' / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    from zipfile import ZipFile
    wheels = tmp_path / 'wheels'; wheels.mkdir()
    def wheel(folder, name, body, requires=''):
        path = folder / (name + '-1.0-py3-none-any.whl')
        with ZipFile(path, 'w') as archive:
            archive.writestr(name + '-1.0.dist-info/METADATA',
                f'Metadata-Version: 2.3\nName: {name}\nVersion: 1.0\n' + requires)
            archive.writestr(body, '# synthetic package\n')
        return path
    dependency = wheel(wheels, 'fixture', 'fixture/__init__.py')
    app = wheel(tmp_path, 'universal_research_library', 'carr_library/__main__.py',
                'Requires-Dist: fixture==1.0\n')
    lock = tmp_path / 'test.lock'
    lock.write_text('fixture==1.0 --hash=sha256:' + sha256(dependency.read_bytes()).hexdigest() + '\n')
    audio = tmp_path / 'audio'; audio.mkdir()
    members = {}
    for name in runtime.ARCHIVE_MEMBERS:
        raw = ('Synthetic reviewed runtime fixture: ' + name).encode()
        (audio / name).parent.mkdir(parents=True, exist_ok=True)
        (audio / name).write_bytes(raw)
        members[name] = (name, len(raw), sha256(raw).hexdigest())
    monkeypatch.setattr(runtime, 'ARCHIVE_MEMBERS', members)
    manifest = {'schema': 1, 'archive_sha256': runtime.FFMPEG_ARCHIVE_SHA256,
                'source_commit': runtime.FFMPEG_SOURCE_COMMIT,
                'files': {name: {'bytes': item[1], 'sha256': item[2]} for name, item in members.items()}}
    (audio / 'ffmpeg-runtime.json').write_text(json.dumps(manifest))
    return load('build_windows_bundle'), load('install_windows'), app, wheels, lock, audio, tmp_path / 'built'


def build(fixture):
    builder, _, app, wheels, lock, audio, output = fixture
    builder.build_bundle(app, wheels, lock, output, audio_dir=audio)
    return output


def test_sealed_audio_runtime_and_notices_are_installed_with_exact_inventory(bundle):
    output = build(bundle)
    bundle[1].verify(output)
    manifest = json.loads((output / 'bundle-manifest.json').read_text())
    assert manifest['bundled_audio_runtime'] is True
    assert (output / 'audio-runtime/ffmpeg.exe').read_bytes() == (bundle[5] / 'ffmpeg.exe').read_bytes()
    assert runtime.verify_runtime(output / 'audio-runtime')['files']
    for name in runtime.ARCHIVE_MEMBERS:
        assert (output / 'audio-runtime' / name).read_bytes() == (bundle[5] / name).read_bytes()


def test_missing_original_build_documentation_rejects_runtime_before_copy(bundle):
    (bundle[5] / 'doc/ffmpeg.html').unlink()
    with pytest.raises(ValueError, match='integrity'):
        build(bundle)
    assert not bundle[6].exists()


def test_runtime_corruption_fails_before_output_publication(bundle):
    (bundle[5] / 'ffmpeg.exe').write_bytes(b'Unapproved executable')
    with pytest.raises(ValueError):
        build(bundle)
    assert not bundle[6].exists()


def test_runtime_changed_during_copy_cannot_be_sealed(bundle, monkeypatch):
    builder = bundle[0]
    original = builder.shutil.copyfile
    def replace_audio(source, target, *args, **kwargs):
        result = original(source, target, *args, **kwargs)
        if Path(target).name == 'ffmpeg.exe':
            Path(target).write_bytes(b'Altered during staging')
        return result
    monkeypatch.setattr(builder.shutil, 'copyfile', replace_audio)
    with pytest.raises(ValueError):
        build(bundle)
    assert not bundle[6].exists()


def test_installer_rejects_extra_and_unannounced_audio_payloads(bundle):
    output = build(bundle)
    extra = output / 'audio-runtime/unapproved.dll'
    extra.write_bytes(b'not admitted')
    with pytest.raises(ValueError, match='unexpected or missing audio'):
        bundle[1].verify(output)
    extra.unlink()
    path = output / 'bundle-manifest.json'
    manifest = json.loads(path.read_text()); manifest['bundled_audio_runtime'] = False
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='unannounced audio'):
        bundle[1].verify(output)
