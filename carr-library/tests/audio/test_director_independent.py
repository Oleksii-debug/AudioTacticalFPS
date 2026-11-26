"""Independent actual local-audio, restart and publication verification.

These tests deliberately cross the real speech, FFmpeg, journal and project
boundaries. Optional engine absence is a visible skip, never mock audio proof.
"""
from array import array
from dataclasses import replace
import json
from pathlib import Path
import shutil
import threading
import wave

import pytest

from carr_library.audio.contracts import MusicCue, parse_script
from carr_library.audio.director import render_project, verify_production
from carr_library.audio.ffmpeg import AudioBackend
from carr_library.audio.project import (
    ProjectStore, atomic_bytes, file_sha256, json_bytes, read_json, restore_project,
)
from carr_library.audio.voices import FFmpegFliteProvider, VoiceRegistry, VoiceUnavailable


SCRIPT = '''# First scene
Narrator: We open the library.
Anna: The door is on the left.
Boris: I will check the right shelf.
[pause 1]
# Second scene
Narrator: Keep the source words safe.
Anna: Every book has a place.
Boris: The work is complete.
'''


class CountedFlite(FFmpegFliteProvider):
    def __init__(self, executable):
        super().__init__(executable)
        self.calls = []

    def synthesize(self, text, voice_id, output, **settings):
        result = super().synthesize(text, voice_id, output, **settings)
        self.calls.append((text, voice_id, result.sha256))
        return result


@pytest.fixture
def runtime():
    executable, probe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not executable or not probe:
        pytest.skip('Actual FFmpeg/ffprobe runtime is required for independent audio acceptance.')
    provider = CountedFlite(Path(executable))
    try:
        inventory = {voice.id: voice for voice in provider.list_voices()}
    except VoiceUnavailable:
        pytest.skip('This installed FFmpeg build does not contain local Flite speech.')
    if not {'awb', 'rms', 'slt'} <= set(inventory):
        pytest.skip('Three actual compatibility voices are required for this scenario.')
    selected = tuple(inventory[name] for name in ('awb', 'rms', 'slt'))
    return provider, selected, VoiceRegistry([provider]), AudioBackend(executable, probe, sample_rate=24000)


def _project(voice_list, script=SCRIPT):
    project = parse_script(script, voice_list, project_id='independent-scenario',
        title='Independent three-role local production', music=MusicCue(mood='tense', seed=41))
    roles = tuple(replace(role, pan=pan) for role, pan in zip(project.roles, (0, -1, 1)))
    return replace(project, roles=roles).validate()


def _channels(path):
    with wave.open(str(path), 'rb') as data:
        assert data.getnchannels() == 2 and data.getsampwidth() == 2
        samples = array('h', data.readframes(data.getnframes()))
    return samples[0::2], samples[1::2]


def test_actual_two_scene_three_role_audio_reuse_and_changed_scene(runtime, tmp_path):
    provider, voice_list, registry, backend = runtime
    project = _project(voice_list)
    store = ProjectStore.create(tmp_path / 'production', project)
    first = render_project(store, registry=registry, backend=backend)
    original = verify_production(first.manifest.parent, expected_project_sha256=project.sha256)
    assert len(provider.calls) == 6
    assert original['frames'] == first.frames == backend.probe(first.master).frames
    assert original['channels'] == 2 and original['sample_rate'] == 24000
    assert original['source_sha256'] == project.source_sha256
    assert (first.manifest.parent / 'script.txt').read_text(encoding='utf-8') == SCRIPT
    assert len(original['chapters']) == 2
    assert original['chapters'][0]['end_frame'] == original['chapters'][1]['start_frame']
    assert original['chapters'][-1]['end_frame'] == first.frames
    assert len([name for name in original['files'] if name.startswith('stem-')]) == 4
    timings = read_json(first.manifest.parent / 'timing.json')['lines']
    assert len(timings) == 6
    for line in timings:
        assert project.source_text[line['source_start']:line['source_end']] == line['text']
        assert line['start_frame'] < line['end_frame'] <= first.frames
    left, right = _channels(first.manifest.parent / 'stem-role-2.wav')
    assert sum(sample * sample for sample in left) > 0
    assert sum(sample * sample for sample in right) == 0
    left, right = _channels(first.manifest.parent / 'stem-role-3.wav')
    assert sum(sample * sample for sample in left) == 0
    assert sum(sample * sample for sample in right) > 0
    assert original['quality']['true_peak_dbfs'] <= 0.1

    same = render_project(ProjectStore(store.root), registry=registry, backend=backend)
    assert same.created == 0 and same.master == first.master
    assert len(provider.calls) == 6

    changed = _project(voice_list, SCRIPT.replace('The work is complete.', 'The work continues.'))
    loaded = store.load()
    store.save(changed, expected_sha256=loaded.file_sha256)
    second = render_project(store, registry=registry, backend=backend)
    updated = verify_production(second.manifest.parent, expected_project_sha256=changed.sha256)
    assert len(provider.calls) == 7
    assert provider.calls[-1][0] == 'The work continues.'
    assert original['files']['chapter-0001.wav'] == updated['files']['chapter-0001.wav']
    assert original['files']['chapter-0002.wav'] != updated['files']['chapter-0002.wav']
    assert first.master.is_file() and second.master.is_file() and first.master != second.master


def test_actual_cancel_snapshot_restore_and_resume_reuses_completed_speech(runtime, tmp_path):
    provider, voice_list, registry, backend = runtime
    store = ProjectStore.create(tmp_path / 'original', _project(voice_list))
    stopped = threading.Event()
    backup = tmp_path / 'snapshot'
    manifest = []

    def progress(event):
        if event.get('created', 0) >= 2 and not manifest:
            manifest.append(store.backup(backup))
            stopped.set()

    with pytest.raises((InterruptedError, RuntimeError)):
        render_project(store, registry=registry, backend=backend, cancel=stopped, progress=progress)
    assert manifest and len(provider.calls) == 1
    state = read_json(store.root / 'render-state.json')
    complete = {key: row['sha256'] for key, row in state['entries'].items() if row['status'] == 'complete'}
    assert len(complete) >= 2
    assert state['status'] == 'interrupted'
    restored = restore_project(backup, tmp_path / 'restored', manifest_sha256=manifest[0])
    result = render_project(restored, registry=registry, backend=backend)
    assert result.reused >= len(complete)
    assert len(provider.calls) == 6
    assert verify_production(result.manifest.parent)['source_sha256'] == store.load().project.source_sha256
    for key, checksum in complete.items():
        assert file_sha256(restored.root / 'cache' / f'{key}.wav') == checksum


def test_actual_changed_published_audio_is_preserved_and_recovered(runtime, tmp_path):
    provider, voice_list, registry, backend = runtime
    small = _project(voice_list, '# One\nNarrator: The source stays unchanged.\n')
    store = ProjectStore.create(tmp_path / 'project', small)
    first = render_project(store, registry=registry, backend=backend)
    fake = tmp_path / 'self-consistent-hashes'
    shutil.copytree(first.manifest.parent, fake)
    (fake / 'master.wav').write_bytes(b'not audio, even though its checksum is correct')
    forged = read_json(fake / 'production.json')
    forged['files']['master.wav'] = file_sha256(fake / 'master.wav')
    atomic_bytes(fake / 'production.json', json_bytes(forged), overwrite=True)
    with pytest.raises((ValueError, RuntimeError)):
        verify_production(fake, backend=backend)
    with first.master.open('ab') as stream:
        stream.write(b'USER-ALTERATION')
    altered = file_sha256(first.master)
    with pytest.raises(ValueError):
        verify_production(first.manifest.parent)
    recovered = render_project(store, registry=registry, backend=backend)
    assert recovered.master != first.master
    assert file_sha256(first.master) == altered
    assert len(provider.calls) == 1
    assert recovered.reused > 0
    assert verify_production(recovered.manifest.parent)['frames'] == recovered.frames


def test_production_validator_rejects_checksumed_non_audio_and_missing_required_files(tmp_path):
    (tmp_path / 'master.wav').write_bytes(b'not audio')
    manifest = {'schema_version': 1, 'status': 'complete', 'project_sha256': 'a' * 64,
        'frames': 123456, 'sample_rate': 1,
        'files': {'master.wav': file_sha256(tmp_path / 'master.wav')}}
    atomic_bytes(tmp_path / 'production.json', json_bytes(manifest))
    with pytest.raises((ValueError, RuntimeError)):
        verify_production(tmp_path, expected_project_sha256='a' * 64)


def test_project_directory_under_symlink_is_refused(tmp_path):
    real = tmp_path / 'actual-root'
    real.mkdir()
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip('This platform does not permit test directory symlinks.')
    from carr_library.audio.project import local_directory
    child = real / 'child'
    child.mkdir()
    with pytest.raises(ValueError):
        local_directory(alias / 'child')


def _small_project():
    from carr_library.audio.voices import Voice
    return parse_script('Narrator: Synthetic local project text.',
        [Voice('slt', 'flite', 'Synthetic fixture identity', ('en',), (), 'fixture')],
        project_id='independent-path-safety')


def test_create_checks_linked_parent_before_making_any_directory(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip('This platform does not permit test directory symlinks.')
    with pytest.raises(ValueError):
        ProjectStore.create(alias / 'new-project', _small_project())
    assert list(real.iterdir()) == []


@pytest.mark.parametrize('relative', ['private-settings.json', 'project.json', 'cache/' + 'b' * 64 + '.wav'])
def test_backup_rejects_journal_path_outside_its_exact_cache_key(tmp_path, relative):
    store = ProjectStore.create(tmp_path / 'project', _small_project())
    secret = store.root / 'private-settings.json'
    secret.write_text('private fixture; must not enter a cache backup')
    state = {'schema_version': 1, 'entries': {'a' * 64: {
        'status': 'complete', 'path': relative, 'sha256': file_sha256(secret), 'bytes': secret.stat().st_size}}}
    atomic_bytes(store.root / 'render-state.json', json_bytes(state))
    destination = tmp_path / 'backup'
    with pytest.raises(ValueError, match='cache identity'):
        store.backup(destination)
    assert not destination.exists()
    assert secret.read_text() == 'private fixture; must not enter a cache backup'


def test_restore_rejects_a_manifest_valid_but_internally_inconsistent_journal(tmp_path):
    from carr_library.audio.jobs import RenderJournal
    store = ProjectStore.create(tmp_path / 'project', _small_project())
    journal = RenderJournal(store.root)
    journal.start(store.load().project)

    def producer(path):
        with wave.open(str(path), 'wb') as audio:
            audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            audio.writeframes(b'\0\0' * 24000)

    journal.run('a' * 64, 'synthetic valid PCM checkpoint', producer)
    backup = tmp_path / 'backup'
    store.backup(backup)
    state = read_json(backup / 'render-state.json')
    state['entries']['a' * 64]['sha256'] = '0' * 64
    atomic_bytes(backup / 'render-state.json', json_bytes(state), overwrite=True)
    manifest = read_json(backup / 'backup-manifest.json')
    manifest['files']['render-state.json'] = file_sha256(backup / 'render-state.json')
    atomic_bytes(backup / 'backup-manifest.json', json_bytes(manifest), overwrite=True)
    destination = tmp_path / 'restored'
    with pytest.raises(ValueError, match='checkpoint checksum'):
        restore_project(backup, destination, manifest_sha256=file_sha256(backup / 'backup-manifest.json'))
    assert not destination.exists()


def test_directory_publication_cannot_replace_a_raced_empty_directory(monkeypatch, tmp_path):
    from carr_library.audio import project as module
    stage = tmp_path / 'staging'
    stage.mkdir()
    (stage / 'complete.txt').write_text('completed artifact')
    destination = tmp_path / 'destination'
    original_sync = module._fsync_directory
    raced_identity = []

    def race(path):
        original_sync(path)
        if Path(path) == stage:
            destination.mkdir()
            raced_identity.append(destination.stat().st_ino)

    monkeypatch.setattr(module, '_fsync_directory', race)
    with pytest.raises(FileExistsError):
        module.publish_directory(stage, destination)
    assert raced_identity == [destination.stat().st_ino]
    assert list(destination.iterdir()) == []
    assert (stage / 'complete.txt').read_text() == 'completed artifact'


def test_project_loaded_object_and_checksum_use_the_same_read(monkeypatch, tmp_path):
    from carr_library.audio import project as module
    store = ProjectStore.create(tmp_path / 'project', _small_project())
    before = file_sha256(store.path)
    original_read = module._read_document
    changed = replace(_small_project(), title='Edited externally')

    def race(path, **kwargs):
        data, raw = original_read(path, **kwargs)
        atomic_bytes(store.path, json_bytes(changed.to_dict()), overwrite=True)
        return data, raw

    monkeypatch.setattr(module, '_read_document', race)
    loaded = store.load()
    assert loaded.file_sha256 == before
    assert loaded.project.title != changed.title
    with pytest.raises(ValueError, match='changed on disk'):
        store.save(loaded.project, expected_sha256=loaded.file_sha256)


def test_cancelled_backup_makes_no_destination_parent(tmp_path):
    store = ProjectStore.create(tmp_path / 'project', _small_project())
    stopped = threading.Event()
    stopped.set()
    with pytest.raises(InterruptedError):
        store.backup(tmp_path / 'never-created' / 'backup', cancel=stopped)
    assert not (tmp_path / 'never-created').exists()


def test_preexisting_hardlinked_lock_cannot_truncate_the_saved_project(tmp_path):
    import os
    store = ProjectStore.create(tmp_path / 'project', _small_project())
    lock = store.root / '.project.lock'
    lock.unlink(missing_ok=True)
    try:
        os.link(store.path, lock)
    except OSError:
        pytest.skip('This filesystem does not support hardlink fixtures.')
    checksum = file_sha256(store.path)
    with pytest.raises(ValueError, match='independent regular file'):
        store.load()
    assert file_sha256(store.path) == checksum
