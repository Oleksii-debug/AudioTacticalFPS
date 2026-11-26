"""Real procedural chunks survive a Studio snapshot and resume after restore."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import zipfile

import pytest

from carr_library.audio.backup import backup_project, restore_project
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.production import render_project
from carr_library.ui.audio_studio import StudioController


class NoSpeech:
    def synthesize(self, *args, **kwargs):
        raise AssertionError('This test uses actual procedural music, not synthesized speech.')


@pytest.fixture
def paused_music(tmp_path):
    binary = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
    assert binary and Path(binary).is_file(), 'Actual local FFmpeg is required.'
    binary = Path(binary).absolute()
    root = tmp_path / 'audio'
    root.mkdir()
    controller = StudioController(private_root=tmp_path / 'private')
    script = '[output sample_rate=8000 stems=false]\n[music mood=calm tempo=240 seed=7]\n[pause 12]'
    controller.new(script, title='Music recovery', input_kind='script')
    controller.configure(output_dir=root)
    project = tmp_path / 'studio.json'
    controller.save(project)
    stopped = []

    def pause_after_accepted_chunk(message):
        if message.startswith('Music chunk 1 of 3 verified;'):
            stopped.append(True)

    with pytest.raises(InterruptedError):
        render_project(controller.plan(), root, engine=NoSpeech(), ffmpeg=binary,
                       cancelled=lambda: bool(stopped), on_status=pause_after_accepted_chunk)
    state_path, = root.glob('music-jobs/*/cue.music-work/state.json')
    state = json.loads(state_path.read_text())
    assert list(state['chunks']) == ['0']
    chunk = state_path.parent / 'part-00000000.wav'
    assert wav_info(chunk).frames == 32000
    return controller, project, root, binary, state_path, chunk


def snapshot(paused_music, tmp_path, **kwargs):
    _, project, root, _, _, _ = paused_music
    archive = tmp_path / 'snapshot.zip'
    backup_project(project, archive, project_dir=root, **kwargs)
    return archive


def rewrite_checked_archive(source, target, mutate):
    """Recompute outer records so semantic checkpoint validation must reject."""
    with zipfile.ZipFile(source) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(contents.pop('manifest.json'))
    mutate(contents)
    manifest['files'] = [{'path': name, 'size': len(raw), 'sha256': sha256(raw).hexdigest()}
                         for name, raw in sorted(contents.items())]
    manifest['total_bytes'] = sum(item['size'] for item in manifest['files'])
    contents['manifest.json'] = json.dumps(manifest).encode()
    with zipfile.ZipFile(target, 'w') as archive:
        for name, raw in contents.items():
            archive.writestr(name, raw)


def test_actual_interrupted_music_backup_restore_reuses_accepted_chunk(paused_music, tmp_path):
    controller, project, root, binary, state_path, chunk = paused_music
    original_project, original_chunk = project.read_bytes(), chunk.read_bytes()
    archive = snapshot(paused_music, tmp_path)
    relative = chunk.relative_to(root)
    with zipfile.ZipFile(archive) as zipped:
        assert 'audio/' + relative.as_posix() in zipped.namelist()
        assert zipped.read('audio/' + state_path.relative_to(root).as_posix()) == state_path.read_bytes()
    restored = restore_project(archive, tmp_path / 'restored')
    reopened = StudioController(private_root=tmp_path / 'other-private')
    reopened.open(restored['project_file'])
    assert reopened.input_kind == 'script' and reopened.script == controller.script
    retained = Path(restored['project_dir']) / relative
    stamp = retained.stat().st_mtime_ns
    result = render_project(reopened.plan(), Path(restored['project_dir']), engine=NoSpeech(), ffmpeg=binary)
    assert wav_info(result['output']).frames == 96000
    assert retained.read_bytes() == original_chunk and retained.stat().st_mtime_ns == stamp
    provenance, = Path(restored['project_dir']).glob('music-jobs/*/cue.music.json')
    assert json.loads(provenance.read_text())['reused_chunks'] == 1
    assert json.loads(state_path.read_text())['chunks'].keys() == {'0'}
    assert project.read_bytes() == original_project and chunk.read_bytes() == original_chunk


def test_completed_music_outputs_and_resume_state_roundtrip(paused_music, tmp_path):
    controller, _, root, binary, _, _ = paused_music
    before = render_project(controller.plan(), root, engine=NoSpeech(), ffmpeg=binary)
    archive = snapshot(paused_music, tmp_path)
    restored = restore_project(archive, tmp_path / 'restored')
    after = render_project(controller.plan(), Path(restored['project_dir']), engine=NoSpeech(), ffmpeg=binary)
    assert sha256(Path(before['output']).read_bytes()).hexdigest() == sha256(Path(after['output']).read_bytes()).hexdigest()
    job, = Path(restored['project_dir']).glob('music-jobs/*')
    assert (job / 'cue.wav').is_file() and (job / 'cue.mid').is_file()
    assert len(json.loads((job / 'cue.music-work' / 'state.json').read_text())['chunks']) == 3


def test_completed_music_rejects_unknown_nested_settings_in_provenance(paused_music, tmp_path):
    controller, project, root, binary, _, _ = paused_music
    render_project(controller.plan(), root, engine=NoSpeech(), ffmpeg=binary)
    path, = root.glob('music-jobs/*/cue.music.json')
    provenance = json.loads(path.read_text())
    provenance['rights'] = {'api_key': 'SYNTHETIC PRIVATE SETTING'}
    path.write_text(json.dumps(provenance))
    with pytest.raises(ValueError, match='provenance'):
        backup_project(project, tmp_path / 'rejected.zip', project_dir=root)
    assert not (tmp_path / 'rejected.zip').exists()


def test_partial_helpers_and_unknown_files_are_preserved_but_not_collected(paused_music, tmp_path):
    _, _, _, _, state_path, _ = paused_music
    unrelated = ['part-00000001.partial.wav', 'segments.txt', 'private-helper.py', 'api-secrets.json']
    for name in unrelated:
        (state_path.parent / name).write_bytes(b'SYNTHETIC UNACCEPTED SCRATCH')
    archive = snapshot(paused_music, tmp_path)
    with zipfile.ZipFile(archive) as zipped:
        names = zipped.namelist()
        assert not any(name.endswith(tuple(unrelated)) for name in names)
    assert all((state_path.parent / name).read_bytes() == b'SYNTHETIC UNACCEPTED SCRATCH' for name in unrelated)


@pytest.mark.parametrize('damage', ['audio_bytes', 'settings_checksum', 'frames', 'discontinuous'])
def test_bad_accepted_music_cannot_be_published_as_a_snapshot(paused_music, tmp_path, damage):
    _, project, root, _, state_path, chunk = paused_music
    if damage == 'audio_bytes':
        with chunk.open('r+b') as stream:
            stream.seek(wav_info(chunk).data_offset + 20)
            value = stream.read(1)
            stream.seek(-1, 1)
            stream.write(bytes([value[0] ^ 1]))
    else:
        state = json.loads(state_path.read_text())
        if damage == 'settings_checksum':
            state['settings']['seed'] += 1
        elif damage == 'frames':
            state['chunks']['0']['frames'] += 1
        else:
            state['chunks']['2'] = state['chunks'].pop('0')
        state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match='checkpoint'):
        backup_project(project, tmp_path / 'rejected.zip', project_dir=root)
    assert not (tmp_path / 'rejected.zip').exists()


def test_rehashed_archive_cannot_forge_accepted_chunk_identity(paused_music, tmp_path):
    archive = snapshot(paused_music, tmp_path)

    def forge(contents):
        name = next(name for name in contents if name.endswith('/cue.music-work/state.json'))
        state = json.loads(contents[name])
        state['chunks']['0']['sha256'] = '0' * 64
        contents[name] = json.dumps(state).encode()

    forged = tmp_path / 'forged.zip'
    rewrite_checked_archive(archive, forged, forge)
    with pytest.raises(ValueError, match='accepted checksum'):
        restore_project(forged, tmp_path / 'unpublished')
    assert not (tmp_path / 'unpublished').exists()


def test_rehashed_archive_cannot_add_an_unaccepted_music_file(paused_music, tmp_path):
    archive = snapshot(paused_music, tmp_path)

    def orphan(contents):
        name = next(name for name in contents if name.endswith('/part-00000000.wav'))
        contents[name.replace('part-00000000', 'part-00000001')] = contents[name]

    forged = tmp_path / 'orphan.zip'
    rewrite_checked_archive(archive, forged, orphan)
    with pytest.raises(ValueError, match='not accepted'):
        restore_project(forged, tmp_path / 'unpublished')
    assert not (tmp_path / 'unpublished').exists()


def test_music_chunks_obey_snapshot_file_and_byte_budgets(paused_music, tmp_path):
    _, project, root, _, _, _ = paused_music
    archive = snapshot(paused_music, tmp_path)
    with zipfile.ZipFile(archive) as zipped:
        manifest = json.loads(zipped.read('manifest.json'))
    for name, limits in [('files', {'file_budget': len(manifest['files']) - 1}),
                         ('bytes', {'byte_budget': manifest['total_bytes'] - 1})]:
        target = tmp_path / (name + '.zip')
        with pytest.raises(ValueError, match='budget'):
            backup_project(project, target, project_dir=root, **limits)
        assert not target.exists()


def test_excluding_cache_explicitly_excludes_music_resume_state(paused_music, tmp_path):
    archive = snapshot(paused_music, tmp_path, include_cache=False)
    with zipfile.ZipFile(archive) as zipped:
        assert not any(name.startswith(('audio/cache/', 'audio/music-jobs/')) for name in zipped.namelist())
    restored = restore_project(archive, tmp_path / 'restored')
    assert not (Path(restored['project_dir']) / 'music-jobs').exists()

