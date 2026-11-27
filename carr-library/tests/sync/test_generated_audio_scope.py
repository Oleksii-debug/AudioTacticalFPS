"""Explicit generated-audio scope is part of the actual cached snapshot."""
import json
from hashlib import sha256
import os
from pathlib import Path
import shutil
from threading import Event

import pytest

from carr_library.application import LibraryApp
from carr_library.sync.service import SyncService
from carr_library.sync.vault import Vault


@pytest.mark.parametrize('include_generated', [False, True])
def test_declared_audio_scope_survives_actual_publication_and_verification(tmp_path, include_generated):
    app = LibraryApp(tmp_path / 'library')
    service, folder = SyncService(app), tmp_path / 'portable'
    receipt = service.publish(folder, 'Explicit scope', None, include_generated=include_generated)
    assert receipt['include_generated'] is include_generated
    preview = service.inspect(folder, receipt['token'])
    assert preview['include_generated'] is include_generated
    omitted = any('Generated audio is not included' in message for message in preview['warnings'])
    assert omitted is (not include_generated)


@pytest.mark.parametrize('bad', [None, 0, 1, 'false'])
def test_audio_scope_rejects_implicit_truthiness_before_preparing_files(tmp_path, bad):
    service = SyncService(LibraryApp(tmp_path / 'library'))
    with pytest.raises(ValueError):
        service.publish(tmp_path / 'portable', 'Invalid scope', None, include_generated=bad)
    assert not service.cache_root.exists()


def test_resume_rejects_a_cached_record_with_a_different_audio_scope(tmp_path):
    service = SyncService(LibraryApp(tmp_path / 'library'))
    folder, cancel = tmp_path / 'portable', Event()
    def progress(**event):
        if event['phase'] == 'Publishing verified checkpoint chunks':
            cancel.set()
    with pytest.raises(InterruptedError):
        service.publish(folder, 'Resumable snapshot', None, include_generated=False,
                        cancelled=cancel.is_set, on_progress=progress)
    ready, = service.cache_root.glob('*/ready.json')
    metadata = json.loads(ready.read_text(encoding='utf-8'))
    assert metadata['include_generated'] is False
    metadata['include_generated'] = True
    ready.write_text(json.dumps(metadata), encoding='utf-8')
    with pytest.raises(ValueError, match='inconsistent'):
        service.publish(folder, 'Resumable snapshot', None, include_generated=False)
    assert Vault(folder).head() is None and ready.exists()


def test_changing_explicit_scope_prepares_a_new_payload_without_reusing_old_cache(tmp_path):
    service = SyncService(LibraryApp(tmp_path / 'library'))
    folder, cancel = tmp_path / 'portable', Event()
    def progress(**event):
        if event['phase'] == 'Publishing verified checkpoint chunks':
            cancel.set()
    with pytest.raises(InterruptedError):
        service.publish(folder, 'Snapshot', None, cancelled=cancel.is_set, on_progress=progress)
    previous, = service.cache_root.glob('*/ready.json')
    previous_bytes = previous.read_bytes()
    phases = []
    receipt = service.publish(folder, 'Snapshot', None, include_generated=True,
                              on_progress=lambda **event: phases.append(event['phase']))
    assert 'Packing the checkpoint' in phases
    assert 'Unpacking the prepared checkpoint' not in phases
    assert previous.read_bytes() == previous_bytes
    assert service.inspect(folder, receipt['token'])['include_generated'] is True


@pytest.mark.parametrize('requested_mode', [False, True], ids=['legacy-default-resume', 'new-audio-payload'])
def test_legacy_false_only_cache_never_becomes_a_true_audio_resume(tmp_path, requested_mode):
    service = SyncService(LibraryApp(tmp_path / 'library'))
    folder, cancel = tmp_path / 'portable', Event()
    def progress(**event):
        if event['phase'] == 'Publishing verified checkpoint chunks':
            cancel.set()
    with pytest.raises(InterruptedError):
        service.publish(folder, 'Legacy snapshot', None, cancelled=cancel.is_set, on_progress=progress)
    ready, = service.cache_root.glob('*/ready.json')
    value = json.loads(ready.read_text(encoding='utf-8'))
    prior_identity = sha256(json.dumps([service._vault_key(folder), value['backup_checksum'],
        'Legacy snapshot', None, False], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    value['identity'] = prior_identity
    value.pop('include_generated')
    prior_job = service.cache_root / prior_identity
    ready.parent.rename(prior_job)
    prior_ready = prior_job / 'ready.json'
    prior_bytes = json.dumps(value).encode()
    prior_ready.write_bytes(prior_bytes)
    payload_bytes = (prior_job / 'payload' / 'snapshot.zip').read_bytes()
    phases = []
    receipt = service.publish(folder, 'Legacy snapshot', None, include_generated=requested_mode,
                              on_progress=lambda **event: phases.append(event['phase']))
    assert service.inspect(folder, receipt['token'])['include_generated'] is requested_mode
    if requested_mode:
        assert 'Packing the checkpoint' in phases and 'Unpacking the prepared checkpoint' not in phases
        assert prior_ready.read_bytes() == prior_bytes
    else:
        assert 'Unpacking the prepared checkpoint' in phases and 'Packing the checkpoint' not in phases
        materialized = Vault(folder).materialize(receipt['id'], tmp_path / 'materialized',
                                                expected_token=receipt['token'])
        assert (materialized / 'snapshot.zip').read_bytes() == payload_bytes


def test_true_scope_rejects_ready_record_without_explicit_scope_and_preserves_it(tmp_path):
    service = SyncService(LibraryApp(tmp_path / 'library'))
    folder, cancel = tmp_path / 'portable', Event()
    with pytest.raises(InterruptedError):
        service.publish(folder, 'True snapshot', None, include_generated=True,
            cancelled=cancel.is_set, on_progress=lambda **event: cancel.set()
                if event['phase'] == 'Publishing verified checkpoint chunks' else None)
    ready, = service.cache_root.glob('*/ready.json')
    value = json.loads(ready.read_text(encoding='utf-8'))
    value.pop('include_generated')
    raw = json.dumps(value).encode()
    ready.write_bytes(raw)
    with pytest.raises(ValueError, match='inconsistent'):
        service.publish(folder, 'True snapshot', None, include_generated=True)
    assert ready.read_bytes() == raw and Vault(folder).head() is None


@pytest.mark.parametrize('include_generated,password', [(False, None), (True, None),
    (True, 'Synthetic encrypted audio snapshot 123')], ids=['metadata-only', 'audio', 'encrypted-audio'])
def test_owned_real_render_and_progress_preferences_survive_explicit_snapshot_scope(
        tmp_path, include_generated, password):
    from carr_library.audio.ffmpeg import wav_info
    from carr_library.progress import FeedbackPreferences, ProgressPreferenceStore, FILENAME
    from carr_library.ui.audio_studio import StudioController
    executable = os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg')
    assert executable and Path(executable).is_file(), 'A real local FFmpeg executable is required.'
    class NoSpeech:
        def synthesize(self, *args, **kwargs):
            raise AssertionError('The source producer renders real silence, not substitute speech.')
    app = LibraryApp(tmp_path / 'library')
    studio = StudioController(private_root=app.root, registry=NoSpeech(), ffmpeg=executable)
    studio.new('[output sample_rate=8000 stems=false]\n[pause 0.12]',
               title='Owned transfer source', input_kind='script')
    project = studio.save_in_library()
    rendered = Path(studio.render()['output'])
    assert wav_info(rendered).frames == 960
    project_bytes, rendered_bytes = project.read_bytes(), rendered.read_bytes()
    preferences = FeedbackPreferences('Beep', interval=7, volume=23, muted=True)
    ProgressPreferenceStore(app.root).save(preferences)
    preference_bytes = (app.root / FILENAME).read_bytes()
    service, folder = SyncService(app), tmp_path / 'vault'
    receipt = service.publish(folder, 'Owned audio', None, password, include_generated=include_generated)
    preview = service.inspect(folder, receipt['token'], password)
    assert preview['include_generated'] is include_generated and preview['verified']
    destination = tmp_path / 'restored'
    service.restore(folder, receipt['token'], destination, password)
    restored_project = destination / project.relative_to(app.root)
    restored_audio = destination / rendered.relative_to(app.root)
    reopened = StudioController(private_root=destination, registry=NoSpeech(), ffmpeg=executable)
    reopened.open(restored_project)
    assert restored_project.read_bytes() == project_bytes and project.read_bytes() == project_bytes
    assert reopened.output_dir.is_relative_to(destination)
    assert restored_audio.exists() is include_generated
    if include_generated:
        assert restored_audio.read_bytes() == rendered_bytes and wav_info(restored_audio).frames == 960
    assert (destination / FILENAME).read_bytes() == preference_bytes
    assert ProgressPreferenceStore(destination).load() == preferences
