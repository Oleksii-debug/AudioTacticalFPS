"""Passive completed-cache preservation with real files and no engines."""
from hashlib import sha256
import json
import subprocess
import tracemalloc
import wave
import pytest
from carr_library.application import LibraryApp
from carr_library import backup_sidecars
from carr_library.durable_state import atomic_json
from carr_library.library_backup import create_backup, restore_backup
from carr_library.runtime_settings import load_settings, save_settings


@pytest.fixture
def produced(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic generated PCM')
    save_settings(app.root / 'state', load_settings(app.root / 'state'))
    key = sha256(b'synthetic PCM cache identity').hexdigest()
    path = app.root / 'state/studio/cache' / (key + '-0123456789.wav')
    path.parent.mkdir(parents=True)
    with wave.open(str(path), 'wb') as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b'\x01\x00' * 800)
    receipt = path.with_name(key + '.json')
    atomic_json(receipt, {'schema': 1, 'key': key, 'file': path.name,
        'sha256': sha256(path.read_bytes()).hexdigest(), 'duration': .1,
        'sample_rate': 8000, 'channels': 1})
    return app, path, receipt


def resign(folder, manifest):
    raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


@pytest.mark.parametrize('damage', ['path', 'hash', 'unknown-field', 'boolean-schema',
                                   'orphan-media', 'container', 'wrong-rate'])
def test_malformed_generated_cache_cannot_be_published(produced, tmp_path, damage):
    app, audio, receipt = produced
    value = json.loads(receipt.read_bytes())
    if damage == 'path': value['file'] = '../' + audio.name
    elif damage == 'hash': value['sha256'] = '0' * 64
    elif damage == 'unknown-field': value['command'] = 'not executable'
    elif damage == 'boolean-schema': value['schema'] = True
    elif damage == 'wrong-rate': value['sample_rate'] = True
    elif damage == 'orphan-media': receipt.unlink()
    elif damage == 'container':
        raw = b'not a wav file' + audio.read_bytes()[14:]
        audio.write_bytes(raw)
        value['sha256'] = sha256(raw).hexdigest()
    if receipt.exists(): receipt.write_text(json.dumps(value))
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError): create_backup(app, destination, include_generated=True)
    assert not destination.exists()


@pytest.mark.parametrize('damage', ['media-bytes', 'false-inclusion', 'receipt-hash'])
def test_restore_authenticates_before_destination(produced, tmp_path, damage):
    app, audio, receipt = produced
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder, include_generated=True)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    if damage == 'media-bytes':
        path = folder / audio.relative_to(app.root)
        raw = bytearray(path.read_bytes()); raw[-1] ^= 1; path.write_bytes(raw)
    elif damage == 'false-inclusion':
        manifest['generated_audio_included'] = False
        checksum = resign(folder, manifest)
    else:
        path = folder / receipt.relative_to(app.root)
        value = json.loads(path.read_bytes()); value['sha256'] = '0' * 64
        raw = json.dumps(value).encode(); path.write_bytes(raw)
        entry = next(x for x in manifest['sidecars'] if x['filename'] == receipt.relative_to(app.root).as_posix())
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
        checksum = resign(folder, manifest)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError): restore_backup(folder, destination, checksum)
    assert not destination.exists()


def test_generated_restore_does_not_launch_providers(produced, tmp_path, monkeypatch):
    app, audio, receipt = produced
    def forbidden(*a, **k): pytest.fail('Backup or restore attempted to launch an engine.')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    folder = tmp_path / 'backup'
    checksum = create_backup(app, folder, include_generated=True)
    destination = restore_backup(folder, tmp_path / 'restored', checksum)
    assert (destination / audio.relative_to(app.root)).read_bytes() == audio.read_bytes()
    assert (destination / receipt.relative_to(app.root)).read_bytes() == receipt.read_bytes()


def test_complete_byte_budget_includes_generated_audio(produced, tmp_path):
    app, audio, receipt = produced
    destination = tmp_path / 'rejected'
    budget = app.database.stat().st_size + audio.stat().st_size - 1
    with pytest.raises(ValueError, match='byte|budget'):
        create_backup(app, destination, include_generated=True, max_bytes=budget)
    assert not destination.exists()


def test_final_bytes_detect_same_size_change_with_fixed_timestamps(produced, tmp_path, monkeypatch):
    app, audio, receipt = produced
    original_identity = backup_sidecars._identity
    monkeypatch.setattr(backup_sidecars, '_identity', lambda info: original_identity(info)[:-2] + (0, 0))
    settings = app.root / 'state/audio/engines.json'
    before = settings.read_bytes()
    assert b'"schema":1' in before
    changed = False
    def concurrent_writer():
        nonlocal changed
        if not changed and list(tmp_path.glob('.carr-library-backup-*/state/studio/cache/*.wav')):
            changed = True
            settings.write_bytes(before.replace(b'"schema":1', b'"schema":2'))
        return False
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='digest changed'):
        create_backup(app, destination, include_generated=True, cancelled=concurrent_writer)
    assert changed and not destination.exists()


def test_media_copy_is_bounded_and_cancellable(tmp_path):
    source = tmp_path / 'large.wav'
    with source.open('wb') as writer:
        for _ in range(12): writer.write(b'x' * (1024 * 1024))
    destination = tmp_path / 'copied.wav'
    tracemalloc.start()
    try:
        size, checksum, _ = backup_sidecars._stream_file(source, destination=destination,
            maximum=16 * 1024 * 1024, cancelled=lambda: False)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert size == source.stat().st_size == destination.stat().st_size
    assert peak < 6 * 1024 * 1024
    calls = 0
    def stop():
        nonlocal calls
        calls += 1
        return calls == 3
    partial = tmp_path / 'partial.wav'
    with pytest.raises(InterruptedError):
        backup_sidecars._stream_file(source, destination=partial,
            maximum=16 * 1024 * 1024, cancelled=stop)
    assert partial.stat().st_size < source.stat().st_size
