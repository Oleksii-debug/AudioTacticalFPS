"""Real PCM, filesystem and complete-backup binding; no audible device claim."""
from dataclasses import replace
from hashlib import sha256
import json
import os
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio.playback_resume import FILENAME, PlaybackOwner, ResumeStore, prepare_audio, validate_bytes
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui.audio_studio import StudioController


def recording(path):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\x01\x00' * 8000)
    return path


@pytest.fixture
def saved(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Sources')
    book = app.add_text(workspace, 'Exact source', 'Original 😀 source\r\nSecond line.')
    page = ReadingDesk(app, workspace, book['edition']).page(1)
    owner = PlaybackOwner('source', source=page.location, start=0, end=len(page.raw_text))
    audio = prepare_audio(recording(tmp_path / 'voice.wav'), owner=owner)
    store = ResumeStore(app.root)
    store.remember(owner, audio.audio, 2000)
    return app, workspace, page, audio, store


def test_position_requires_exact_owner_and_actual_recording(saved):
    app, _, _, audio, store = saved
    assert store.position(audio.owner, audio.audio) == 2000
    assert store.position(replace(audio.owner, start=1), audio.audio) is None
    assert store.position(audio.owner, replace(audio.audio, sha256='a' * 64)) is None
    raw = (app.root / FILENAME).read_bytes()
    assert str(audio.path).encode() not in raw and b'voice.wav' not in raw
    assert validate_bytes(raw)[0].frame == 2000


def test_default_backup_preserves_resume_bytes_and_source_binding(saved, tmp_path):
    app, workspace, _, audio, _ = saved
    before = (app.root / FILENAME).read_bytes()
    app.archive_workspace(workspace)
    token = create_backup(app, tmp_path / 'backup')
    target = restore_backup(tmp_path / 'backup', tmp_path / 'restored', token)
    assert (target / FILENAME).read_bytes() == before
    assert ResumeStore(target).position(audio.owner, audio.audio) == 2000
    manifest = json.loads((tmp_path / 'backup' / 'manifest.json').read_bytes())
    assert not manifest['generated_audio_included']
    assert any(row['filename'] == FILENAME for row in manifest['sidecars'])


def test_owned_project_resume_round_trip_has_no_external_output_capability(saved, tmp_path):
    app, _, _, audio, store = saved
    controller = StudioController(private_root=app.root)
    controller.new('Synthetic owned narration.')
    project = controller.save_in_library()
    owner = PlaybackOwner('project', project=controller.project_id, revision=sha256(project.read_bytes()).hexdigest())
    store.remember(owner, audio.audio, 3000)
    token = create_backup(app, tmp_path / 'snapshot')
    target = restore_backup(tmp_path / 'snapshot', tmp_path / 'restored', token)
    assert ResumeStore(target).position(owner, audio.audio) == 3000
    assert (target / project.relative_to(app.root)).is_file()
    assert not (target / audio.path.name).exists()


@pytest.mark.parametrize('bad', ['missing-source', 'out-of-range', 'missing-owned-project'])
def test_backup_refuses_invalid_owner_without_publishing(saved, tmp_path, bad):
    app, _, _, audio, store = saved
    if bad == 'missing-source':
        owner = replace(audio.owner, source=replace(audio.owner.source, revision='missing'))
    elif bad == 'out-of-range':
        owner = replace(audio.owner, end=10000)
    else:
        owner = PlaybackOwner('project', project='a' * 32, revision='b' * 64)
    store.remember(owner, audio.audio, 100)
    before = (app.root / FILENAME).read_bytes()
    with pytest.raises(ValueError):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()
    assert (app.root / FILENAME).read_bytes() == before


@pytest.mark.parametrize('change', ['external-path', 'duplicate', 'bad-frame', 'nan', 'bool-version'])
def test_tampered_resume_is_refused_without_rewriting(saved, change):
    app, _, _, audio, store = saved
    path = app.root / FILENAME
    value = json.loads(path.read_bytes())
    if change == 'external-path':
        value['entries'][0]['owner']['path'] = '/outside.wav'
    elif change == 'duplicate':
        value['entries'].append(value['entries'][0])
    elif change == 'bad-frame':
        value['entries'][0]['frame'] = audio.audio.frames + 1
    elif change == 'nan':
        value['entries'][0]['frame'] = float('nan')
    else:
        value['version'] = True
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        store.remember(audio.owner, audio.audio, 4000)
    assert path.read_bytes() == raw


def test_replaced_data_folder_cannot_receive_old_resume(saved):
    app, _, _, audio, store = saved
    app.root.rename(app.root.with_name('old-root'))
    app.root.mkdir()
    with pytest.raises(ValueError, match='changed'):
        store.remember(audio.owner, audio.audio, 1)
    assert not (app.root / FILENAME).exists()


def test_cancel_before_publication_keeps_previous_position(saved):
    app, _, _, audio, store = saved
    raw = (app.root / FILENAME).read_bytes()
    calls = []
    def cancelled():
        calls.append(True)
        return len(calls) >= 2
    with pytest.raises(InterruptedError):
        store.remember(audio.owner, audio.audio, 5000, cancelled=cancelled)
    assert (app.root / FILENAME).read_bytes() == raw
    assert not list(app.root.glob('.playback-resume-*.tmp'))


def test_preparation_rejects_mutation_with_timestamp_coincidence(tmp_path, monkeypatch):
    import carr_library.audio.playback_resume as module
    path = recording(tmp_path / 'changing.wav')
    original = module._identity
    monkeypatch.setattr(module, '_identity', lambda info: original(info)[:3])
    calls = []
    def cancelled():
        calls.append(True)
        if len(calls) == 2:
            with path.open('r+b', buffering=0) as stream:
                stream.seek(44)
                stream.write(b'\x02\x00')
        return False
    with pytest.raises(ValueError, match='content changed'):
        prepare_audio(path, cancelled=cancelled)


def test_prepared_recording_rejects_later_replacement(saved):
    _, _, _, audio, _ = saved
    replacement = recording(audio.path.with_name('replacement.wav'))
    os.replace(replacement, audio.path)
    with pytest.raises(ValueError, match='changed'):
        audio.check()
