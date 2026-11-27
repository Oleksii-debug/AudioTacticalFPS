"""Typed recording originals through real complete backup and Studio storage.

Fixtures are generated PCM and synthetic receipts. These tests never discover
or open a microphone, execute a model or claim physical-device qualification.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import recording_backup
from carr_library.audio.recording import (
    RecordingOptions, RecordingService, validate_recording_receipt, unverified_recording_files,
)
from carr_library.library_backup import restore_backup, verify_backup
from carr_library.ui.recording_dialog import create_recording_project


def retained(app, identity='a' * 32):
    service = RecordingService(app.root)
    path = service.root / (identity + '.wav')
    options = RecordingOptions(max_seconds=1, sample_rate=24000)
    with wave.open(str(path), 'wb') as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24000)
        target.writeframes(b'\x01\x00\xff\xff' * 12000)
    value = {
        'schema': 'carr-recording/1', 'id': identity, 'filename': path.name,
        'title': 'Synthetic recording — Україна', 'created': '2026-11-27T10:00:00+00:00',
        'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': 'b' * 64,
                   'name': 'Synthetic device; never opened'},
        'ffmpeg_sha256': 'c' * 64, 'completion': 'duration-limit',
        'audio': service._audio(path, options, lambda: False), 'limits': asdict(options),
    }
    validate_recording_receipt(value)
    path.with_suffix('.json').write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return service, {**value, 'path': str(path)}


def test_saved_capture_and_inert_crash_originals_survive_deleted_root_and_rebackup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service, value = retained(app)
    complete = Path(value['path']).read_bytes()
    orphan = service.root / ('d' * 32 + '.wav')
    orphan.write_bytes(b'Unverified final original without a receipt or a valid WAV header')
    stage = service.root / '.pending-recording-abcdefgh'
    stage.mkdir()
    (stage / 'capture.wav').write_bytes(b'Unfinished original before native WAV finalization')
    before = {path.relative_to(app.root).as_posix(): path.read_bytes()
              for path in recording_backup.state_paths(app.root)}
    assert len(before) == 4
    backup = tmp_path / 'backup'
    digest = app.backup(backup)
    verify_backup(backup, digest)
    shutil.rmtree(app.root)
    root = tmp_path / 'відновлена-бібліотека'
    restore_backup(backup, root, expected_manifest_sha256=digest)
    restored = RecordingService(root)
    assert restored.get(value['id'])['audio'] == value['audio']
    assert Path(restored.get(value['id'])['path']).read_bytes() == complete
    assert restored._devices == {} and restored._engine_hash is None and restored.ffmpeg is None
    assert len(unverified_recording_files(root)) == 2
    assert {path.relative_to(root).as_posix(): path.read_bytes()
            for path in recording_backup.state_paths(root)} == before
    second = LibraryApp(root).backup(tmp_path / 'second')
    verify_backup(tmp_path / 'second', second)


def test_active_capture_guard_refuses_snapshot_without_misclassifying_worker(tmp_path):
    from filelock import FileLock
    app = LibraryApp(tmp_path / 'library')
    service, _ = retained(app)
    target = tmp_path / 'backup'
    with FileLock(service.root / '.recording.lock', timeout=0):
        with pytest.raises(RuntimeError, match='active'):
            app.backup(target)
    assert not (target / 'manifest.json').exists()
    digest = app.backup(tmp_path / 'idle-backup')
    verify_backup(tmp_path / 'idle-backup', digest)


@pytest.mark.parametrize('change', [
    {'filename': '../outside.wav'},
    {'ffmpeg_sha256': 'unbound'},
    {'source': {'kind': 'microphone', 'backend': 'network'}},
    {'unknown': 'field'},
], ids=['path', 'runtime-digest', 'source-schema', 'unknown'])
def test_forged_recording_metadata_is_rejected_by_actual_snapshot(tmp_path, change):
    app = LibraryApp(tmp_path / 'library')
    service, item = retained(app)
    path = service.root / (item['id'] + '.json')
    data = json.loads(path.read_text(encoding='utf-8'))
    data.update(change)
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError):
        app.backup(tmp_path / 'backup')


def test_same_size_pcm_change_does_not_pass_saved_receipt_or_backup(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service, item = retained(app)
    path = Path(item['path'])
    raw = bytearray(path.read_bytes())
    raw[-2:] = b'\x00\x00'
    path.write_bytes(raw)
    with pytest.raises(ValueError, match='differs'):
        service.get(item['id'])
    with pytest.raises(ValueError, match='differs'):
        app.backup(tmp_path / 'backup')


def test_recording_hardlink_and_unregistered_neighbors_are_not_silently_collected(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    service, item = retained(app)
    external = tmp_path / 'external-alias.wav'
    os.link(item['path'], external)
    with pytest.raises(ValueError, match='hard-link'):
        recording_backup.state_paths(app.root)
    external.unlink()
    (service.root / 'unrelated-secret.txt').write_text('synthetic unrelated bytes', encoding='utf-8')
    with pytest.raises(ValueError, match='unregistered'):
        recording_backup.state_paths(app.root)


def test_recording_inventory_counts_transients_and_enforces_byte_budget(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    service, item = retained(app)
    monkeypatch.setattr(recording_backup, 'MAX_SNAPSHOT_BYTES', 10)
    with pytest.raises(ValueError, match='allowance'):
        recording_backup.state_paths(app.root)


def test_verified_recording_enters_existing_studio_as_an_independent_owned_asset(tmp_path):
    from carr_library.audio.models import Sound
    from carr_library.audio.backup import input_assets
    app = LibraryApp(tmp_path / 'library')
    service, item = retained(app)
    original = Path(item['path']).read_bytes()
    controller = create_recording_project(app.root, item['id'], title='Editable recording project')
    plan = controller.plan()
    events = tuple(event for scene in plan.scenes for event in scene.events)
    assert len(events) == 1 and isinstance(events[0], Sound)
    copied = controller.output_dir / 'assets' / events[0].asset
    assert copied.read_bytes() == original and copied.stat().st_nlink == 1
    assert (copied.stat().st_dev, copied.stat().st_ino) != (
        Path(item['path']).stat().st_dev, Path(item['path']).stat().st_ino)
    assert controller.project_path.is_relative_to(app.root / 'audio-projects')
    assert controller.source_reference['receipt']['audio'] == item['audio']
    assert controller.input_kind == 'script' and not controller.dirty
    # An edited derivative does not discard the source WAV or its receipt.
    controller.edit_script('Narrator: An explicit replacement derivative.\n')
    controller.save()
    assert input_assets(controller.project_path, controller.output_dir, strict=True)
    assert service.get(item['id']) == item
    target = tmp_path / 'backup'
    digest = app.backup(target)
    restored = tmp_path / 'restored'
    restore_backup(target, restored, expected_manifest_sha256=digest)
    from carr_library.ui.audio_studio import StudioController
    reopened = StudioController(private_root=restored).open(
        restored / controller.project_path.relative_to(app.root))
    assert reopened.output_dir.is_relative_to(restored)
    assert reopened.script == controller.script
    assert input_assets(reopened.project_path, reopened.output_dir, strict=True)


def test_project_creation_cancel_preserves_original_and_publishes_no_partial_project(tmp_path, monkeypatch):
    import threading
    from carr_library.audio import jobs
    app = LibraryApp(tmp_path / 'library')
    service, item = retained(app)
    cancelled = threading.Event()
    actual = jobs.publish_new
    def copied_then_cancelled(*args, **kwargs):
        actual(*args, **kwargs)
        cancelled.set()
    monkeypatch.setattr(jobs, 'publish_new', copied_then_cancelled)
    with pytest.raises((InterruptedError, jobs.RenderCancelled)):
        create_recording_project(app.root, item['id'], title='Cancelled',
                                 cancelled=cancelled.is_set)
    assert service.get(item['id']) == item
    assert not list((app.root / 'audio-projects').iterdir())


def test_original_recording_reference_and_copied_pcm_remain_hash_bound(tmp_path):
    from carr_library.audio.backup import input_assets
    app = LibraryApp(tmp_path / 'library')
    _, item = retained(app)
    controller = create_recording_project(app.root, item['id'], title='Bound')
    file = controller.output_dir / 'assets' / controller.source_reference['asset']
    raw = bytearray(file.read_bytes())
    raw[-2:] = b'\x00\x00'
    file.write_bytes(raw)
    with pytest.raises(ValueError, match='differs'):
        input_assets(controller.project_path, controller.output_dir, strict=True)
    with pytest.raises(ValueError, match='differs'):
        app.backup(tmp_path / 'backup')


def test_opaque_recovery_inputs_do_not_authorize_devices_or_runtime(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    service = RecordingService(app.root)
    stage = service.root / '.pending-recording-abcdefgh'
    stage.mkdir()
    (stage / 'capture.wav').write_bytes(b'')
    monkeypatch.setattr(RecordingService, '_runtime', lambda *a: pytest.fail('Recovery selected a runtime'))
    monkeypatch.setattr(RecordingService, 'devices', lambda *a, **k: pytest.fail('Recovery enumerated microphones'))
    digest = app.backup(tmp_path / 'backup')
    restore_backup(tmp_path / 'backup', tmp_path / 'restored', expected_manifest_sha256=digest)
    records = unverified_recording_files(tmp_path / 'restored')
    assert len(records) == 1 and records[0].read_bytes() == b''
