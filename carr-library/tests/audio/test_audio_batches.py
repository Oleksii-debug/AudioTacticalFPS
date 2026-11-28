"""Real pinned-FFmpeg queue execution, pause/retry and durable output boundaries."""
from dataclasses import replace
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import shutil
import struct
import wave

import pytest

from carr_library.application import LibraryApp
from carr_library.audio import batches, jobs, media_conversion
from carr_library.audio.batch_models import AudioPreset, Selection
from carr_library.audio.batches import AudioBatchService
from carr_library.audio.ffmpeg import wav_info
from carr_library.audio.runtime import resolve_ffmpeg
from carr_library.audio.recording_process import CaptureBudget, run_process
from carr_library.audio.cleanup import AudioCleanupPending, CleanupOwner
from carr_library.research.export_file import capture_export_destination
from carr_library.ui.audio_studio import StudioController


@pytest.fixture
def ffmpeg():
    binary = resolve_ffmpeg()
    assert binary is not None and binary.is_file(), 'This strict suite requires actual pinned CARR_FFMPEG.'
    return binary


def pcm(path, *, seconds=.25, rate=48000, frequency=500):
    frames = round(seconds * rate)
    with wave.open(str(path), 'wb') as output:
        output.setparams((1, 2, rate, frames, 'NONE', 'not compressed'))
        output.writeframes(b''.join(struct.pack('<h', round(5000 * math.sin(n * 2 * math.pi * frequency / rate)))
                                   for n in range(frames)))
    return path


def preset(format='wav', *, split='whole'):
    return AudioPreset(label='Synthetic explicit preset', output_format=format, sample_rate=48000,
                       channels=1, max_seconds=5, timeout_seconds=30, split=split)


def service_at(tmp_path, ffmpeg, *, voice=None):
    app = LibraryApp(tmp_path / 'library')
    service = AudioBatchService(app.root, ffmpeg=ffmpeg, registry=voice)
    return app, service


def local_queue(tmp_path, ffmpeg, *, formats=('wav',), source=None):
    app, service = service_at(tmp_path, ffmpeg)
    source = source or pcm(tmp_path / 'original.wav')
    original = source.read_bytes()
    state = service.create(tuple(Selection('audio', str(source), 'Exact selected source', preset(format))
                                 for format in formats))
    return app, service, state, source, original


def output_file(service, batch, index=0, output=0):
    item = batch.items[index]
    return service.path(batch.id) / 'outputs' / item.id / item.outputs[output]


@pytest.mark.parametrize('format', ['wav', 'flac', 'mp3', 'opus', 'm4a', 'm4b', 'aiff', 'ogg', 'aac', 'wma'])
def test_each_existing_output_format_is_actually_encoded_and_decoded_before_completion(tmp_path, ffmpeg, format):
    _, service, state, source, original = local_queue(tmp_path, ffmpeg, formats=(format,))
    result = service.run(state.id)
    assert result.status == 'complete', [item.error for item in result.items]
    item = result.items[0]
    assert item.state == 'complete' and item.attempt == 1
    record = service.verified_receipt(result, item)
    row = record['files'][0]
    assert row['sha256'] == sha256(output_file(service, result).read_bytes()).hexdigest()
    assert row['sample_rate'] == 48000 and row['channels'] == 1 and row['frames'] > 0
    assert source.read_bytes() == original
    assert not list(service.path(state.id).glob('.pending-item-*'))
    assert service.broker.snapshot()['leases'] == []


def test_selected_original_can_move_after_independent_capture_and_completed_replay_is_exact(tmp_path, ffmpeg):
    _, service, state, source, original = local_queue(tmp_path, ffmpeg)
    source.unlink()
    completed = service.run(state.id)
    assert completed.status == 'complete'
    output = output_file(service, completed)
    before = (output.stat().st_mtime_ns, output.read_bytes())
    replayed = service.run(state.id)
    assert replayed.status == 'complete' and replayed.items[0].attempt == 1
    assert (output.stat().st_mtime_ns, output.read_bytes()) == before
    assert (service.path(state.id) / 'sources' / state.items[0].source.file).read_bytes() == original


def test_actual_video_extraction_uses_the_explicit_second_audio_stream(tmp_path, ffmpeg):
    video = tmp_path / 'two-local-streams.mkv'
    run_process([str(ffmpeg), '-hide_banner', '-nostdin', '-loglevel', 'error',
                 '-f', 'lavfi', '-i', 'color=c=black:s=16x16:r=4:d=0.5',
                 '-f', 'lavfi', '-i', 'sine=frequency=400:sample_rate=48000:duration=0.5',
                 '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000:duration=0.5',
                 '-map', '0:v:0', '-map', '1:a:0', '-map', '2:a:0',
                 '-c:v', 'ffv1', '-c:a', 'pcm_s16le', '-f', 'matroska', '-n', str(video)],
                budget=CaptureBudget(timeout=30, max_output_bytes=8 * 1024**2),
                watch=(video,), owned_tree=True)
    original = video.read_bytes()
    _, service = service_at(tmp_path, ffmpeg)
    state = service.create((Selection('video', str(video), 'Chosen second stream', preset(), audio_stream=1),))
    result = service.run(state.id)
    assert result.status == 'complete', result.items[0].error
    info = wav_info(output_file(service, result))
    with output_file(service, result).open('rb') as source:
        source.seek(info.data_offset)
        values = struct.unpack('<' + 'h' * info.frames, source.read(info.data_size))
    def power(frequency):
        real = sum(sample * math.cos(2 * math.pi * frequency * n / info.rate) for n, sample in enumerate(values))
        imag = sum(sample * math.sin(2 * math.pi * frequency * n / info.rate) for n, sample in enumerate(values))
        return real * real + imag * imag
    assert power(1000) > power(400) * 100
    assert video.read_bytes() == original
    assert service.verified_receipt(result, result.items[0])['files'][0]['frames'] == info.frames


def test_missing_explicit_audio_stream_cannot_fall_back_or_publish(tmp_path, ffmpeg):
    _, service = service_at(tmp_path, ffmpeg)
    source = pcm(tmp_path / 'one-stream.wav')
    state = service.create((Selection('audio', str(source), 'Missing stream', preset(), audio_stream=1),))
    result = service.run(state.id)
    assert result.status == result.items[0].state == 'failed'
    assert not (service.path(state.id) / 'outputs' / state.items[0].id).exists()


@pytest.mark.parametrize('action', ['pause', 'cancel'])
def test_mid_queue_control_preserves_completed_items_and_explicit_resume_finishes(tmp_path, ffmpeg, action):
    _, service, state, source, original = local_queue(tmp_path, ffmpeg, formats=('wav', 'flac'))
    issued = []
    def progress(message):
        if message.startswith('Converting item 2 ') and not issued:
            issued.append(service.control(state.id, action))
    stopped = service.run(state.id, on_status=progress)
    assert stopped.status == ('paused' if action == 'pause' else 'cancelled')
    assert [item.state for item in stopped.items] == ['complete', stopped.status]
    first = output_file(service, stopped)
    before = (first.stat().st_mtime_ns, first.read_bytes())
    with pytest.raises(ValueError, match='Resume'):
        service.run(state.id)
    result = service.run(state.id, action='resume')
    assert result.status == 'complete'
    assert [item.attempt for item in result.items] == [1, 2]
    assert (first.stat().st_mtime_ns, first.read_bytes()) == before
    assert source.read_bytes() == original


def test_retry_only_failed_item_retains_the_other_verified_output(tmp_path, ffmpeg, monkeypatch):
    _, service, state, _, _ = local_queue(tmp_path, ffmpeg, formats=('wav', 'flac'))
    actual, calls = media_conversion.encode, []
    def encode(source, target, **kwargs):
        calls.append(Path(target).suffix)
        if Path(target).suffix == '.wav' and calls.count('.wav') == 1:
            raise RuntimeError('Injected encoder boundary failure')
        return actual(source, target, **kwargs)
    monkeypatch.setattr(media_conversion, 'encode', encode)
    failed = service.run(state.id)
    assert [item.state for item in failed.items] == ['failed', 'complete']
    completed = output_file(service, failed, 1)
    before = completed.read_bytes(), completed.stat().st_mtime_ns
    retry = service.run(state.id, action='retry', retry_items=(failed.items[0].id,))
    assert retry.status == 'complete'
    assert calls == ['.wav', '.flac', '.wav']
    assert (completed.read_bytes(), completed.stat().st_mtime_ns) == before
    assert [item.attempt for item in retry.items] == [2, 1]


def test_changed_retained_source_is_not_hidden_by_same_size_or_restored_mtime(tmp_path, ffmpeg):
    _, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    captured = service.path(state.id) / 'sources' / state.items[0].source.file
    stamp, raw = captured.stat(), bytearray(captured.read_bytes())
    raw[-1] ^= 1
    captured.write_bytes(raw)
    os.utime(captured, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    result = service.run(state.id)
    assert result.status == 'failed' and 'changed' in result.items[0].error.lower()
    assert not output_file(service, result).exists()


def test_actual_runtime_byte_change_requires_a_new_plan_before_any_conversion(tmp_path, ffmpeg):
    selected = tmp_path / ffmpeg.name
    shutil.copyfile(ffmpeg, selected)
    if os.name != 'nt':
        selected.chmod(0o700)
    _, service, state, _, _ = local_queue(tmp_path, selected)
    with selected.open('ab') as output:
        output.write(b'\nexplicit-runtime-identity-change')
    with pytest.raises((ValueError, RuntimeError)):
        service.run(state.id)
    assert service.load(state.id).items[0].attempt == 0
    assert not output_file(service, state).exists()


def test_existing_foreign_output_directory_is_never_replaced(tmp_path, ffmpeg):
    _, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    destination = service.path(state.id) / 'outputs' / state.items[0].id
    destination.mkdir()
    foreign = destination / 'someone-elses-file'
    foreign.write_bytes(b'keep exact bytes')
    result = service.run(state.id)
    assert result.status == 'failed'
    assert foreign.read_bytes() == b'keep exact bytes'
    assert not (destination / state.items[0].outputs[0]).exists()


def test_post_move_flush_failure_retries_the_receipt_without_reencoding(tmp_path, ffmpeg, monkeypatch):
    _, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    destination = service.path(state.id) / 'outputs' / state.items[0].id
    original_sync, failing, encodes = jobs._sync_directory, [True], []
    actual_encode = media_conversion.encode
    def encode(*args, **kwargs):
        encodes.append(True)
        return actual_encode(*args, **kwargs)
    def sync(path):
        if destination.exists() and Path(path) in (destination, destination.parent) and failing[0]:
            raise OSError('Injected completed-directory flush failure')
        return original_sync(path)
    monkeypatch.setattr(media_conversion, 'encode', encode)
    monkeypatch.setattr(jobs, '_sync_directory', sync)
    monkeypatch.setattr(batches, '_sync_directory', sync)
    result = service.run(state.id)
    assert result.status == 'failed' and destination.exists()
    before = output_file(service, result).read_bytes()
    again = service.run(state.id, action='retry')
    assert again.status == 'failed' and encodes == [True]
    failing[0] = False
    repaired = service.run(state.id, action='retry')
    assert repaired.status == 'complete' and encodes == [True]
    assert output_file(service, repaired).read_bytes() == before


def test_cancel_after_actual_atomic_move_finishes_that_committed_item(tmp_path, ffmpeg, monkeypatch):
    _, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    destination = service.path(state.id) / 'outputs' / state.items[0].id
    actual = batches.publish_noreplace
    def publish(source, target, **kwargs):
        result = actual(source, target, **kwargs)
        if Path(target) == destination:
            service.control(state.id, 'cancel')
        return result
    monkeypatch.setattr(batches, 'publish_noreplace', publish)
    result = service.run(state.id)
    assert result.status == result.items[0].state == 'complete'
    assert service.verified_receipt(result, result.items[0]) is not None


def test_pending_native_cleanup_keeps_stage_and_resource_lease_until_settled(tmp_path, ffmpeg, monkeypatch):
    _, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    original = media_conversion.run_process
    observations = []
    owner = None
    def start(args, **kwargs):
        nonlocal owner
        if '-i' not in args:
            return original(args, **kwargs)
        watched = Path(kwargs['watch'][0])
        def settle():
            observations.append((watched.parent.exists(), len(service.broker.snapshot()['leases'])))
            return True
        owner = CleanupOwner(settle)
        raise AudioCleanupPending('Injected retained owner boundary', cleanup_owner=owner)
    monkeypatch.setattr(media_conversion, 'run_process', start)
    with pytest.raises(AudioCleanupPending):
        service.run(state.id)
    assert owner.quiescent_event.is_set()
    assert observations == [(True, 1)]
    assert service.broker.snapshot()['leases'] == []
    assert not list(service.path(state.id).glob('.pending-item-*'))
    assert not output_file(service, state).exists()


class SyntheticVoice:
    def __init__(self):
        self.calls = []
    def cache_identity(self, voice, **kwargs):
        return {'fixture': 'batch-book-tone-v1'}
    def synthesize(self, text, output, **kwargs):
        self.calls.append(text)
        return pcm(output, rate=8000, seconds=.2, frequency=600)


def save_book(app, ffmpeg, voice, script, title):
    studio = StudioController(private_root=app.root, registry=voice, ffmpeg=ffmpeg)
    studio.new(script, title=title, source_id='batch-synthetic-book:' + title, input_kind='script')
    studio.configure(voice='fixture:voice')
    studio.save_in_library()
    return studio


def test_two_books_keep_exact_source_drafts_and_explicit_whole_chapter_previews(tmp_path, ffmpeg):
    voice = SyntheticVoice()
    app, service = service_at(tmp_path, ffmpeg, voice=voice)
    first = save_book(app, ffmpeg, voice, '# One\nNarrator: Exact first.\n# Two\nNarrator: Exact second.', 'Book A')
    second = save_book(app, ffmpeg, voice, '# Only\nNarrator: Another book.', 'Book B')
    originals = {studio.project_id: studio.project_path.read_bytes() for studio in (first, second)}
    state = service.create((
        Selection('book', first.project_id, 'Delivered book', preset('wav', split='chapters')),
        Selection('book', second.project_id, 'Delivered book', preset('flac', split='whole'))))
    assert [len(item.outputs) for item in state.items] == [2, 1]
    assert len({name for item in state.items for name in item.outputs}) == 3
    result = service.run(state.id)
    assert result.status == 'complete', [item.error for item in result.items]
    for item, studio in zip(result.items, (first, second)):
        assert studio.project_path.read_bytes() == originals[studio.project_id]
        child = service.library / 'audio-projects' / item.id / 'studio' / 'project.json'
        source = json.loads(originals[studio.project_id])
        copied = json.loads(child.read_text(encoding='utf-8'))
        assert copied['draft'] == source['draft'] and copied['source_reference'] == source['source_reference']
        assert copied['project_id'] == item.id and copied['output_dir'] is None
        assert service.verified_receipt(result, item)['render']['source_sha256'] == studio.plan().source_sha256
    calls = list(voice.calls)
    assert service.run(state.id).status == 'complete'
    assert voice.calls == calls


def test_independent_export_reuses_verified_data_and_refuses_a_changed_destination(tmp_path, ffmpeg):
    _, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    result = service.run(state.id)
    item = result.items[0]
    target = tmp_path / 'exported.wav'
    approval = capture_export_destination(target, private_root=service.library)
    exported = service.export_file(state.id, item.id, item.outputs[0], target, approval=approval)
    assert exported['status'] == 'complete' and target.read_bytes() == output_file(service, result).read_bytes()
    assert target.stat().st_nlink == 1
    another = tmp_path / 'changed.wav'
    captured = capture_export_destination(another, private_root=service.library)
    another.write_bytes(b'pre-existing data')
    with pytest.raises((ValueError, FileExistsError)):
        service.export_file(state.id, item.id, item.outputs[0], another, approval=captured)
    assert another.read_bytes() == b'pre-existing data'


def test_uncommitted_derived_stage_is_recovered_without_touching_completed_outputs(tmp_path, ffmpeg):
    _, service, state, _, _ = local_queue(tmp_path, ffmpeg)
    stage = service.path(state.id) / ('.pending-item-' + state.items[0].id + '-crashed')
    (stage / 'work').mkdir(parents=True)
    (stage / 'publish').mkdir()
    jobs.atomic_json(stage / 'owner.json', {'batch_id': state.id, 'item_id': state.items[0].id,
                                           'plan_sha256': state.plan_sha256})
    (stage / 'work' / 'decoded.wav').write_bytes(b'partial derived PCM')
    result = service.run(state.id, action='resume')
    assert result.status == 'complete' and not stage.exists()
    assert service.verified_receipt(result, result.items[0]) is not None


def test_cumulative_staged_output_cannot_exceed_the_reserved_item_budget(tmp_path, ffmpeg, monkeypatch):
    _, service, state, source, original = local_queue(tmp_path, ffmpeg)
    actual = media_conversion.encode
    limit = state.items[0].preset.pcm_budget * 4 + 2 * batches.MAX_STATE_BYTES
    def oversized(source, target, **kwargs):
        actual(source, target, **kwargs)
        # A sparse derived fixture tests aggregate accounting without consuming
        # an equivalent amount of physical runner disk or changing the original.
        with Path(target).open('r+b') as output:
            output.seek(limit)
            output.write(b'\0')
    monkeypatch.setattr(media_conversion, 'encode', oversized)
    failed = service.run(state.id)
    assert failed.status == failed.items[0].state == 'failed'
    assert 'cumulative' in failed.items[0].error and 'disk allowance' in failed.items[0].error
    assert not (service.path(state.id) / 'outputs' / state.items[0].id).exists()
    assert not list(service.path(state.id).glob('.pending-item-*'))
    assert service.broker.snapshot()['leases'] == [] and source.read_bytes() == original
