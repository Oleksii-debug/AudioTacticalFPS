"""Actual managed projects and FFmpeg; ToneEngine is synthetic test audio."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil

import pytest

from carr_library.audio.book_audio import BookAudioService
from carr_library.audio.book_batch import BookBatchService, select_books
from carr_library.audio.book_store import BookStore, part_plan
from carr_library.audio.book_export import BookExportPublished
from carr_library.audio import jobs
from carr_library.audio.models import digest
from carr_library.audio.ffmpeg import wav_info
from carr_library.ui.audio_studio import StudioController
from tests.audio.test_book_plans import library, make_plan
from tests.audio.test_production import ToneEngine


def services(library):
    app, workspace, *_ = library
    ffmpeg = Path(os.environ.get('CARR_FFMPEG') or shutil.which('ffmpeg') or '/missing-ffmpeg')
    assert ffmpeg.is_file(), 'This strict suite requires the selected real FFmpeg runtime.'
    engine = ToneEngine()
    books = BookAudioService(app, workspace, engine=engine, ffmpeg=ffmpeg)
    return books, BookBatchService(books), engine


def prepared(library, tmp_path):
    books, batches, engine = services(library)
    plans = (make_plan(library), make_plan(library, item=library[3]))
    batch = select_books(plans)
    output = tmp_path / 'export'
    output.mkdir()
    outputs = (output, output)
    reviewed = batches.review(batch, outputs, plans=plans)
    assert not reviewed.prepared and engine.calls == []
    ready = batches.prepare(batch, outputs, plans=plans, approval=reviewed.fingerprint)
    assert ready.prepared and engine.calls == []
    return books, batches, engine, plans, batch, outputs, ready


def test_actual_render_resume_independent_export_and_recorded_status(library, tmp_path):
    books, batches, engine, plans, batch, outputs, ready = prepared(library, tmp_path)
    first = batches.run(batch.id, outputs, approval=ready.fingerprint)
    assert first['status'] == 'complete' and len(engine.calls) == 2
    original_calls = tuple(engine.calls)
    for row, plan in zip(first['books'], plans):
        assert row['plan'] == plan.id
        result = row['items'][0]
        target = Path(result['destination'])
        assert wav_info(target / 'master.wav').frames > 0
        receipt = jobs.read_json(target / 'complete.json', limit=16 * 1024**2)
        assert receipt['plan_sha256'] == digest(plan.project(1).to_dict())
        assert (target / 'master.wav').stat().st_nlink == 1
        assert jobs.read_json(target / 'book-source.json', limit=4 * 1024**2)['quotation_authority'] is False
    fresh = batches.review(batch, outputs)
    again = batches.run(batch.id, outputs, approval=fresh.fingerprint)
    assert again['status'] == 'complete' and tuple(engine.calls) == original_calls
    assert all(item['reused'] for row in again['books'] for item in row['items'])
    assert batches.status(batch.id)['verified_outputs'] is False


def test_changed_foreign_export_is_never_overwritten(library, tmp_path):
    books, batches, engine, plans, batch, outputs, ready = prepared(library, tmp_path)
    result = batches.run(batch.id, outputs, approval=ready.fingerprint)
    target = Path(result['books'][0]['items'][0]['destination']) / 'master.wav'
    target.write_bytes(b'user changed this file')
    with pytest.raises(ValueError, match='existing part'):
        batches.run(batch.id, outputs, approval=batches.review(batch, outputs).fingerprint)
    assert target.read_bytes() == b'user changed this file'


def test_retained_plan_and_managed_doc_have_no_output_permission(library, tmp_path):
    books, batches, engine, plans, batch, outputs, ready = prepared(library, tmp_path)
    for plan in plans:
        record = jobs.read_json(books.store.path(plan.id, 'plans'), limit=32 * 1024**2)
        assert str(outputs[0]) not in json.dumps(record)
        root, document, audio = books.managed.paths(plan.job_id(1))
        value = jobs.read_json(document, limit=64 * 1024**2)
        assert value['output_dir'] is None
        opened = StudioController(private_root=library[0].root)
        opened.open(document)
        assert opened.plan().to_dict() == plan.project(1).to_dict()
        value['draft']['source_text'] += 'changed'
        with pytest.raises(ValueError):
            part_plan(library[0].root, value)


def test_source_revoked_after_engine_work_blocks_final_publication(library, tmp_path, monkeypatch):
    books, batches, engine, plans, batch, outputs, ready = prepared(library, tmp_path)
    import carr_library.audio.production as production
    real = production.publish_noreplace
    revoked = False
    original = books._validate
    def validate(*args):
        if revoked:
            raise ValueError('Source revoked after source review')
        return original(*args)
    def publish(source, destination, **kwargs):
        nonlocal revoked
        if Path(destination).parent.name == 'renders':
            revoked = True
        return real(source, destination, **kwargs)
    monkeypatch.setattr(books, '_validate', validate)
    monkeypatch.setattr(production, 'publish_noreplace', publish)
    with pytest.raises(ValueError, match='Source revoked'):
        batches.run(batch.id, outputs, approval=ready.fingerprint)
    assert list(outputs[0].iterdir()) == []


def test_completed_resume_revalidates_source_before_engine_cache_return(library, tmp_path, monkeypatch):
    books, batches, engine, plans, batch, outputs, ready = prepared(library, tmp_path)
    batches.run(batch.id, outputs, approval=ready.fingerprint)
    calls = tuple(engine.calls)
    monkeypatch.setattr(books, '_validate', lambda *a: (_ for _ in ()).throw(ValueError('revoked')))
    with pytest.raises(ValueError, match='revoked'):
        batches.run(batch.id, outputs, approval=ready.fingerprint)
    assert tuple(engine.calls) == calls


def test_post_move_sync_failure_preserves_visible_export_and_retry_verifies(library, tmp_path, monkeypatch):
    books, batches, engine, plans, batch, outputs, ready = prepared(library, tmp_path)
    import carr_library.audio.book_export as export
    real = export.publish_noreplace
    failed = False
    def publish(source, destination, **kwargs):
        nonlocal failed
        real(source, destination, **kwargs)
        if not failed:
            failed = True
            raise OSError('injected post-move sync failure')
    monkeypatch.setattr(export, 'publish_noreplace', publish)
    with pytest.raises(BookExportPublished) as error:
        batches.run(batch.id, outputs, approval=ready.fingerprint)
    assert (error.value.destination / 'complete.json').is_file()
    monkeypatch.setattr(export, 'publish_noreplace', real)
    result = batches.run(batch.id, outputs, approval=batches.review(batch, outputs).fingerprint)
    assert result['status'] == 'complete' and result['books'][0]['items'][0]['reused']


def test_stop_after_completed_export_does_not_start_next_book(library, tmp_path, monkeypatch):
    books, batches, engine, plans, batch, outputs, ready = prepared(library, tmp_path)
    import carr_library.audio.book_audio as module
    real = module.export_part
    stopped = False
    def exported(*args, **kwargs):
        nonlocal stopped
        result = real(*args, **kwargs)
        stopped = True
        return result
    monkeypatch.setattr(module, 'export_part', exported)
    result = batches.run(batch.id, outputs, approval=ready.fingerprint, cancelled=lambda: stopped)
    assert result['status'] == 'paused' and len(result['books']) == 1
    assert result['books'][0]['items'][0]['status'] == 'complete'
    assert len(engine.calls) == 1


def test_restored_completed_books_export_without_any_live_engine_or_ffmpeg(library, tmp_path, monkeypatch):
    """Real renders and complete restore; the second export accepts retained bytes only."""
    from carr_library.application import open_existing_library
    from carr_library.library_backup import create_backup, restore_backup, verify_backup
    import carr_library.audio.book_audio as module
    books, batches, engine, plans, batch, outputs, ready = prepared(library, tmp_path)
    first = batches.run(batch.id, outputs, approval=ready.fingerprint)
    assert first['status'] == 'complete' and len(engine.calls) == 2
    expected = [jobs.read_json(Path(row['items'][0]['destination']) / 'complete.json',
                               limit=16 * 1024**2) for row in first['books']]
    archive = tmp_path / 'complete-library-backup'
    identity = create_backup(library[0], archive)
    verify_backup(archive, identity)
    restored_root = restore_backup(archive, tmp_path / 'other-library-root', identity)
    restored = open_existing_library(restored_root)
    def forbidden(*args, **kwargs):
        raise AssertionError('Offline completed-book reuse must not discover or invoke a runtime')
    monkeypatch.setattr(module, 'resolve_ffmpeg', forbidden)
    monkeypatch.setattr(BookAudioService, '_engine', forbidden)
    current = BookBatchService(BookAudioService(restored, library[1], renderer=forbidden))
    destination = tmp_path / 'new-explicit-book-output'
    destination.mkdir()
    selected = current.load(batch.id)
    review = current.review(selected, (destination, destination))
    notices = []
    result = current.run(batch.id, (destination, destination), approval=review.fingerprint,
                         progress=notices.append)
    assert result['status'] == 'complete'
    assert any('Retained completion reused' in notice for notice in notices)
    for row, receipt in zip(result['books'], expected):
        item = row['items'][0]
        assert item['retained_completion'] is True and item['reused'] is False
        path = Path(item['destination'])
        assert path.parent == destination
        assert wav_info(path / 'master.wav').frames > 0
        assert jobs.read_json(path / 'complete.json', limit=16 * 1024**2) == receipt
        assert (path / 'master.wav').stat().st_nlink == 1
    # Offline acceptance neither activates historical runtime paths nor mutates
    # the complete marker. A second full backup still validates that lineage.
    second = create_backup(restored, tmp_path / 'completed-rebackup')
    verify_backup(tmp_path / 'completed-rebackup', second)


@pytest.mark.parametrize('changed', ['plan', 'audio'])
def test_invalid_saved_completion_is_not_hidden_by_fresh_synthesis(library, tmp_path, monkeypatch, changed):
    import carr_library.audio.book_audio as module
    books, batches, engine, plans, batch, outputs, ready = prepared(library, tmp_path)
    first = batches.run(batch.id, outputs, approval=ready.fingerprint)
    result = first['books'][0]['items'][0]
    _, _, audio = books.managed.paths(plans[0].job_id(1))
    if changed == 'plan':
        path = audio / ('plan-' + result['run_key'] + '.json')
        value = jobs.read_json(path, limit=32 * 1024**2)
        value['title'] += ' changed'
        jobs.atomic_json(path, value)
    else:
        path = audio / 'renders' / result['run_key'] / 'master.wav'
        with path.open('r+b') as stream:
            stream.seek(-1, 2)
            previous = stream.read(1)
            stream.seek(-1, 2)
            stream.write(bytes([previous[0] ^ 1]))
    before = path.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError('A damaged completed part must not silently start a new renderer')
    monkeypatch.setattr(module, 'resolve_ffmpeg', forbidden)
    books.renderer = forbidden
    destination = tmp_path / 'refused-output'
    destination.mkdir()
    review = batches.review(batch, (destination, destination))
    with pytest.raises(ValueError, match='typed plan|no longer verifies'):
        batches.run(batch.id, (destination, destination), approval=review.fingerprint)
    assert path.read_bytes() == before and not list(destination.iterdir())


def test_legacy_completed_marker_requires_live_renderer_before_plan_upgrade(library, tmp_path, monkeypatch):
    import carr_library.audio.book_audio as module
    books, batches, engine, plans, batch, outputs, ready = prepared(library, tmp_path)
    first = batches.run(batch.id, outputs, approval=ready.fingerprint)
    run_key = first['books'][0]['items'][0]['run_key']
    _, _, audio = books.managed.paths(plans[0].job_id(1))
    marker = audio / 'renders' / run_key / 'complete.json'
    receipt = jobs.read_json(marker, limit=16 * 1024**2)
    receipt.pop('plan_sha256')
    jobs.atomic_json(marker, receipt)
    before = marker.read_bytes()
    class RuntimeRequired(Exception):
        pass
    def required(*args, **kwargs):
        raise RuntimeRequired('Exact live renderer validation remains required')
    monkeypatch.setattr(module, 'resolve_ffmpeg', required)
    with pytest.raises(RuntimeRequired):
        batches.run(batch.id, outputs, approval=batches.review(batch, outputs).fingerprint)
    assert marker.read_bytes() == before
