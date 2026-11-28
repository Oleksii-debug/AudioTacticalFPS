"""Real PCM and exact composite/flat backup boundaries, without a capture device."""
from dataclasses import asdict
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import wave

from filelock import FileLock
import pytest

from carr_library.application import LibraryApp
from carr_library.audio.recording import RecordingOptions, RecordingService, unverified_recording_files
from carr_library import backup_compat, recording_backup
from carr_library.durable_state import atomic_json, read_json
from carr_library.ingest.restore import RestoreError
from carr_library.keybindings import encode_preferences
from carr_library.library_backup import create_backup, restore_backup, INCOMPLETE
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION


def _raw(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()


def _seal(folder, manifest):
    raw = _raw(manifest)
    (folder / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _record(folder, identity, *, previous=None):
    options = RecordingOptions(max_seconds=1, sample_rate=16000, max_bytes=65536)
    target = folder / (identity + '.wav')
    with wave.open(str(target), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes((b'\x01\x00' if identity[0] == 'a' else b'\x02\x00') * 3200)
    value = {'schema': 'carr-recording/1', 'id': identity, 'filename': target.name,
        'title': 'Retained original', 'created': '2026-11-27T12:00:00+00:00',
        'source': {'kind': 'microphone', 'backend': 'dshow', 'device_id': 'd' * 64,
                   'name': 'Synthetic fixture; no physical capture'},
        'ffmpeg_sha256': 'e' * 64, 'audio': RecordingService._audio(target, options, lambda: False),
        'limits': asdict(options), 'completion': 'stopped'}
    if previous is not None:
        value.update(schema='carr-recording/2', retake={
            'origin_id': previous.get('retake', {}).get('origin_id', previous['id']),
            'previous_id': previous['id'], 'previous_sha256': previous['audio']['sha256']})
    atomic_json(folder / (identity + '.json'), value)
    return value


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Recording backup')
    app.add_text(workspace, 'Exact source', 'Immutable recorded-source library.')
    (app.root / 'keybindings.json').write_bytes(encode_preferences({}))
    folder = app.root / recording_backup.DIRECTORY
    folder.mkdir(parents=True)
    return app, workspace, folder


def _backup(app, target, shape, *, include_generated=False):
    checksum = create_backup(app, target, include_generated=include_generated)
    manifest = json.loads((target / 'manifest.json').read_bytes())
    assert manifest['version'] == 5 and manifest['schema_version'] == CURRENT_SCHEMA_VERSION == 6
    if shape == 'composite':
        return manifest, checksum
    rows = [{**row, 'filename': 'state/' + row['filename']} for row in manifest['state']]
    for row in manifest['sidecars']['files']:
        name = row['path']
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(target / 'sidecars' / name, destination)
        rows.append({'filename': name, 'size_bytes': row['size_bytes'], 'sha256': row['sha256']})
    shutil.rmtree(target / 'sidecars')
    flat = {key: manifest[key] for key in ('kind', 'schema_version', 'schema_fingerprint', 'database', 'assets')}
    flat.update(version=4, sidecars=sorted(rows, key=lambda row: row['filename']),
                generated_audio_included=include_generated)
    if 'pending' in manifest:
        flat['pending'] = manifest['pending']
    return flat, _seal(target, flat)


def _reseal_entry(folder, manifest, name, raw=None, *, remove=False):
    path = folder / name
    if remove:
        path.unlink()
    else:
        path.write_bytes(raw)
    collection = 'sidecars' if type(manifest.get('sidecars')) is list else 'state'
    relative = name if collection == 'sidecars' else name.removeprefix('state/')
    rows = []
    for entry in manifest[collection]:
        if entry['filename'] == relative:
            if remove:
                continue
            entry = {**entry, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()}
        rows.append(entry)
    manifest[collection] = rows
    return _seal(folder, manifest)


@pytest.mark.parametrize('shape', ['composite', 'flat'])
@pytest.mark.parametrize('include_generated', [False, True], ids=['no-cache', 'with-cache'])
def test_recordings_retake_and_orphan_round_trip_without_runtime_authority(library, tmp_path, monkeypatch,
                                                                        shape, include_generated):
    app, workspace, folder = library
    first = _record(folder, 'a' * 32)
    second = _record(folder, 'b' * 32, previous=first)
    orphan = folder / ('c' * 32 + '.wav')
    orphan.write_bytes(b'Unreceipted final; deliberately not a decodable WAV.')
    pending = folder / '.pending-recording-owned'
    pending.mkdir()
    (pending / 'capture.wav').write_bytes(b'incomplete private capture')
    engines = app.root / 'state/audio/engines.json'
    engines.parent.mkdir(parents=True)
    atomic_json(engines, {'schema': 1, 'ffmpeg': None, 'ffprobe': None,
        'piper': [], 'voice_helpers': [], 'music_models': [], 'fluidsynth': None})
    original_bytes = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    measured = RecordingService._audio
    checked = []
    def no_orphan_decode(path, options, cancelled):
        assert Path(path).name != orphan.name
        checked.append(Path(path).name)
        return measured(path, options, cancelled)
    monkeypatch.setattr(RecordingService, '_audio', staticmethod(no_orphan_decode))
    monkeypatch.setattr(RecordingService, '_runtime', lambda *_a, **_k: pytest.fail('backup activated capture runtime'))
    backup = tmp_path / 'backup'
    manifest, checksum = _backup(app, backup, shape, include_generated=include_generated)
    assert not tuple(backup.rglob('.pending-recording-*'))
    assert not tuple(backup.rglob('.recording.lock'))
    assert checked and set(checked) == {first['filename'], second['filename']}
    before = (backup / 'manifest.json').read_bytes()
    destination = tmp_path / 'restored'
    restore_backup(backup, destination, checksum)
    restored = RecordingService(destination)
    assert restored.get(second['id']) == {**second, 'path': str(destination / recording_backup.DIRECTORY / second['filename'])}
    assert {path.name: path.read_bytes() for path in (destination / recording_backup.DIRECTORY).iterdir()
            if path.is_file()} == original_bytes
    assert [path.name for path in unverified_recording_files(destination)] == [orphan.name]
    assert read_json(destination / 'state/audio/engines.json')['restore_requires_revalidation'] is True
    assert LibraryApp(destination).search(workspace, 'Immutable')
    assert (backup / 'manifest.json').read_bytes() == before and not (destination / INCOMPLETE).exists()


@pytest.mark.parametrize('shape', ['composite', 'flat'])
def test_binary_recordings_use_streaming_copy_not_json_materialization(library, tmp_path, monkeypatch, shape):
    app, _, folder = library
    value = _record(folder, 'a' * 32)
    backup = tmp_path / 'backup'
    _, checksum = _backup(app, backup, shape)
    original = backup_compat.read_regular
    def guarded(path, maximum, *, allow_empty=False):
        assert not str(path).endswith('.wav'), 'binary original reached the JSON reader'
        return original(path, maximum, allow_empty=allow_empty)
    monkeypatch.setattr(backup_compat, 'read_regular', guarded)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    assert sha256((root / recording_backup.DIRECTORY / value['filename']).read_bytes()).hexdigest() == value['audio']['sha256']


@pytest.mark.parametrize('shape', ['composite', 'flat'])
@pytest.mark.parametrize('change', ['pcm', 'receipt-hash', 'missing-wav', 'foreign-id', 'retake-origin', 'malformed-json'],
                         ids=str)
def test_resealed_invalid_recording_or_graph_is_rejected_before_publication(library, tmp_path, shape, change):
    app, _, folder = library
    first = _record(folder, 'a' * 32)
    second = _record(folder, 'b' * 32, previous=first)
    backup = tmp_path / 'backup'
    manifest, _ = _backup(app, backup, shape)
    name = recording_backup.PREFIX + second['id'] + '.json'
    raw = _raw(second)
    remove = False
    if change == 'pcm':
        name = recording_backup.PREFIX + second['filename']
        raw = bytearray((backup / name).read_bytes())
        raw[-1] ^= 1
        raw = bytes(raw)
    elif change == 'receipt-hash':
        raw = _raw({**second, 'audio': {**second['audio'], 'sha256': 'f' * 64}})
    elif change == 'missing-wav':
        name, remove = recording_backup.PREFIX + second['filename'], True
    elif change == 'foreign-id':
        raw = _raw({**second, 'id': 'c' * 32})
    elif change == 'retake-origin':
        raw = _raw({**second, 'retake': {**second['retake'], 'origin_id': 'c' * 32}})
    elif change == 'malformed-json':
        raw = b'not a receipt'
    checksum = _reseal_entry(backup, manifest, name, raw, remove=remove)
    destination = tmp_path / 'restored'
    with pytest.raises((ValueError, RestoreError, json.JSONDecodeError)):
        restore_backup(backup, destination, checksum)
    assert not destination.exists()


def test_retake_cycle_is_rejected_without_quadratic_or_runtime_work(library, tmp_path):
    app, _, folder = library
    first, second = _record(folder, 'a' * 32), _record(folder, 'b' * 32)
    for item, previous in ((first, second), (second, first)):
        item.update(schema='carr-recording/2', retake={'origin_id': 'c' * 32,
            'previous_id': previous['id'], 'previous_sha256': previous['audio']['sha256']})
        atomic_json(folder / (item['id'] + '.json'), item, replace=True)
    with pytest.raises(ValueError, match='cycle'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_active_capture_lock_blocks_backup_and_releases_for_next_snapshot(library, tmp_path):
    app, _, folder = library
    _record(folder, 'a' * 32)
    with FileLock(folder / '.recording.lock', timeout=0, thread_local=False):
        with pytest.raises(RuntimeError, match='recording is still active'):
            create_backup(app, tmp_path / 'blocked')
    assert not (tmp_path / 'blocked').exists()
    create_backup(app, tmp_path / 'allowed')


@pytest.mark.parametrize('name', ['unknown.wav', 'notes.tmp', 'A' * 32 + '.json'])
def test_unknown_final_state_cannot_be_omitted_by_generic_filters(library, tmp_path, name):
    app, _, folder = library
    (folder / name).write_bytes(b'authored state')
    with pytest.raises(ValueError, match='Unknown retained recording'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


@pytest.mark.parametrize('name', ['Studio/recordings/' + 'a' * 32 + '.json',
    'studio/Recordings/' + 'a' * 32 + '.wav', 'studio/recordings/' + 'a' * 32 + '.JSON'])
def test_mixed_case_recording_alias_never_falls_through_opaque_restore(name):
    with pytest.raises(ValueError, match='Noncanonical'):
        recording_backup.validate_state_entries([{'filename': name, 'size_bytes': 2, 'sha256': 'a' * 64}])


def test_unreadable_recording_root_fails_closed_before_backup(library, tmp_path, monkeypatch):
    app, _, folder = library
    original = Path.lstat
    def denied(path, *args, **kwargs):
        if path == folder:
            raise PermissionError('fixture unreadable recording inventory')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', denied)
    with pytest.raises(PermissionError, match='unreadable'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_recording_hardlink_is_rejected_in_existing_regular_file_boundary(library, tmp_path):
    app, _, folder = library
    value = _record(folder, 'a' * 32)
    os.link(folder / value['filename'], tmp_path / 'foreign-link.wav')
    with pytest.raises(ValueError, match='without links'):
        create_backup(app, tmp_path / 'backup')
    assert not (tmp_path / 'backup').exists()


def test_cancel_during_actual_pcm_verification_keeps_destination_absent(library, tmp_path, monkeypatch):
    app, _, folder = library
    _record(folder, 'a' * 32)
    measured = RecordingService._audio
    stopped = False
    def cancel_in_pcm(path, options, cancelled):
        nonlocal stopped
        stopped = True
        return measured(path, options, cancelled)
    monkeypatch.setattr(RecordingService, '_audio', staticmethod(cancel_in_pcm))
    with pytest.raises(InterruptedError, match='cancelled'):
        create_backup(app, tmp_path / 'backup', cancelled=lambda: stopped)
    assert stopped and not (tmp_path / 'backup').exists()


def test_receipt_budget_and_orphan_zero_bytes_keep_distinct_meanings(library, tmp_path):
    app, _, folder = library
    orphan = folder / ('a' * 32 + '.wav')
    orphan.write_bytes(b'')
    backup = tmp_path / 'backup'
    _, checksum = _backup(app, backup, 'composite')
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    assert (root / recording_backup.DIRECTORY / orphan.name).read_bytes() == b''
    (folder / ('a' * 32 + '.json')).write_bytes(b' ' * (recording_backup.JSON_LIMIT + 1))
    with pytest.raises(ValueError, match='byte allowance'):
        create_backup(app, tmp_path / 'bad')
    assert not (tmp_path / 'bad').exists()


def test_foreign_flat_recording_does_not_raise_existing_total_byte_budget():
    manifest = {'version': 4, 'kind': 'carr-library', 'schema_version': 6,
        'schema_fingerprint': 'a' * 64, 'database': {}, 'assets': [],
        'sidecars': [{'filename': recording_backup.PREFIX + 'a' * 32 + '.wav',
            'size_bytes': backup_compat.MAX_BYTES + 1, 'sha256': 'b' * 64}],
        'generated_audio_included': False}
    with pytest.raises(RestoreError, match='manifest byte limit'):
        backup_compat.normalize_manifest(manifest)


def _monitored(folder, value):
    """Convert only the explicit monitoring envelope; PCM/retake bytes stay bound."""
    changed = {**value, 'schema': 'carr-recording/3', 'monitoring': {
        'enabled': True, 'output_identity': 'f' * 64,
        'submitted_frames': value['audio']['frames'] - 1, 'dropped_frames': 1}}
    atomic_json(folder / (value['id'] + '.json'), changed, replace=True)
    return changed


@pytest.mark.parametrize('shape', ['composite', 'flat'])
def test_monitored_recording_and_retake_keep_pcm_lineage_passive_after_restore(
        library, tmp_path, monkeypatch, shape):
    app, _, folder = library
    first = _monitored(folder, _record(folder, 'a' * 32))
    second = _monitored(folder, _record(folder, 'b' * 32, previous=first))
    captured = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    monkeypatch.setattr(RecordingService, '_runtime',
                        lambda *_a, **_k: pytest.fail('Restoration activated capture or monitoring.'))
    manifest, checksum = _backup(app, tmp_path / 'backup', shape)
    root = restore_backup(tmp_path / 'backup', tmp_path / 'restored', checksum)
    restored = RecordingService(root)
    observed = restored.get(second['id'])
    assert observed == {**second, 'path': str(root / recording_backup.DIRECTORY / second['filename'])}
    assert {path.name: path.read_bytes() for path in (root / recording_backup.DIRECTORY).iterdir()
            if path.is_file()} == captured
    assert second['retake']['origin_id'] == first['id']
    assert second['retake']['previous_sha256'] == first['audio']['sha256']
    assert observed['monitoring']['submitted_frames'] + observed['monitoring']['dropped_frames'] == (
        observed['audio']['frames'])
    assert manifest['schema_version'] == 6 and not (root / INCOMPLETE).exists()


@pytest.mark.parametrize('shape', ['composite', 'flat'])
@pytest.mark.parametrize('change', ['origin', 'previous-hash', 'missing-parent'], ids=str)
def test_resealed_monitored_retake_cannot_bypass_its_exact_retained_parent(
        library, tmp_path, shape, change):
    app, _, folder = library
    first = _monitored(folder, _record(folder, 'a' * 32))
    second = _monitored(folder, _record(folder, 'b' * 32, previous=first))
    backup = tmp_path / 'backup'
    manifest, _ = _backup(app, backup, shape)
    name = recording_backup.PREFIX + second['id'] + '.json'
    value, remove = {**second, 'retake': dict(second['retake'])}, False
    if change == 'origin':
        value['retake']['origin_id'] = 'c' * 32
    elif change == 'previous-hash':
        value['retake']['previous_sha256'] = 'e' * 64
    else:
        name, remove = recording_backup.PREFIX + first['id'] + '.json', True
    checksum = _reseal_entry(backup, manifest, name, _raw(value), remove=remove)
    target = tmp_path / 'restored'
    with pytest.raises((ValueError, RestoreError), match='retake.*identity'):
        restore_backup(backup, target, checksum)
    assert not target.exists()


def _segmented(folder, value):
    """Bind Pause/Resume declarations to the fixture's actual retained PCM."""
    with wave.open(str(folder / value['filename']), 'rb') as reader:
        frames = reader.getnframes()
        width = reader.getnchannels() * reader.getsampwidth()
        pcm = reader.readframes(frames)
    assert len(pcm) == frames * width
    first = frames // 3
    changed = {**value, 'schema': 'carr-recording/4',
        'timeline': 'Retained PCM frames only; paused wall time is omitted. Review joins before use.',
        'segments': [
            {'start_frame': 0, 'frames': first, 'pcm_sha256': sha256(pcm[:first * width]).hexdigest(),
             'ended_by': 'paused'},
            {'start_frame': first, 'frames': frames - first,
             'pcm_sha256': sha256(pcm[first * width:]).hexdigest(), 'ended_by': 'stopped'}]}
    atomic_json(folder / (value['id'] + '.json'), changed, replace=True)
    return changed


@pytest.mark.parametrize('shape', ['composite', 'flat'])
@pytest.mark.parametrize('monitored', [False, True], ids=['dry', 'monitored'])
def test_segmented_retake_round_trip_preserves_exact_pcm_and_passive_lineage(
        library, tmp_path, monkeypatch, shape, monitored):
    app, _, folder = library
    first = _record(folder, 'a' * 32)
    second = _record(folder, 'b' * 32, previous=first)
    if monitored:
        second = _monitored(folder, second)
    second = _segmented(folder, second)
    captured = {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}
    monkeypatch.setattr(RecordingService, '_runtime',
                        lambda *_a, **_k: pytest.fail('Backup or restore activated a recording runtime.'))
    backup = tmp_path / 'backup'
    manifest, checksum = _backup(app, backup, shape)
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    actual = RecordingService(root).get(second['id'])
    assert actual == {**second, 'path': str(root / recording_backup.DIRECTORY / second['filename'])}
    assert {path.name: path.read_bytes() for path in (root / recording_backup.DIRECTORY).iterdir()
            if path.is_file()} == captured
    assert actual['retake'] == {'origin_id': first['id'], 'previous_id': first['id'],
                               'previous_sha256': first['audio']['sha256']}
    assert ('monitoring' in actual) is monitored
    with wave.open(actual['path'], 'rb') as reader:
        for segment in actual['segments']:
            assert reader.tell() == segment['start_frame']
            data = reader.readframes(segment['frames'])
            assert sha256(data).hexdigest() == segment['pcm_sha256']
        assert reader.tell() == actual['audio']['frames'] and reader.readframes(1) == b''
    assert manifest['schema_version'] == 6 and not (root / INCOMPLETE).exists()


@pytest.mark.parametrize('shape', ['composite', 'flat'])
@pytest.mark.parametrize('change', ['frame-hash', 'overlap', 'boolean-frames', 'uncovered', 'foreign-parent'],
                         ids=str)
def test_resealed_segment_or_parent_mutation_rejects_even_with_unchanged_whole_pcm(
        library, tmp_path, shape, change):
    app, _, folder = library
    first = _record(folder, 'a' * 32)
    second = _segmented(folder, _record(folder, 'b' * 32, previous=first))
    backup = tmp_path / 'backup'
    manifest, _ = _backup(app, backup, shape)
    changed = json.loads(_raw(second))
    if change == 'frame-hash':
        changed['segments'][0]['pcm_sha256'] = 'f' * 64
    elif change == 'overlap':
        changed['segments'][1]['start_frame'] -= 1
    elif change == 'boolean-frames':
        changed['segments'][0]['frames'] = True
    elif change == 'uncovered':
        changed['segments'][1]['frames'] -= 1
    else:
        changed['retake']['previous_id'] = 'c' * 32
    checksum = _reseal_entry(backup, manifest, recording_backup.PREFIX + second['id'] + '.json',
                            _raw(changed))
    # The generic manifest and whole-file audio receipt still match. The
    # additional rejection must come from the typed frame/retake contract.
    assert RecordingService._audio(backup / recording_backup.PREFIX / second['filename'],
                                   RecordingOptions(**second['limits']), lambda: False) == second['audio']
    target = tmp_path / 'restored'
    with pytest.raises((ValueError, RestoreError), match='segment|retake'):
        restore_backup(backup, target, checksum)
    assert not target.exists()


def test_cancellation_inside_actual_segment_scan_prevents_snapshot_publication(
        library, tmp_path, monkeypatch):
    app, _, folder = library
    value = _segmented(folder, _record(folder, 'a' * 32))
    measured = RecordingService._audio
    verify = recording_backup.verify_recording_segments
    readframes = wave.Wave_read.readframes
    verified, reads, checks = [], [], []
    stopped = False
    scanning = False

    def measured_whole(path, options, cancelled):
        result = measured(path, options, cancelled)
        verified.append(Path(path).name)
        return result

    def observed_read(reader, count):
        if scanning:
            reads.append(count)
        return readframes(reader, count)

    def cancelled_during_segments(path, receipt, *, cancelled):
        nonlocal scanning, stopped
        assert verified == [value['filename']]
        def checked():
            nonlocal stopped
            checks.append(len(reads))
            if len(checks) == 3:
                stopped = True
            return cancelled()
        scanning = True
        try:
            return verify(path, receipt, cancelled=checked)
        finally:
            scanning = False

    monkeypatch.setattr(RecordingService, '_audio', staticmethod(measured_whole))
    monkeypatch.setattr(wave.Wave_read, 'readframes', observed_read)
    monkeypatch.setattr(recording_backup, 'verify_recording_segments', cancelled_during_segments)
    target = tmp_path / 'backup'
    with pytest.raises(InterruptedError, match='Recording snapshot verification was cancelled'):
        create_backup(app, target, cancelled=lambda: stopped)
    assert stopped and checks == [0, 0, 1]
    assert reads == [value['segments'][0]['frames']]
    assert not target.exists()
