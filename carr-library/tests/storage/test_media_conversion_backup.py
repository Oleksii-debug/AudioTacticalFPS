"""Actual prepared/partial/complete media snapshots, not a processor activation."""
from contextlib import closing, contextmanager
import os
from pathlib import Path
import struct
import threading
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import media_backup, media_worker
from carr_library.audio.jobs import atomic_json, project_lock, read_json
from carr_library.audio.media_conversion import MediaConversionService
from carr_library.audio.media_types import ConversionAllowance, ConversionPreset, MediaChoice
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.operations.broker import ResourceBroker
from carr_library.operations.models import ResourceLimits
from carr_library.storage.revisions import CURRENT_SCHEMA_VERSION, Storage


def prepare(tmp_path, *, count=1):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic media snapshot')
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(
        cpu_slots=2, memory_bytes=1024**3, disk_bytes=128 * 1024**2,
        min_free_disk_bytes=0))
    service = MediaConversionService(app.root, ffmpeg=resolve_ffmpeg(), broker=broker)
    originals = []
    for index in range(count):
        path = tmp_path / ('Оригінал Ω ' + str(index) + '.wav')
        with wave.open(str(path), 'wb') as output:
            output.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            output.writeframes(struct.pack('<h', 1000 + index) * 4800)
        originals.append(path)
    choices = [MediaChoice(row, row.streams[0].index, ConversionPreset())
               for row in service.inspect(originals)]
    identity = service.prepare(choices, title='Черга Україна Ω',
        allowance=ConversionAllowance(disk_bytes=32 * 1024**2))
    return app, service, identity, originals


def test_prepared_snapshot_preserves_owned_sources_and_resumes_after_originals_are_lost(tmp_path):
    app, service, identity, originals = prepare(tmp_path)
    definition = service.definition(identity)
    service.save_preset('Saved FLAC Україна', ConversionPreset(format='flac', pcm_bits=24))
    for path in originals:
        path.unlink()
    archive = tmp_path / 'snapshot'
    receipt = create_backup(app, archive)
    verify_backup(archive, receipt)
    restored = restore_backup(archive, tmp_path / 'different-library', receipt)
    replay = MediaConversionService(restored, ffmpeg=resolve_ffmpeg())
    assert replay.definition(identity) == definition
    assert replay.presets()['Saved FLAC Україна']['pcm_bits'] == 24
    owned = restored / 'media-conversion-jobs' / identity / definition['inputs'][0]['retained']
    assert owned.read_bytes() == (service.root / identity / definition['inputs'][0]['retained']).read_bytes()
    assert owned.stat().st_nlink == 1
    assert not list((restored / 'media-conversion-jobs').rglob('*.lock'))
    assert replay.verify(identity)['completed'] == 0
    assert replay.run(identity)['status'] == 'complete'
    assert replay.verify(identity)['completed'] == 1


def test_partial_snapshot_keeps_completed_output_and_only_encodes_pending_item(tmp_path, monkeypatch):
    app, service, identity, originals = prepare(tmp_path, count=2)
    stop = threading.Event()
    with pytest.raises(InterruptedError):
        service.run(identity, cancelled=stop.is_set,
            progress=lambda done, total, state: stop.set() if done == 1 else None)
    first = service.verify(identity)['outputs'][0]
    before = (service.root / identity / 'results/000001' / first['path']).read_bytes()
    for path in originals:
        path.unlink()
    archive = tmp_path / 'partial'
    receipt = create_backup(app, archive)
    restored = restore_backup(archive, tmp_path / 'resumed', receipt)
    replay = MediaConversionService(restored, ffmpeg=resolve_ffmpeg())
    result = replay.root / identity / 'results/000001' / first['path']
    inode = result.stat().st_ino
    called = []
    actual = media_worker.convert_item
    def observed(job, definition, row, *args, **kwargs):
        called.append(row['index'])
        return actual(job, definition, row, *args, **kwargs)
    monkeypatch.setattr(media_worker, 'convert_item', observed)
    assert replay.run(identity)['completed'] == 2
    assert called == [2]
    assert result.read_bytes() == before and result.stat().st_ino == inode
    assert result.stat().st_nlink == 1


def test_complete_restore_and_rebackup_are_offline_and_preserve_exact_receipts(tmp_path, monkeypatch):
    app, service, identity, originals = prepare(tmp_path)
    service.run(identity)
    summary = service.verify(identity)
    definition = service.definition(identity)
    originals[0].unlink()
    monkeypatch.setattr(media_worker, 'run_process',
        lambda *args, **kwargs: pytest.fail('A snapshot or read-only output check launched FFmpeg.'))
    archive = tmp_path / 'complete'
    receipt = create_backup(app, archive)
    verify_backup(archive, receipt)
    restored = restore_backup(archive, tmp_path / 'restored', receipt)
    replay = MediaConversionService(restored, ffmpeg='/optional-runtime-removed/ffmpeg')
    assert replay.definition(identity) == definition
    assert replay.verify(identity) == summary
    assert replay.export(identity, tmp_path / 'copied-results').is_dir()
    second = tmp_path / 'second-snapshot'
    second_receipt = create_backup(LibraryApp(restored), second)
    verify_backup(second, second_receipt)
    again = restore_backup(second, tmp_path / 'restored-again', second_receipt)
    assert MediaConversionService(again).verify(identity) == summary


def test_snapshot_never_owns_or_copies_a_live_native_job_lock(tmp_path):
    app, service, identity, _ = prepare(tmp_path)
    target = tmp_path / 'busy-snapshot'
    with project_lock(service.root / identity):
        with pytest.raises(RuntimeError):
            create_backup(app, target)
    assert not target.exists()
    assert service.verify(identity)['status'] == 'prepared'


@pytest.mark.parametrize('change', ['input-bytes', 'receipt-digest', 'output-bytes',
    'extra-file', 'empty-job', 'wrong-control', 'hardlink'], ids=[
    'retained-input', 'receipt-binding', 'output-checksum', 'unregistered-file',
    'orphan-empty-job', 'foreign-control', 'external-hardlink'])
def test_invalid_media_namespace_is_refused_before_backup_publication(tmp_path, change):
    app, service, identity, _ = prepare(tmp_path)
    service.run(identity)
    job = service.root / identity
    summary = service.verify(identity)
    output = job / 'results/000001' / summary['outputs'][0]['path']
    if change == 'input-bytes':
        target = job / 'inputs/000001.media'
        target.write_bytes(target.read_bytes()[:-2] + b'\x01\x02')
    elif change == 'receipt-digest':
        target = output.parent / 'receipt.json'
        value = read_json(target)
        value['definition_sha256'] = '0' * 64
        atomic_json(target, value)
    elif change == 'output-bytes':
        output.write_bytes(output.read_bytes()[:-2] + b'\x01\x02')
    elif change == 'extra-file':
        (job / 'unexpected.json').write_text('{}', encoding='utf-8')
    elif change == 'empty-job':
        (service.root / ('e' * 32)).mkdir()
    elif change == 'wrong-control':
        atomic_json(job / 'control.json', dict(schema='carr-media-control/1',
            id='f' * 32, run_id='1' * 32, action='cancel'))
    else:
        os.link(output, tmp_path / 'untracked-alias.wav')
    target = tmp_path / 'refused'
    with pytest.raises((ValueError, RuntimeError, OSError)):
        create_backup(app, target)
    assert not target.exists()


def test_denied_optional_namespace_lookup_is_never_silently_omitted(tmp_path, monkeypatch):
    app, service, _, _ = prepare(tmp_path)
    original = Path.lstat
    def denied(path, *args, **kwargs):
        if path == service.root:
            raise PermissionError('Synthetic denied optional media root')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', denied)
    with pytest.raises(PermissionError):
        media_backup.state_paths(app.root)
    target = tmp_path / 'denied'
    with pytest.raises(PermissionError):
        create_backup(app, target)
    assert not target.exists()


def test_snapshot_ignores_only_unacknowledged_exact_staging_and_refuses_linked_stage(tmp_path):
    app, service, identity, _ = prepare(tmp_path)
    job = service.root / identity
    stage = job / '.work-abcdefgh'
    stage.mkdir()
    (stage / 'unacknowledged.partial').write_bytes(b'not a completed result')
    files = media_backup.state_paths(app.root)
    assert not any(stage in path.parents for path in files)
    with closing(Storage(app.database, schema_version=CURRENT_SCHEMA_VERSION)) as storage:
        media_backup.validate_state_tree(storage.db, app.root)
    os.link(stage / 'unacknowledged.partial', tmp_path / 'unregistered-stage-alias')
    with pytest.raises(ValueError):
        media_backup.state_paths(app.root)


def test_stale_control_is_retained_as_data_and_cannot_cancel_a_new_restored_run(tmp_path):
    app, service, identity, _ = prepare(tmp_path)
    job = service.root / identity
    atomic_json(job / 'control.json', dict(schema='carr-media-control/1',
        id=identity, run_id='1' * 32, action='cancel'))
    target = tmp_path / 'queued-with-old-control'
    receipt = create_backup(app, target)
    restored = restore_backup(target, tmp_path / 'restored', receipt)
    replay = MediaConversionService(restored, ffmpeg=resolve_ffmpeg())
    assert replay.run(identity)['status'] == 'complete'


def test_snapshot_guard_matches_actual_project_lock_without_exporting_native_lock_bytes(tmp_path):
    app, service, identity, _ = prepare(tmp_path)
    before = media_backup.state_paths(app.root)
    job = service.root / identity
    with media_backup.snapshot_guard(app.root):
        assert (job / '.render.lock').stat().st_size == 1
        assert media_backup.state_paths(app.root) == before
        assert not any(path.name.endswith('.lock') for path in before)
        with pytest.raises(RuntimeError):
            with project_lock(job):
                pytest.fail('The backup guard did not acquire the actual renderer lock.')
    assert media_backup.state_paths(app.root) == before
    with project_lock(job):
        assert media_backup.state_paths(app.root) == before


@pytest.mark.parametrize('case', [
    'obsolete-job-name', 'namespace-render-name', 'input-render-name', 'oversized-render', 'aliased-render',
])
def test_only_exact_current_native_lock_locations_are_excluded(tmp_path, case):
    app, service, identity, _ = prepare(tmp_path)
    job = service.root / identity
    if case == 'obsolete-job-name':
        path, raw = job / '.job.lock', b'0'
    elif case == 'namespace-render-name':
        path, raw = service.root / '.render.lock', b'0'
    elif case == 'input-render-name':
        path, raw = job / 'inputs/.render.lock', b'0'
    else:
        path, raw = job / '.render.lock', b'00' if case == 'oversized-render' else b'0'
    path.write_bytes(raw)
    if case == 'aliased-render':
        os.link(path, tmp_path / 'unregistered-native-lock-alias')
    with pytest.raises((ValueError, OSError)):
        media_backup.state_paths(app.root)
    assert path.read_bytes() == raw
