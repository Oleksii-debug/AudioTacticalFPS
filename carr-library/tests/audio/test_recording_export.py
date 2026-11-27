"""Verified streaming WAV export without a device, runtime, or overwritten file."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import os
import shutil
from types import SimpleNamespace
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import recording
from carr_library.audio.models import AudioCancelled
from carr_library.audio.recording import RecordingService
from carr_library.durable_state import atomic_json
from carr_library.library_backup import restore_backup


@pytest.fixture
def retained(tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    service = RecordingService(app.root)
    identity = 'a' * 32
    source = service.root / (identity + '.wav')
    with wave.open(str(source), 'wb') as output:
        output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\x10\x00' * 600000)
    original = source.read_bytes()
    receipt = {
        'schema': 'carr-recording/1', 'id': identity, 'filename': source.name,
        'title': 'Retained synthetic PCM', 'created': datetime.now(timezone.utc).isoformat(),
        'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': 'b' * 64,
                   'name': 'Synthetic fixture; no microphone opened'},
        'ffmpeg_sha256': 'c' * 64,
        'audio': {'sha256': sha256(original).hexdigest(), 'size_bytes': len(original),
                  'sample_rate': 24000, 'channels': 1, 'frames': 600000,
                  'sample_width': 2, 'duration_seconds': 25.0},
        'limits': {'max_seconds': 30, 'sample_rate': 24000, 'channels': 1,
                   'max_bytes': 2 * 1024 * 1024},
        'completion': 'device-ended'}
    receipt_path = service.root / (identity + '.json')
    atomic_json(receipt_path, receipt)
    def no_runtime(*args, **kwargs):
        pytest.fail('Reading/exporting a completed recording activated an audio engine')
    monkeypatch.setattr(service, '_runtime', no_runtime)
    return SimpleNamespace(app=app, service=service, identity=identity, source=source,
                           original=original, receipt_path=receipt_path, receipt=receipt)


def test_streamed_export_verifies_original_and_needs_no_microphone_or_engine(retained, tmp_path):
    destination = tmp_path / 'Новий запис.wav'
    receipt_bytes = retained.receipt_path.read_bytes()
    result = retained.service.export(retained.identity, destination)
    assert result == {'recording_id': retained.identity, 'path': str(destination),
                      'sha256': sha256(retained.original).hexdigest(),
                      'size_bytes': len(retained.original)}
    assert destination.read_bytes() == retained.source.read_bytes() == retained.original
    assert retained.receipt_path.read_bytes() == receipt_bytes
    assert not list(tmp_path.glob('.recording-export-*'))


@pytest.mark.parametrize('kind', ['private-library', 'retained-source', 'existing-export'])
def test_private_or_existing_destination_is_rejected_before_reading_pcm(retained, tmp_path, monkeypatch, kind):
    if kind == 'private-library':
        destination = retained.app.root / 'must-not-be-created.wav'
    elif kind == 'retained-source':
        destination = retained.source
    else:
        destination = tmp_path / 'existing.wav'
        destination.write_bytes(b'Previously saved user audio')
    old_bytes = destination.read_bytes() if destination.exists() else None
    monkeypatch.setattr(retained.service, 'get', lambda *a, **k: pytest.fail('Rejected destination read PCM'))
    with pytest.raises((ValueError, FileExistsError)):
        retained.service.export(retained.identity, destination)
    if old_bytes is None:
        assert not destination.exists()
    else:
        assert destination.read_bytes() == old_bytes
    assert retained.source.read_bytes() == retained.original


def _during_copy(retained, destination, monkeypatch, callback, *, after_close=False):
    original_open = Path.open
    triggered = []

    class Reader:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, kind, error, traceback):
            result = self.stream.__exit__(kind, error, traceback)
            if after_close and kind is None and not triggered:
                triggered.append(True)
                callback(original_open)
            return result
        def read(self, size=-1):
            block = self.stream.read(size)
            assert 0 <= size <= 1024 * 1024, 'Export requested an unbounded input read'
            if block and not after_close and not triggered:
                triggered.append(True)
                callback(original_open)
            return block

    def opened(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if (Path(path) == retained.source and mode == 'rb' and not triggered
                and any(destination.parent.glob('.recording-export-*'))):
            return Reader(stream)
        return stream
    monkeypatch.setattr(Path, 'open', opened)
    return triggered


@pytest.mark.parametrize('after_close', [False, True])
def test_changed_original_during_or_after_copy_never_publishes(retained, tmp_path, monkeypatch, after_close):
    destination = tmp_path / 'rejected.wav'
    def change(original_open):
        with original_open(retained.source, 'r+b') as source:
            source.seek(44)
            source.write(b'\x7f')
            source.flush()
            os.fsync(source.fileno())
    triggered = _during_copy(retained, destination, monkeypatch, change, after_close=after_close)
    with pytest.raises(ValueError, match='recording.*(changed|differs)'):
        retained.service.export(retained.identity, destination)
    assert triggered and not destination.exists()
    assert not list(tmp_path.glob('.recording-export-*'))


def test_cancellation_during_streamed_copy_preserves_original_and_destination(retained, tmp_path, monkeypatch):
    destination = tmp_path / 'cancelled.wav'
    cancelled = []
    triggered = _during_copy(retained, destination, monkeypatch,
                             lambda original_open: cancelled.append(True))
    with pytest.raises(AudioCancelled, match='export cancelled'):
        retained.service.export(retained.identity, destination, cancelled=lambda: bool(cancelled))
    assert triggered and not destination.exists()
    assert retained.source.read_bytes() == retained.original
    assert not list(tmp_path.glob('.recording-export-*'))


def test_competing_destination_create_at_publication_is_never_overwritten(retained, tmp_path, monkeypatch):
    destination = tmp_path / 'competing.wav'
    publish = recording.publish_file
    def competing(staged, target, **kwargs):
        Path(target).write_bytes(b'Competing completed user output')
        return publish(staged, target, **kwargs)
    monkeypatch.setattr(recording, 'publish_file', competing)
    with pytest.raises(FileExistsError):
        retained.service.export(retained.identity, destination)
    assert destination.read_bytes() == b'Competing completed user output'
    assert retained.source.read_bytes() == retained.original
    assert not list(tmp_path.glob('.recording-export-*'))


def test_backup_delete_original_root_restore_then_export_without_runtime(retained, tmp_path, monkeypatch):
    backup, restored = tmp_path / 'backup', tmp_path / 'Відновлена бібліотека'
    manifest = retained.app.backup(backup)
    shutil.rmtree(retained.app.root)
    restore_backup(backup, restored, expected_manifest_sha256=manifest)
    service = RecordingService(restored)
    monkeypatch.setattr(service, '_runtime', lambda: pytest.fail('Restore export activated a runtime'))
    destination = tmp_path / 'restored-export.wav'
    exported = service.export(retained.identity, destination)
    assert exported['sha256'] == retained.receipt['audio']['sha256']
    assert destination.read_bytes() == retained.original
    assert service.get(retained.identity)['path'].startswith(str(restored))
    assert service._devices == {} and service.runtime is None
