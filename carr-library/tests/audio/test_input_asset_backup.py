"""Actual PCM inputs and generated companions survive project snapshots."""
from hashlib import sha256
import json
from pathlib import Path
import struct
import wave
import zipfile

import pytest

from carr_library.audio.backup import backup_project, restore_project
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.models import Sound
from carr_library.audio.runtime import find_ffmpeg
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


def _saved(tmp_path, *, name='EFFECT.WAV', music=False):
    studio = StudioController(private_root=tmp_path / 'private')
    script = _script(name)
    if music:
        script = script.replace('[sound', '[music mood=calm tempo=90 seed=7]\n[sound')
    studio.new(script, title='Portable input sounds', input_kind='script')
    output = tmp_path / 'audio'
    output.mkdir()
    studio.configure(output_dir=output)
    return studio, studio.save(tmp_path / 'studio.json')


def test_saved_and_recovery_inputs_survive_without_unrelated_neighbors(tmp_path):
    studio, source = _saved(tmp_path)
    root = studio.output_dir
    original = {'EFFECT.WAV': _wav(root / 'assets' / 'EFFECT.WAV'),
                'effect.bin': _wav(root / 'assets' / 'effect.bin', amplitude=1700)}
    studio.edit_script(_script('effect.bin'))
    recovery = studio.save_recovery()
    saved_bytes, recovery_bytes = source.read_bytes(), recovery.read_bytes()
    (root / 'assets' / 'private.json').write_text('{"token":"DO NOT COPY"}', encoding='utf-8')
    (root / 'assets' / 'notes.txt').write_text('not audio', encoding='utf-8')
    _wav(root / 'assets' / 'unreferenced.bin')
    _wav(root / 'cache' / ('a' * 64 + '.wav'))
    snapshot = tmp_path / 'snapshot.zip'
    backup_project(source, snapshot, project_dir=root, include_cache=False, include_audio=False)
    with zipfile.ZipFile(snapshot) as archive:
        assert not any(name.startswith(('audio/cache/', 'audio/renders/')) for name in archive.namelist())
    restored = restore_project(snapshot, tmp_path / 'restored')
    reopened = StudioController(private_root=tmp_path / 'other-private').open(restored['project_file'])
    assert reopened.output_dir != root
    assert {path.name for path in (reopened.output_dir / 'assets').iterdir()} == set(original)
    assert {name: (reopened.output_dir / 'assets' / name).read_bytes() for name in original} == original
    assert reopened.script == _script('EFFECT.WAV')
    reopened.recover()
    assert reopened.script == _script('effect.bin')
    assert source.read_bytes() == saved_bytes and recovery.read_bytes() == recovery_bytes
    assert {name: (root / 'assets' / name).read_bytes() for name in original} == original


@pytest.mark.parametrize('name', ['EFFECT.WAV', 'effect.bin'])
@pytest.mark.parametrize('damage', ['missing', 'truncated'])
def test_missing_or_damaged_referenced_input_prevents_completed_backup(tmp_path, name, damage):
    studio, source = _saved(tmp_path, name=name)
    asset = studio.output_dir / 'assets' / name
    if damage == 'truncated':
        raw = _wav(asset)
        asset.write_bytes(raw[:-2])
    target = tmp_path / 'snapshot.zip'
    with pytest.raises(ValueError):
        backup_project(source, target, project_dir=studio.output_dir,
                       include_cache=False, include_audio=False)
    assert not target.exists()


def test_required_input_needs_the_audio_project_folder(tmp_path):
    studio, source = _saved(tmp_path)
    _wav(studio.output_dir / 'assets' / 'EFFECT.WAV')
    with pytest.raises(ValueError, match='audio project folder'):
        backup_project(source, tmp_path / 'snapshot.zip')
    assert not (tmp_path / 'snapshot.zip').exists()


@pytest.mark.parametrize('damage', ['missing', 'truncated', 'unreferenced', 'unreferenced-pcm'])
def test_resigned_archive_rejects_damaged_or_unrelated_inputs(tmp_path, damage):
    studio, source = _saved(tmp_path, name='effect.bin')
    _wav(studio.output_dir / 'assets' / 'effect.bin')
    snapshot = tmp_path / 'snapshot.zip'
    backup_project(source, snapshot, project_dir=studio.output_dir, include_cache=False, include_audio=False)
    with zipfile.ZipFile(snapshot) as incoming:
        contents = {name: incoming.read(name) for name in incoming.namelist()}
    manifest = json.loads(contents['manifest.json'])
    name = 'audio/assets/effect.bin'
    if damage == 'missing':
        del contents[name]
        manifest['files'] = [row for row in manifest['files'] if row['path'] != name]
        record = None
    elif damage == 'truncated':
        contents[name] = contents[name][:-2]
        record = next(row for row in manifest['files'] if row['path'] == name)
    else:
        raw = contents[name] if damage == 'unreferenced-pcm' else b'{"token":"DO NOT RESTORE"}'
        name = 'audio/assets/private.bin' if damage == 'unreferenced-pcm' else 'audio/assets/private.json'
        contents[name] = raw
        record = {'path': name}
        manifest['files'].append(record)
    if record is not None:
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


@pytest.mark.parametrize('directive', ['[ sound', '["sound"', "['sound'", '[soun\\d'])
def test_renderer_accepted_sound_spellings_preserve_required_pcm(tmp_path, directive):
    studio, source = _saved(tmp_path, name='effect.bin')
    raw = _wav(studio.output_dir / 'assets' / 'effect.bin')
    studio.edit_script(directive + ' "effect.bin"]')
    assert studio.plan().scenes[0].events[0].asset == 'effect.bin'
    studio.save()
    snapshot = tmp_path / 'snapshot.zip'
    backup_project(source, snapshot, project_dir=studio.output_dir, include_cache=False, include_audio=False)
    restored = restore_project(snapshot, tmp_path / 'restored')
    assert (Path(restored['project_dir']) / 'assets' / 'effect.bin').read_bytes() == raw


def test_unfinished_sound_cannot_drop_its_input_but_other_work_remains_saveable(tmp_path):
    studio, source = _saved(tmp_path, name='effect.bin')
    studio.edit_script('[sound "unfinished')
    studio.save()
    with pytest.raises(ValueError):
        backup_project(source, tmp_path / 'rejected.zip', project_dir=studio.output_dir)
    assert not (tmp_path / 'rejected.zip').exists()
    studio.edit_script('[role Narrator voice="unfinished')
    studio.save()
    backup_project(source, tmp_path / 'unfinished-role.zip', project_dir=studio.output_dir)


@pytest.mark.parametrize('name', ['EFFECT.WAV', 'effect.bin'])
def test_restored_input_renders_without_original_assets(tmp_path, name):
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        pytest.skip('Actual local FFmpeg is required for input render verification.')
    studio, source = _saved(tmp_path, name=name)
    original = studio.output_dir / 'assets' / name
    _wav(original)
    plan = studio.plan()
    assert isinstance(plan.scenes[0].events[0], Sound)
    snapshot = tmp_path / 'snapshot.zip'
    backup_project(source, snapshot, project_dir=studio.output_dir, include_cache=False, include_audio=False)
    restored = restore_project(snapshot, tmp_path / 'restored')
    original.unlink()
    reopened = StudioController(private_root=tmp_path / 'other-private', ffmpeg=ffmpeg)
    reopened.open(restored['project_file'])
    result = reopened.render()
    assert Path(result['output']).is_relative_to(Path(restored['project_dir']))
    assert result['status'] == 'complete' and result['frames'] == 8000
    with wave.open(str(result['output']), 'rb') as stream:
        assert any(stream.readframes(stream.getnframes()))


def test_generated_music_companions_roundtrip_as_independent_verified_files(tmp_path, monkeypatch):
    from carr_library.audio import music
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        pytest.skip('Actual local FFmpeg is required for music checkpoint verification.')
    studio, source = _saved(tmp_path, music=True)
    original = studio.output_dir / 'assets' / 'EFFECT.WAV'
    _wav(original)
    studio.configure(ffmpeg=ffmpeg)
    before = studio.render()
    companions = {p.name: p.read_bytes() for p in (studio.output_dir / 'cache').iterdir()
                  if p.name.endswith(('.mid', '.music.json'))}
    assert any(name.endswith('.mid') for name in companions)
    assert any(name.endswith('.music.json') for name in companions)
    assert any((studio.output_dir / 'cache' / name).stat().st_nlink > 1 for name in companions)
    snapshot = tmp_path / 'music.zip'
    backup_project(source, snapshot, project_dir=studio.output_dir, include_audio=False)
    restored = restore_project(snapshot, tmp_path / 'restored')
    cache = Path(restored['project_dir']) / 'cache'
    assert {name: (cache / name).read_bytes() for name in companions} == companions
    assert all((cache / name).stat().st_nlink == 1 for name in companions)
    original.unlink()
    def no_generation(*args, **kwargs):
        raise AssertionError('Verified restored music should be reused.')
    monkeypatch.setattr(music, 'render_music', no_generation)
    reopened = StudioController(private_root=tmp_path / 'other-private', ffmpeg=ffmpeg)
    reopened.open(restored['project_file'])
    after = reopened.render()
    assert after['status'] == 'complete' and after['frames'] == before['frames']
    assert sha256(Path(after['output']).read_bytes()).digest() == sha256(Path(before['output']).read_bytes()).digest()


@pytest.mark.parametrize('legacy', [False, True], ids=['version2-literal', 'version1-imported'])
def test_literal_source_directives_never_select_sound_inputs_during_backup(tmp_path, legacy):
    source_text = '[sound ../../outside.wav]\n[pause 2]\n# Literal heading'
    original = tmp_path / 'book.txt'
    original.write_text(source_text, encoding='utf-8')
    studio = StudioController(private_root=tmp_path / 'private')
    studio.new(source_text, source_reference={
        'kind': 'local-text', 'filename': original.name, 'original_path': str(original),
        'bytes_sha256': sha256(original.read_bytes()).hexdigest()})
    output = tmp_path / 'audio'
    output.mkdir()
    studio.configure(output_dir=output)
    project = studio.save(tmp_path / 'literal.json')
    if legacy:
        payload = json.loads(project.read_text(encoding='utf-8'))
        payload['version'] = 1
        del payload['input_kind']
        project.write_text(json.dumps(payload), encoding='utf-8')
    before = project.read_bytes()
    snapshot = tmp_path / 'literal.zip'
    backup_project(project, snapshot, project_dir=output, include_cache=False, include_audio=False)
    with zipfile.ZipFile(snapshot) as archive:
        assert not any(name.startswith('audio/assets/') for name in archive.namelist())
    restored = restore_project(snapshot, tmp_path / 'restored')
    reopened = StudioController(private_root=tmp_path / 'other-private').open(restored['project_file'])
    assert reopened.input_kind == 'narration' and reopened.script == source_text
    assert reopened.plan().original_script == source_text
    assert project.read_bytes() == before and original.read_text(encoding='utf-8') == source_text
