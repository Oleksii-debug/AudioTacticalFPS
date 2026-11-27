"""Actual current renders, PCM bytes and publication boundaries.

Synthetic ToneEngine is not speech, device or model qualification. This strict
suite needs the already selected FFmpeg only to create its completed fixtures;
all measured review/export/restore work must succeed after runtime removal.
"""
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import struct
from threading import Event
from zipfile import ZipFile, ZIP_STORED

import pytest

from carr_library.audio import ffmpeg, jobs, measured_audio, publishing
from carr_library.audio.book_audio import BookAudioService
from carr_library.audio.models import canonical
from tests.audio.test_book_plans import library, make_plan
from tests.audio.test_production import ToneEngine


@pytest.fixture
def completed(library, tmp_path, monkeypatch):
    app, workspace, *_ = library
    executable = Path(os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg') or '/missing-ffmpeg')
    assert executable.is_file(), 'The strict completed-PCM fixture requires actual selected FFmpeg.'
    engine = ToneEngine()
    books = BookAudioService(app, workspace, engine=engine, ffmpeg=executable)
    item = app.add_text(workspace, 'Measured retained book',
                        '[music ../secret]\n' + 'Literal source sentence. ' * 400)
    plan = make_plan(library, item=item)
    output = tmp_path / 'original-export'
    output.mkdir()
    review = books.review(plan, output)
    ready = books.prepare(plan, output, approval=review.fingerprint)
    result, = books.run(plan.id, output, approval=ready.fingerprint)
    assert result['status'] == 'complete' and engine.calls
    _, _, audio = books.managed.paths(plan.job_id(1))
    def no_runtime(*args, **kwargs):
        raise AssertionError('Measured completed-audio work must not discover or execute a runtime')
    import carr_library.audio.runtime as runtime
    monkeypatch.setattr(runtime, 'resolve_ffmpeg', no_runtime)
    monkeypatch.setattr(BookAudioService, '_engine', no_runtime)
    monkeypatch.setattr(ffmpeg, 'run_ffmpeg', no_runtime)
    return books, plan, result, audio


def split(completed, output, maximum_ms=70, **kwargs):
    books, plan, *_ = completed
    review = books.review_duration(plan.id, 1, output, maximum_ms=maximum_ms)
    result = books.publish_duration(plan.id, 1, output, maximum_ms=maximum_ms,
                                    review_sha256=review['approval_sha256'], **kwargs)
    return review, result


def pcm(path):
    info = ffmpeg.wav_info(path)
    with jobs.verified_reader(path) as stream:
        stream.seek(info.data_offset)
        return stream.read(info.data_size)


def test_actual_pcm_parts_exact_concatenation_original_unchanged_and_offline_receipt(completed, tmp_path):
    books, plan, result, audio = completed
    master = audio / 'renders' / result['run_key'] / 'master.wav'
    original = master.read_bytes()
    output = tmp_path / 'Виміряні частини ü.zip'
    review, receipt = split(completed, output)
    assert receipt['format'] == 'pcm-parts' and receipt['status'] == 'complete'
    assert receipt['source']['book']['plan_sha256'] == plan.fingerprint
    parts = review['content']['parts']
    assert 2 <= len(parts) <= 256
    assert all(row['frames'] <= 70 * plan.sample_rate // 1000 for row in parts)
    assert parts[0]['start_frame'] == 0
    assert all(a['end_frame'] == b['start_frame'] for a, b in zip(parts, parts[1:]))
    assert any(row['partial_line'] for part in parts for row in part['source_lines'])
    assert all(row['scope'] == 'whole-line' and 'no character or word alignment' in row['alignment']
               for part in parts for row in part['source_lines'])
    with ZipFile(output) as archive:
        reconstructed = []
        for part in parts:
            raw = archive.read(part['name'])
            header = measured_audio._header(review['content']['pcm'], part['frames'])
            assert raw.startswith(header)
            reconstructed.append(raw[len(header):])
        assert b''.join(reconstructed) == pcm(master)
        original_book = json.loads(archive.read('source/book-plan.json'))
        assert original_book == plan.to_dict()
    assert master.read_bytes() == original
    verified = books.verify_duration(plan.id, 1, receipt['id'])
    assert verified == receipt
    assert measured_audio.inspect(output)['source'] == receipt['source']


def test_small_actual_rf64_master_uses_the_same_exact_reader(completed, tmp_path):
    books, plan, result, audio = completed
    directory = audio / 'renders' / result['run_key']
    master = directory / 'master.wav'
    info = ffmpeg.wav_info(master)
    data = pcm(master)
    fmt = struct.pack('<HHIIHH', 1, info.channels, info.rate, info.rate * info.channels * 2,
                      info.channels * 2, 16)
    raw = (b'RF64\xff\xff\xff\xffWAVEds64' + struct.pack('<IQQQI', 28, len(data) + 72, len(data), info.frames, 0)
           + b'fmt ' + struct.pack('<I', 16) + fmt + b'data\xff\xff\xff\xff' + data)
    master.write_bytes(raw)
    marker = jobs.read_json(directory / 'complete.json', limit=16 * 1024**2)
    entry = next(row for row in marker['files'] if row['name'] == 'master.wav')
    entry.update(size=len(raw), sha256=sha256(raw).hexdigest())
    jobs.atomic_json(directory / 'complete.json', marker)
    output = tmp_path / 'rf64.zip'
    review, _ = split(completed, output)
    assert review['content']['pcm']['pcm_sha256'] == sha256(data).hexdigest()
    assert measured_audio.inspect(output)['content']['pcm']['frames'] == info.frames


def test_rf64_part_header_and_bounds_do_not_allocate_full_pcm():
    pcm_info = {'sample_rate': 48000, 'channels': 2, 'sample_width': 2,
                'frames': 2**31, 'pcm_sha256': '0' * 64}
    header = measured_audio._header(pcm_info, pcm_info['frames'])
    assert len(header) == 80 and header[:4] == b'RF64'
    assert struct.unpack('<Q', header[28:36])[0] == pcm_info['frames'] * 4


@pytest.mark.parametrize('change', ['time', 'template', 'destination', 'plan'])
def test_review_pin_rejects_changed_scope_before_output(completed, tmp_path, change):
    books, plan, *_ = completed
    output = tmp_path / 'reviewed.zip'
    review = books.review_duration(plan.id, 1, output, maximum_ms=70)
    kwargs = dict(maximum_ms=70, review_sha256=review['approval_sha256'])
    if change == 'time':
        kwargs['maximum_ms'] = 80
    elif change == 'template':
        kwargs['name_template'] = '{index} - changed'
    elif change == 'destination':
        output = tmp_path / 'other.zip'
    else:
        # This changes the saved original Book review, not just a UI label.
        path = books.store.path(plan.id, 'plans')
        envelope = jobs.read_json(path, limit=32 * 1024**2)
        envelope['value']['role']['pan'] = .5
        envelope['sha256'] = sha256(__import__('carr_library.audio.book_plan', fromlist=['encode']).encode(
            envelope['value'])).hexdigest()
        jobs.atomic_json(path, envelope)
    with pytest.raises(ValueError):
        books.publish_duration(plan.id, 1, output, **kwargs)
    assert not output.exists()


def test_part_count_budget_is_real_pcm_not_an_estimated_text_duration(completed, tmp_path):
    books, plan, _, audio = completed
    with pytest.raises(ValueError, match='256'):
        books.review_duration(plan.id, 1, tmp_path / 'too-many.zip', maximum_ms=1)
    assert not list(audio.glob('publication-*.json'))


def test_post_move_sync_failure_retains_prepared_receipt_for_exact_verify(completed, tmp_path, monkeypatch):
    books, plan, *_ = completed
    output = tmp_path / 'recover.zip'
    real = publishing.publish_noreplace
    def committed_then_failed(source, destination, **kwargs):
        real(source, destination, **kwargs)
        raise OSError('injected post-move directory fsync failure')
    monkeypatch.setattr(publishing, 'publish_noreplace', committed_then_failed)
    with pytest.raises(RuntimeError, match='explicit verification'):
        split(completed, output)
    receipt, = books.duration_publications(plan.id, 1)
    assert receipt['status'] == 'prepared' and output.is_file()
    verified = books.verify_duration(plan.id, 1, receipt['id'])
    assert verified['status'] == 'complete'
    with pytest.raises((FileExistsError, ValueError)):
        books.review_duration(plan.id, 1, output, maximum_ms=70)


def test_cancellation_during_final_flush_keeps_no_output(completed, tmp_path, monkeypatch):
    books, plan, *_ = completed
    output, stopped = tmp_path / 'cancelled.zip', Event()
    review = books.review_duration(plan.id, 1, output, maximum_ms=70)
    real = jobs.os.fsync
    def fsync(descriptor):
        real(descriptor)
        for stage in tmp_path.glob('.publication-*/publication.zip'):
            current = stage.stat()
            opened = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                stopped.set()
    monkeypatch.setattr(jobs.os, 'fsync', fsync)
    with pytest.raises(publishing.PublicationCancelled):
        books.publish_duration(plan.id, 1, output, maximum_ms=70,
            review_sha256=review['approval_sha256'], cancelled=stopped.is_set)
    assert stopped.is_set() and not output.exists()
    receipt, = books.duration_publications(plan.id, 1)
    assert receipt['status'] == 'prepared'


def test_source_revalidation_after_final_flush_blocks_publication(completed, tmp_path, monkeypatch):
    books, plan, *_ = completed
    output = tmp_path / 'revoked.zip'
    review = books.review_duration(plan.id, 1, output, maximum_ms=70)
    real_fsync, validate = jobs.os.fsync, books._validate
    revoked = False
    def fsync(descriptor):
        nonlocal revoked
        real_fsync(descriptor)
        for stage in tmp_path.glob('.publication-*/publication.zip'):
            opened, current = os.fstat(descriptor), stage.stat()
            if (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino):
                revoked = True
    def guarded(*args):
        if revoked:
            raise ValueError('Source was revoked while flushing.')
        return validate(*args)
    monkeypatch.setattr(jobs.os, 'fsync', fsync)
    monkeypatch.setattr(books, '_validate', guarded)
    with pytest.raises((ValueError, RuntimeError)):
        books.publish_duration(plan.id, 1, output, maximum_ms=70, review_sha256=review['approval_sha256'])
    assert revoked and not output.exists()


def test_late_stop_after_move_remains_complete(completed, tmp_path, monkeypatch):
    stopped = Event()
    real = publishing.publish_noreplace
    def moved(source, destination, **kwargs):
        real(source, destination, **kwargs)
        stopped.set()
    monkeypatch.setattr(publishing, 'publish_noreplace', moved)
    output = tmp_path / 'completed.zip'
    _, receipt = split(completed, output, cancelled=stopped.is_set)
    assert stopped.is_set() and receipt['status'] == 'complete' and output.is_file()


def test_actual_archive_pcm_mutation_is_not_accepted_as_a_saved_completion(completed, tmp_path):
    output = tmp_path / 'original.zip'
    _, receipt = split(completed, output)
    changed = tmp_path / 'changed.zip'
    with ZipFile(output) as source, ZipFile(changed, 'x', compression=ZIP_STORED) as target:
        corrupted = False
        for member in source.infolist():
            raw = source.read(member.filename)
            if member.filename.startswith('audio/') and not corrupted:
                raw = raw[:-1] + bytes([raw[-1] ^ 1])
                corrupted = True
            target.writestr(member.filename, raw)
    with pytest.raises(ValueError, match='checksum'):
        measured_audio.inspect(changed)
    books, plan, *_ = completed
    with pytest.raises(ValueError, match='content receipt'):
        books.verify_duration(plan.id, 1, receipt['id'], output=changed)


def test_restored_measured_archive_verifies_without_engine_or_old_paths(completed, tmp_path):
    from carr_library.application import open_existing_library
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    books, plan, *_ = completed
    output = tmp_path / 'completed-parts.zip'
    _, receipt = split(completed, output)
    archive = tmp_path / 'library-backup'
    identity = create_backup(books.library, archive)
    verify_backup(archive, identity)
    root = restore_backup(archive, tmp_path / 'restored-library', identity)
    restored = BookAudioService(open_existing_library(root), books.workspace)
    selected = tmp_path / 'selected-moved-parts.zip'
    output.rename(selected)
    with pytest.raises(ValueError, match='restored elsewhere'):
        restored.verify_duration(plan.id, 1, receipt['id'])
    verified = restored.verify_duration(plan.id, 1, receipt['id'], output=selected)
    assert verified['status'] == 'complete' and verified['output'] == str(selected)
    fresh = tmp_path / 'new-parts-from-restored.zip'
    review = restored.review_duration(plan.id, 1, fresh, maximum_ms=90)
    result = restored.publish_duration(plan.id, 1, fresh, maximum_ms=90,
                                      review_sha256=review['approval_sha256'])
    assert result['status'] == 'complete' and measured_audio.inspect(fresh)['source']['book']['plan'] == plan.id


def test_actual_zip64_directory_below_32bit_sentinel_is_bounded_and_accepted(completed, tmp_path, monkeypatch):
    import zipfile
    # Exercise exactly the 2 GiB-versus-4 GiB sentinel distinction using the
    # real standard-library ZIP64 writer and tiny PCM, without allocating GiB.
    monkeypatch.setattr(zipfile, 'ZIP64_LIMIT', 128)
    output = tmp_path / 'real-zip64-records.zip'
    _, receipt = split(completed, output)
    raw = output.read_bytes()
    assert raw[-42:-38] == b'PK\x06\x07'
    end = struct.unpack('<4s4H2IH', raw[-22:])
    assert end[-2] != 0xffffffff
    assert measured_audio.inspect(output)['publication_id'] == receipt['id']
