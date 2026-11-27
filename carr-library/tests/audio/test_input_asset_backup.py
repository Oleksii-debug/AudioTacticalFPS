"""Real PCM inputs survive project and library snapshots independent of suffix."""
from hashlib import sha256
import json
from pathlib import Path
import struct
import wave
import zipfile

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.backup import backup_project, restore_project
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.models import Sound
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.library_backup import create_backup, restore_backup
from carr_library.ui.audio_studio import StudioController


def _wav(path, amplitude=2500):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
        stream.writeframes(b''.join(struct.pack('<h', amplitude if i % 20 < 10 else -amplitude)
                                   for i in range(8000)))
    assert wav_info(path).frames == 8000
    return path.read_bytes()


def _script(name):
    return '[output sample_rate=8000 stems=false]\n[sound "' + name + '"]'


def _saved(tmp_path, *, managed=False, name='EFFECT.WAV', input_kind='script'):
    app = LibraryApp(tmp_path / 'library') if managed else None
    private = app.root if app else tmp_path / 'private'
    studio = StudioController(private_root=private)
    studio.new(_script(name), title='Portable input sounds', input_kind=input_kind)
    if managed:
        source = studio.save_in_library()
    else:
        output = tmp_path / 'audio'
        output.mkdir()
        studio.configure(output_dir=output)
        source = studio.save(tmp_path / 'studio.json')
    return app, studio, source


@pytest.mark.parametrize('managed', [False, True], ids=['project-zip', 'library-backup'])
def test_saved_and_recovery_input_bytes_survive_new_root_without_unrelated_files(tmp_path, managed):
    app, studio, source = _saved(tmp_path, managed=managed)
    root = studio.output_dir
    original = {'EFFECT.WAV': _wav(root / 'assets' / 'EFFECT.WAV'),
                'effect.bin': _wav(root / 'assets' / 'effect.bin', amplitude=1700)}
    # A recovery-only sound input is just as important as the saved script.
    studio.edit_script(_script('effect.bin'))
    recovery = studio.save_recovery()
    saved_bytes, recovery_bytes = source.read_bytes(), recovery.read_bytes()
    (root / 'assets' / 'private.json').write_text('{"token":"DO NOT COPY"}', encoding='utf-8')
    (root / 'assets' / 'notes.txt').write_text('not audio', encoding='utf-8')
    _wav(root / 'assets' / 'unreferenced.bin')
    _wav(root / 'cache' / ('a' * 64 + '.wav'))

    if managed:
        snapshot = tmp_path / 'backup'
        digest = create_backup(app, snapshot)
        restored = restore_backup(snapshot, tmp_path / 'restored', digest)
        project = restored / source.relative_to(app.root)
        reopened = StudioController(private_root=restored).open(project)
        assert project.read_bytes() == saved_bytes
        assert project.with_name(project.name + '.recovery.json').read_bytes() == recovery_bytes
    else:
        snapshot = tmp_path / 'snapshot.zip'
        backup_project(source, snapshot, project_dir=root, include_cache=False, include_audio=False)
        with zipfile.ZipFile(snapshot) as archive:
            assert not any(name.startswith(('audio/cache/', 'audio/renders/')) for name in archive.namelist())
        restored = restore_project(snapshot, tmp_path / 'restored')
        reopened = StudioController(private_root=tmp_path / 'other-private').open(restored['project_file'])

    assert reopened.output_dir != root
    assert reopened.output_dir.is_relative_to(tmp_path / 'restored')
    assert {path.name for path in (reopened.output_dir / 'assets').iterdir()} == set(original)
    assert {name: (reopened.output_dir / 'assets' / name).read_bytes() for name in original} == original
    assert reopened.script == _script('EFFECT.WAV')
    reopened.recover()
    assert reopened.script == _script('effect.bin')
    assert source.read_bytes() == saved_bytes and recovery.read_bytes() == recovery_bytes
    assert {name: (root / 'assets' / name).read_bytes() for name in original} == original


@pytest.mark.parametrize('name', ['EFFECT.WAV', 'effect.bin'])
@pytest.mark.parametrize('managed', [False, True], ids=['project-zip', 'library-backup'])
@pytest.mark.parametrize('damage', ['missing', 'truncated'])
def test_missing_or_damaged_referenced_input_prevents_completed_backup(tmp_path, name, managed, damage):
    app, studio, source = _saved(tmp_path, managed=managed, name=name)
    asset = studio.output_dir / 'assets' / name
    if damage == 'truncated':
        raw = _wav(asset)
        asset.write_bytes(raw[:-2])
    target = tmp_path / ('backup' if managed else 'snapshot.zip')
    with pytest.raises(ValueError):
        if managed:
            create_backup(app, target)
        else:
            backup_project(source, target, project_dir=studio.output_dir,
                           include_cache=False, include_audio=False)
    assert not target.exists()


def test_required_input_is_not_silently_omitted_when_audio_folder_was_not_selected(tmp_path):
    _, studio, source = _saved(tmp_path)
    _wav(studio.output_dir / 'assets' / 'EFFECT.WAV')
    with pytest.raises(ValueError, match='audio project folder'):
        backup_project(source, tmp_path / 'snapshot.zip')
    assert not (tmp_path / 'snapshot.zip').exists()


def test_literal_narration_does_not_treat_bracketed_text_as_an_input_reference(tmp_path):
    _, studio, source = _saved(tmp_path, name='missing.bin', input_kind='narration')
    backup_project(source, tmp_path / 'snapshot.zip', project_dir=studio.output_dir,
                   include_cache=False, include_audio=False)
    result = restore_project(tmp_path / 'snapshot.zip', tmp_path / 'restored')
    reopened = StudioController(private_root=tmp_path / 'other-private').open(result['project_file'])
    assert reopened.script == studio.script and reopened.input_kind == 'narration'


@pytest.mark.parametrize('damage', ['truncated', 'unreferenced'])
def test_resigned_archives_cannot_publish_damaged_or_unrelated_input_assets(tmp_path, damage):
    _, studio, source = _saved(tmp_path, name='effect.bin')
    _wav(studio.output_dir / 'assets' / 'effect.bin')
    snapshot = tmp_path / 'snapshot.zip'
    backup_project(source, snapshot, project_dir=studio.output_dir, include_cache=False, include_audio=False)
    with zipfile.ZipFile(snapshot) as incoming:
        contents = {name: incoming.read(name) for name in incoming.namelist()}
    manifest = json.loads(contents['manifest.json'])
    name = 'audio/assets/effect.bin'
    if damage == 'truncated':
        contents[name] = contents[name][:-2]
        record = next(row for row in manifest['files'] if row['path'] == name)
    else:
        name = 'audio/assets/private.json'
        contents[name] = b'{"token":"DO NOT RESTORE"}'
        record = {'path': name}
        manifest['files'].append(record)
    record.update(size=len(contents[name]), sha256=sha256(contents[name]).hexdigest())
    manifest['total_bytes'] = sum(row['size'] for row in manifest['files'])
    contents['manifest.json'] = json.dumps(manifest).encode('utf-8')
    forged = tmp_path / 'resigned.zip'
    with zipfile.ZipFile(forged, 'w') as outgoing:
        for member, raw in contents.items():
            outgoing.writestr(member, raw)
    with pytest.raises(ValueError):
        restore_project(forged, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('name', ['EFFECT.WAV', 'effect.bin'])
def test_restored_input_runs_through_real_ffmpeg_without_original_assets(tmp_path, name):
    ffmpeg = resolve_ffmpeg()
    if ffmpeg is None:
        pytest.skip('A real local FFmpeg executable is required for sound-input render verification.')
    _, studio, source = _saved(tmp_path, name=name)
    original = studio.output_dir / 'assets' / name
    _wav(original)
    plan = studio.plan()
    assert isinstance(plan.scenes[0].events[0], Sound)
    assert plan.scenes[0].events[0].asset == name
    snapshot = tmp_path / 'snapshot.zip'
    backup_project(source, snapshot, project_dir=studio.output_dir, include_cache=False, include_audio=False)
    restored = restore_project(snapshot, tmp_path / 'restored')
    original.unlink()
    reopened_app = LibraryApp(tmp_path / 'other-private')
    reopened = StudioController(private_root=reopened_app.root, ffmpeg=ffmpeg)
    reopened.open(restored['project_file'])
    result = reopened.render()
    output = Path(result['output'])
    assert output.is_relative_to(Path(restored['project_dir']))
    assert result['status'] == 'complete' and result['frames'] == 8000
    with wave.open(str(output), 'rb') as stream:
        assert any(stream.readframes(stream.getnframes()))


def test_resigned_library_cannot_restore_orphan_inputs_without_their_project(tmp_path):
    app, studio, source = _saved(tmp_path, managed=True)
    raw = _wav(studio.output_dir / 'assets' / 'EFFECT.WAV')
    snapshot = tmp_path / 'backup'
    create_backup(app, snapshot)
    identity = 'f' * 32 if studio.project_id != 'f' * 32 else 'e' * 32
    name = 'audio-projects/' + identity + '/audio/assets/orphan.bin'
    orphan = snapshot / 'sidecars' / name
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(raw)
    manifest_path = snapshot / 'manifest.json'
    manifest = json.loads(manifest_path.read_bytes())
    manifest['sidecars'].append({'path': name, 'size_bytes': len(raw),
                                'sha256': sha256(raw).hexdigest()})
    encoded = (json.dumps(manifest, sort_keys=True) + '\n').encode('utf-8')
    manifest_path.write_bytes(encoded)
    with pytest.raises(ValueError, match='saved Studio project'):
        restore_backup(snapshot, tmp_path / 'rejected', sha256(encoded).hexdigest())
    assert not (tmp_path / 'rejected').exists()
