"""Typed v2 frame closure on real copied WAVs; no microphone or playback."""
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import recording, recording_monitor
from carr_library.audio.recording import RecordingOptions, RecordingService
from carr_library.durable_state import encode
from carr_library.library_backup import create_backup, restore_backup
from carr_library.state_backup import snapshot_state, restore_state


@pytest.fixture
def retained(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    service = RecordingService(app.root)
    identity = 'a' * 32
    path = service.root / (identity + '.wav')
    first, second = b'\x01\x02' * 120, b'\x03\x04' * 120
    with wave.open(str(path), 'wb') as writer:
        writer.setparams((1, 2, 24000, 0, 'NONE', 'PCM'))
        writer.writeframes(first + second)
    options = RecordingOptions(max_seconds=1, sample_rate=24000)
    receipt = {'schema': 'carr-recording/2', 'id': identity, 'filename': path.name,
        'title': 'Original Ω', 'created': '2026-11-27T00:00:00+00:00',
        'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': 'b' * 64, 'name': 'Selected input'},
        'ffmpeg_sha256': 'c' * 64, 'audio': service._audio(path, options, lambda: False),
        'limits': asdict(options), 'completion': 'stopped', 'timeline': recording._FRAME_NOTICE,
        'segments': [{'start_frame': 0, 'frames': 120, 'pcm_sha256': sha256(first).hexdigest(), 'ended_by': 'paused'},
                     {'start_frame': 120, 'frames': 120, 'pcm_sha256': sha256(second).hexdigest(), 'ended_by': 'stopped'}],
        'monitoring': {'enabled': True, 'output_identity': 'd' * 64, 'submitted_frames': 100, 'dropped_frames': 20}}
    (service.root / (identity + '.json')).write_bytes(encode(receipt))
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('Recovery started a native engine.'))
    monkeypatch.setattr(recording_monitor, '_api', lambda: pytest.fail('Recovery opened monitoring.'))
    return app, service, receipt, path.read_bytes()


@pytest.mark.parametrize('kind', ['full', 'state'])
def test_recorded_segments_survive_deleted_original_root_and_explicit_export(retained, tmp_path, kind):
    app, service, receipt, original = retained
    backup = tmp_path / 'backup'
    if kind == 'full':
        identity = create_backup(app, backup)
    else:
        identity = snapshot_state(app.root / 'state', backup)
    shutil.rmtree(app.root)
    destination = tmp_path / 'restored'
    if kind == 'full':
        restore_backup(backup, destination, identity)
    else:
        destination.mkdir()
        restore_state(backup, destination / 'state', identity)
    restored = RecordingService(destination)
    item = restored.get(receipt['id'])
    assert item['segments'] == receipt['segments'] and item['monitoring'] == receipt['monitoring']
    assert restored._devices == {} and restored._engine_hash is None
    target = tmp_path / 'explicit.wav'
    result = restored.export(receipt['id'], target)
    assert target.read_bytes() == original and result['sha256'] == receipt['audio']['sha256']
    assert Path(item['path']).read_bytes() == original


def damage(root, value, kind):
    receipt_path = root / 'studio/recordings' / (value['id'] + '.json')
    wav = receipt_path.with_suffix('.wav')
    if kind == 'missing-segment':
        value['segments'].pop()
    elif kind == 'overlap':
        value['segments'][1]['start_frame'] = 119
    elif kind == 'gap':
        value['segments'][1]['start_frame'] = 121
    elif kind == 'forged-hash':
        value['segments'][0]['pcm_sha256'] = '0' * 64
    elif kind == 'changed-pcm':
        raw = bytearray(wav.read_bytes())
        raw[-1] ^= 1
        wav.write_bytes(raw)
        value['audio']['sha256'] = sha256(raw).hexdigest()
    elif kind == 'forged-count':
        value['segments'][0]['frames'] = True
    elif kind == 'monitor-overrun':
        value['monitoring']['submitted_frames'] = value['audio']['frames']
    elif kind == 'missing-wave':
        wav.unlink()
    receipt_path.write_bytes(encode(value))


@pytest.mark.parametrize('kind', ['full', 'state'])
@pytest.mark.parametrize('corruption', ['missing-segment', 'overlap', 'gap', 'forged-hash',
                                      'changed-pcm', 'forged-count', 'monitor-overrun', 'missing-wave'])
def test_resigned_recording_range_corruption_fails_before_restore_publication(retained, tmp_path, kind, corruption):
    app, service, value, _ = retained
    backup, destination = tmp_path / 'backup', tmp_path / 'rejected'
    if kind == 'full':
        create_backup(app, backup)
        manifest = json.loads((backup / 'manifest.json').read_bytes())
        root, rows = backup / 'state', manifest['sidecars']
    else:
        rows = snapshot_state(app.root / 'state', backup)
        root = backup
    damage(root, value, corruption)
    prefix = 'state/' if kind == 'full' else ''
    kept = []
    for entry in rows:
        selected = backup / entry['filename']
        if selected.exists():
            raw = selected.read_bytes()
            kept.append({**entry, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
    if kind == 'full':
        manifest['sidecars'] = kept
        raw = (json.dumps(manifest, sort_keys=True) + '\n').encode('utf-8')
        (backup / 'manifest.json').write_bytes(raw)
        invoke = lambda: restore_backup(backup, destination, sha256(raw).hexdigest())
    else:
        invoke = lambda: restore_state(backup, destination, kept)
    with pytest.raises(ValueError):
        invoke()
    assert not destination.exists()


@pytest.mark.parametrize('kind', ['full', 'state'])
def test_snapshot_does_not_publish_a_recording_with_forged_retained_frame_hash(retained, tmp_path, kind):
    app, service, value, _ = retained
    damage(app.root / 'state', value, 'forged-hash')
    target = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='segment'):
        if kind == 'full':
            create_backup(app, target)
        else:
            snapshot_state(app.root / 'state', target)
    if target.exists():
        assert not (target / 'manifest.json').exists()
