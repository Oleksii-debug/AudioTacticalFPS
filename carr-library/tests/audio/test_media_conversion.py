"""Actual local FFmpeg encode/decode, immutable inputs and resumable batches."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import threading
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import media_conversion as store, media_worker as worker
from carr_library.audio.capture_process import run_process
from carr_library.audio.capture_types import AudioBudget, AudioCleanupPending
from carr_library.audio.jobs import atomic_json, read_json
from carr_library.audio.media_conversion import MediaConversionService
from carr_library.audio.media_types import (AudioStream, ConversionAllowance, ConversionPreset,
    MediaChoice, output_name, validate_definition, validate_presets, validate_receipt)
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.operations.broker import ResourceBroker
from carr_library.operations.facade import OperationFacade
from carr_library.operations.models import ResourceLimits


def wav(path, *, value=1000, seconds=.2, rate=24000):
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(struct.pack('<h', value) * round(seconds * rate))
    return path


@pytest.fixture
def environment(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    app.create_workspace('Synthetic media tests')
    ffmpeg = resolve_ffmpeg()
    broker = ResourceBroker.for_app(app.root, limits=ResourceLimits(cpu_slots=2,
        memory_bytes=1024**3, disk_bytes=256 * 1024**2, min_free_disk_bytes=0))
    service = MediaConversionService(app.root, ffmpeg=ffmpeg, broker=broker)
    source = wav(tmp_path / 'Original Україна Ω.wav')
    return app, service, source, broker


def prepared(environment, *, preset=None, sources=None, allowance=None):
    _, service, source, _ = environment
    inspected = service.inspect(sources or [source])
    choices = [MediaChoice(value, value.streams[0].index, preset or ConversionPreset()) for value in inspected]
    return service.prepare(choices, allowance=allowance or ConversionAllowance(disk_bytes=32 * 1024**2))


@pytest.mark.parametrize('format,bits', [('wav', 16), ('wav', 24), ('aiff', 16), ('aiff', 24),
    ('flac', 16), ('flac', 24), ('mp3', 16), ('ogg', 16), ('opus', 16), ('m4a', 16),
    ('m4b', 16), ('aac', 16), ('wma', 16)], ids=['wav16', 'wav24', 'aiff16', 'aiff24',
    'flac16', 'flac24', 'mp3', 'ogg', 'opus', 'm4a', 'm4b', 'aac', 'wma'])
def test_actual_formats_decode_and_export_without_altering_original(environment, tmp_path, format, bits):
    _, service, original, broker = environment
    before = original.read_bytes()
    job_id = prepared(environment, preset=ConversionPreset(format=format, pcm_bits=bits,
        title='Synthetic title Україна', artist='Synthetic author', album='Synthetic album'))
    assert service.jobs()[0]['status'] == 'prepared'
    assert service.run(job_id)['completed'] == 1
    summary = service.verify(job_id)
    assert summary['completed'] == 1 and summary['outputs'][0]['decoded_frames'] > 0
    assert summary['outputs'][0]['sample_rate'] == 48000 and summary['outputs'][0]['channels'] == 2
    definition = service.definition(job_id)
    output = service.root / job_id / 'results/000001' / summary['outputs'][0]['path']
    assert hashlib.sha256(output.read_bytes()).hexdigest() == summary['outputs'][0]['sha256']
    destination = service.export(job_id, tmp_path / 'reviewed export')
    assert (destination / output.name).read_bytes() == output.read_bytes()
    receipt = json.loads((destination / 'media-export.json').read_text(encoding='utf-8'))
    assert str(original) not in json.dumps(receipt) and definition['inputs'][0]['origin'] == str(original)
    assert original.read_bytes() == before
    assert broker.snapshot()['leases'] == []
    with pytest.raises(FileExistsError):
        service.export(job_id, destination)


def test_actual_video_extraction_uses_explicit_selected_audio_stream(environment, tmp_path):
    _, service, _, broker = environment
    one, two = wav(tmp_path / 'quiet.wav', value=1000, seconds=1), wav(tmp_path / 'loud.wav', value=12000, seconds=1)
    video = tmp_path / 'Two audio tracks Україна.mkv'
    run_process([str(resolve_ffmpeg()), '-hide_banner', '-nostdin', '-v', 'error', '-n',
        '-f', 'lavfi', '-i', 'color=c=black:s=16x16:r=1:d=1', '-i', str(one), '-i', str(two),
        '-map', '0:v:0', '-map', '1:a:0', '-map', '2:a:0', '-c:v', 'ffv1', '-c:a', 'pcm_s16le',
        '-metadata:s:a:0', 'title=First quiet track', '-metadata:s:a:1', 'title=Second loud track', str(video)],
        budget=AudioBudget(timeout=15, max_output_bytes=1024**2), owned_tree=True, watch=(video,))
    inspected = service.inspect([video])[0]
    assert inspected.has_video and len(inspected.streams) == 2
    assert inspected.streams[1].title == 'Second loud track'
    preset = ConversionPreset(sample_rate=24000, channels=1)
    job_id = service.prepare([MediaChoice(inspected, inspected.streams[1].index, preset)],
                             allowance=ConversionAllowance(disk_bytes=32 * 1024**2))
    service.run(job_id)
    result = service.verify(job_id)['outputs'][0]
    with wave.open(str(service.root / job_id / 'results/000001' / result['path']), 'rb') as output:
        data = output.readframes(output.getnframes())
    with wave.open(str(two), 'rb') as expected:
        assert data == expected.readframes(expected.getnframes())
    assert broker.snapshot()['leases'] == []
    with pytest.raises(ValueError, match='audio stream'):
        MediaChoice(inspected, 0, preset)  # This is the real video stream index.


def test_cancellation_between_items_preserves_first_output_and_resumes_only_pending(environment, monkeypatch, tmp_path):
    _, service, source, broker = environment
    second = wav(tmp_path / 'Other original.wav', value=2400)
    job_id = prepared(environment, sources=[source, second])
    cancel = threading.Event()
    def progress(done, total, state):
        if done == 1:
            cancel.set()
    with pytest.raises(InterruptedError):
        service.run(job_id, cancelled=cancel.is_set, progress=progress)
    first = service.verify(job_id)
    assert first['completed'] == 1 and first['status'] == 'cancelled'
    output = service.root / job_id / 'results/000001' / first['outputs'][0]['path']
    before = output.stat().st_ino, output.read_bytes()
    source.unlink(); second.unlink()
    called = []
    actual = worker.convert_item
    def observed(job, definition, row, *args, **kwargs):
        called.append(row['index'])
        return actual(job, definition, row, *args, **kwargs)
    monkeypatch.setattr(worker, 'convert_item', observed)
    assert service.run(job_id)['completed'] == 2
    assert called == [2] and (output.stat().st_ino, output.read_bytes()) == before
    assert broker.snapshot()['leases'] == []


def test_current_run_pause_control_is_acknowledged_without_reusing_old_control(environment):
    app, service, _, broker = environment
    job_id = prepared(environment)
    seen = []
    def progress(done, total, state):
        if not seen:
            view = OperationFacade(app, broker=broker).get('media', job_id)
            assert view.live and view.controls == ('pause', 'cancel')
            service.control(job_id, 'pause')
            seen.append(True)
    with pytest.raises(InterruptedError):
        service.run(job_id, progress=progress)
    assert service.verify(job_id)['status'] == 'paused'
    assert service.run(job_id)['status'] == 'complete'
    assert OperationFacade(app).get('media', job_id).controls == ()
    assert broker.snapshot()['leases'] == []


def test_cancel_after_last_publication_reports_durable_completion(environment):
    _, service, _, _ = environment
    job_id = prepared(environment)
    cancel = threading.Event()
    assert service.run(job_id, cancelled=cancel.is_set,
        progress=lambda done, total, state: cancel.set() if done == total else None)['status'] == 'complete'
    assert service.verify(job_id)['completed'] == 1


@pytest.mark.parametrize('when', ['after-inspection', 'after-preparation'])
def test_changed_input_bytes_are_refused_before_new_output(environment, when):
    _, service, source, broker = environment
    inspected = service.inspect([source])[0]
    if when == 'after-inspection':
        source.write_bytes(source.read_bytes()[:-2] + b'\x01\x09')
        with pytest.raises((ValueError, RuntimeError)):
            service.prepare([MediaChoice(inspected, 0)], allowance=ConversionAllowance(disk_bytes=32 * 1024**2))
        assert not service.jobs()
    else:
        job_id = service.prepare([MediaChoice(inspected, 0)], allowance=ConversionAllowance(disk_bytes=32 * 1024**2))
        owned = service.root / job_id / 'inputs/000001.media'
        owned.write_bytes(owned.read_bytes()[:-2] + b'\x01\x09')
        with pytest.raises(ValueError, match='checksum'):
            service.run(job_id)
        assert not list((service.root / job_id / 'results').iterdir())
    assert broker.snapshot()['leases'] == []


def test_matching_result_after_directory_sync_failure_is_resynced_before_resume(environment, monkeypatch):
    _, service, _, _ = environment
    job_id = prepared(environment)
    published = store.publish_noreplace
    def fail_after_move(source, target, **options):
        result = published(source, target, **options)
        if Path(target).parent.name == 'results':
            raise OSError('Synthetic post-move directory flush failure')
        return result
    with monkeypatch.context() as patch:
        patch.setattr(store, 'publish_noreplace', fail_after_move)
        with pytest.raises(OSError, match='post-move'):
            service.run(job_id)
    assert service.verify(job_id)['completed'] == 1
    original = store._sync_directory
    synced = []
    def sync(path):
        synced.append(Path(path).name)
        return original(path)
    monkeypatch.setattr(store, '_sync_directory', sync)
    monkeypatch.setattr(worker, 'convert_item', lambda *a, **k: pytest.fail('Completed output was encoded again.'))
    assert service.run(job_id)['status'] == 'complete' and 'results' in synced


def test_pending_family_cleanup_keeps_staging_and_resource_lease_alive(environment, monkeypatch):
    _, service, _, broker = environment
    job_id = prepared(environment)
    closed = []
    class Owner:
        quiescent_event = threading.Event()
        def close(self):
            assert list((service.root / job_id).glob('.work-*'))
            assert len(broker.snapshot()['leases']) == 1
            closed.append(True)
            self.quiescent_event.set()
    original = worker.run_process
    def refused(command, **options):
        if '-map' in command:
            raise AudioCleanupPending('Synthetic retained native owner', cleanup_owner=Owner())
        return original(command, **options)
    monkeypatch.setattr(worker, 'run_process', refused)
    with pytest.raises(AudioCleanupPending):
        service.run(job_id)
    assert closed and not list((service.root / job_id).glob('.work-*'))
    assert broker.snapshot()['leases'] == []


def test_verified_output_changed_before_receipt_publication_is_refused(environment, monkeypatch):
    _, service, _, _ = environment
    job_id = prepared(environment)
    actual = worker.convert_item
    def changed(*args, **kwargs):
        result = actual(*args, **kwargs)
        path = args[3] / result['path']
        path.write_bytes(path.read_bytes()[:-2] + b'\x12\x34')
        return result
    monkeypatch.setattr(worker, 'convert_item', changed)
    with pytest.raises(ValueError, match='checksum'):
        service.run(job_id)
    assert not list((service.root / job_id / 'results').iterdir())


def test_shared_item_deadline_includes_all_probe_and_decode_steps(environment, monkeypatch):
    _, service, _, broker = environment
    job_id = prepared(environment, allowance=ConversionAllowance(disk_bytes=32 * 1024**2, timeout_seconds=1))
    ticks = [0]
    actual = worker._run
    def consumed(command, **options):
        result = actual(command, **options)
        ticks[0] += 600_000_000
        return result
    monkeypatch.setattr(worker.time, 'perf_counter_ns', lambda: ticks[0])
    monkeypatch.setattr(worker, '_run', consumed)
    with pytest.raises(TimeoutError):
        service.run(job_id)
    assert not list((service.root / job_id / 'results').iterdir())
    assert broker.snapshot()['leases'] == []


def test_metadata_presets_are_inert_typed_and_never_overwrite(environment):
    _, service, _, _ = environment
    preset = ConversionPreset(format='flac', pcm_bits=24, title='Reviewed title')
    service.save_preset('Lossless Україна', preset)
    assert service.presets()['Lossless Україна'] == preset.to_dict()
    with pytest.raises(FileExistsError):
        service.save_preset('Lossless Україна', ConversionPreset())
    with pytest.raises(ValueError):
        service.save_preset('LOSSLESS УКРАЇНА', ConversionPreset())
    with pytest.raises(ValueError):
        ConversionPreset.from_dict(dict(preset.to_dict(), arguments='-f lavfi'))


@pytest.mark.parametrize('field,value', [('channels', True), ('sample_rate', 12345), ('pcm_bits', 32),
    ('bitrate_kbps', 0), ('format', 'http'), ('title', 'line\nbreak')],
    ids=['boolean-channels', 'unknown-rate', 'unsupported-depth', 'zero-bitrate', 'url-format', 'control-tag'])
def test_invalid_encoding_fields_are_rejected(field, value):
    with pytest.raises(ValueError):
        ConversionPreset(**{field: value})


@pytest.mark.parametrize('format', ['wma', 'opus'])
def test_native_codec_rate_is_an_explicit_choice(format):
    with pytest.raises(ValueError, match='48000'):
        ConversionPreset(format=format, sample_rate=96000)


def test_playlist_and_video_without_audio_are_not_silent_audio_defaults(environment, tmp_path):
    _, service, source, broker = environment
    playlist = tmp_path / 'unselected local reference.m3u'
    playlist.write_text('#EXTM3U\n' + str(source) + '\n', encoding='utf-8')
    with pytest.raises((ValueError, RuntimeError)):
        service.inspect([playlist])
    assert broker.snapshot()['leases'] == []


def test_source_link_is_not_imported_as_an_independent_original(environment, tmp_path):
    _, service, source, _ = environment
    alias = tmp_path / 'aliased.wav'
    os.link(source, alias)
    with pytest.raises(ValueError):
        service.inspect([source])


def test_readonly_output_verification_does_not_require_the_optional_runtime(environment, monkeypatch):
    _, service, _, _ = environment
    job_id = prepared(environment)
    service.run(job_id)
    service.ffmpeg = '/not-installed/ffmpeg'
    monkeypatch.setattr(worker, 'run_process', lambda *a, **k: pytest.fail('Readonly verification launched a processor.'))
    assert service.verify(job_id)['completed'] == 1


def test_export_carries_the_parent_selected_before_a_later_native_confirmation(environment, tmp_path):
    _, service, _, _ = environment
    job_id = prepared(environment)
    service.run(job_id)
    parent = tmp_path / 'chosen-parent'
    parent.mkdir()
    info = parent.stat()
    approved = (info.st_dev, info.st_ino)
    parent.rename(tmp_path / 'former-parent')
    parent.mkdir()
    with pytest.raises(ValueError, match='reviewed export parent'):
        service.export(job_id, parent / 'new-export', parent_identity=approved)
    assert not list(parent.iterdir())
    assert service.verify(job_id)['status'] == 'complete'
