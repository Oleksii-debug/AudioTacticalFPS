"""Raster import/review/recovery with actual images and canonical journals.

The deterministic recognizer isolates source authority from OCR accuracy.
Independent page fingerprints still run in the real isolated Pillow worker.
test_raster_ocr_native separately exercises the actual installed OCR engine.
"""
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from PIL import Image
import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import snapshot
from carr_library.extract import page_ocr, raster_pages
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import prepare_request, record_progress, load_request
from carr_library.library_backup import create_backup, restore_backup
from carr_library.reader.reading_desk import ReadingDesk
from carr_library.ui.source_review import PdfReviewSession


def make_source(path, format):
    first = Image.new('RGB', (64, 32), 'red')
    second = Image.new('RGB', (64, 32), 'blue')
    try:
        options = dict(save_all=True, append_images=[second], compression='raw') if format == 'TIFF' else {}
        first.save(path, format=format, **options)
    finally:
        first.close()
        second.close()
    return path.read_bytes()


@pytest.fixture
def synthetic_ocr(monkeypatch):
    calls = []

    def recognize(result, image, config, deadline, **kwargs):
        red, _, blue = image.getpixel((0, 0))
        index = 1 if red > blue else 2
        calls.append(index)
        raw = f'Raster railway frame {index}.\n'
        result['raw_text'] = raw
        result['status'] = 'review_required'
        result['warnings'].append('ocr_not_verified')
        result['provenance'].update(method='ocr_candidate', ocr_text=raw)
        return result

    monkeypatch.setattr(page_ocr, '_recognize_image', recognize)
    return calls


def prepare(app, workspace, source):
    metadata = dict(title='Exact raster source', authors=['Synthetic author'],
        language='en', edition='Original edition', volume=None, collections=[],
        work_id=uuid4().hex, edition_id=uuid4().hex)
    request = prepare_request(app.root, workspace, source, metadata, Config(),
                              expected_manifest=snapshot(source.parent, source.name).manifest())
    return request['request_id']


def run_saved(app, workspace, request_id, *, cancelled=lambda: False):
    request = load_request(app.root, request_id)
    path = (app.assets.resolve(request['asset_sha256']) if request.get('asset_sha256')
            else Path(request['source']))
    result = app._import_file(workspace, path, **request['metadata'],
        config=Config(**request['config']), _request_id=request_id,
        _resume_job=request.get('job'), _expected_manifest=request.get('expected_manifest'),
        cancelled=cancelled)
    record_progress(app.root, request_id, state=result['state'], result=result)
    return result


def confirm_next(app, workspace, request_id):
    pending = app.pending_review(workspace, request_id)
    assert pending is not None and 'Pillow/' in pending['renderer']
    app.confirm_import_review(workspace, request_id, 'Explicit synthetic independent reviewer',
        confirmed=True, expected_checksum=pending['checksum'])
    return pending


@pytest.mark.parametrize('format,suffix,total', [
    ('PNG', '.png', 1), ('JPEG', '.jpeg', 1), ('TIFF', '.tiff', 2)])
def test_actual_containers_keep_physical_order_and_require_each_review(
        tmp_path, synthetic_ocr, format, suffix, total):
    source = tmp_path / ('Джерело' + suffix)
    raw = make_source(source, format)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Image sources')
    request_id = prepare(app, workspace, source)
    result = run_saved(app, workspace, request_id)
    reviews = []
    for physical in range(1, total + 1):
        assert result['state'] == 'quarantined'
        assert result['completed'] == physical and result['total'] == total
        assert app.browse(workspace) == ()
        pending = app.pending_review(workspace, request_id)
        assert pending['physical'] == physical
        assert pending['raw_text'] == f'Raster railway frame {physical}.\n'
        with pytest.raises(ValueError, match='changed'):
            app.confirm_import_review(workspace, request_id, 'Wrong displayed snapshot',
                                      confirmed=True, expected_checksum='0' * 64)
        reviews.append(confirm_next(app, workspace, request_id))
        result = run_saved(app, workspace, request_id)
    assert result['state'] == 'complete' and result['verified'] is False
    assert synthetic_ocr == list(range(1, total + 1))
    assert source.read_bytes() == raw
    assert app.assets.resolve(sha256(raw).hexdigest()).read_bytes() == raw
    desk = ReadingDesk(app, workspace, result['edition'])
    assert len(desk.pages) == total
    for physical in range(1, total + 1):
        page = desk.page(physical)
        assert page.locator.kind == 'physical_page' and page.locator.index == physical
        assert page.locator.printed is None and page.locator.verified is False
        assert page.raw_text == f'Raster railway frame {physical}.\n'
        assert page.verified is True
        assert 'Physical page' in page.description and 'PDF page' not in page.description
    hits = app.search(workspace, 'railway')
    assert len(hits) == total and all(hit.verified for hit in hits)
    assert all(app.copy(workspace, hit.ref, hit.revision, 'quote') == 'railway' for hit in hits)
    with app.read(workspace) as db:
        assert [tuple(row) for row in db.execute(
            'SELECT physical,printed,verified FROM page ORDER BY ordinal')] == [
                (physical, None, 0) for physical in range(1, total + 1)]


def test_existing_source_review_revokes_raster_without_recreating_approval(tmp_path, synthetic_ocr):
    source = tmp_path / 'source.png'
    make_source(source, 'PNG')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Image review')
    request_id = prepare(app, workspace, source)
    assert run_saved(app, workspace, request_id)['state'] == 'quarantined'
    confirm_next(app, workspace, request_id)
    assert run_saved(app, workspace, request_id)['state'] == 'complete'
    hit, = app.search(workspace, 'railway')
    review = PdfReviewSession(app, workspace, hit.ref, hit.revision)
    opened = []
    review.open_original(lambda path: opened.append(Path(path).read_bytes()) or True)
    assert opened == [source.read_bytes()]
    review.revoke(confirmed=True)
    assert app.search(workspace, 'railway')[0].verified is False
    with pytest.raises(ValueError, match='unverified'):
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    with pytest.raises(ValueError, match='cannot be replaced'):
        PdfReviewSession(app, workspace, hit.ref, hit.revision).approve('Other reviewer', confirmed=True)


@pytest.mark.parametrize('complete', [False, True])
def test_backup_delete_original_restore_preserves_tiff_review_and_requires_explicit_resume(
        tmp_path, synthetic_ocr, monkeypatch, complete):
    source = tmp_path / 'original.tiff'
    raw = make_source(source, 'TIFF')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Image backup')
    request_id = prepare(app, workspace, source)
    first = run_saved(app, workspace, request_id)
    assert first['state'] == 'quarantined'
    confirm_next(app, workspace, request_id)
    if complete:
        second = run_saved(app, workspace, request_id)
        assert second['state'] == 'quarantined'
        confirm_next(app, workspace, request_id)
        assert run_saved(app, workspace, request_id)['state'] == 'complete'
    count_before = len(synthetic_ocr)
    backup = tmp_path / 'backup'
    # Backup/restore must only validate retained bytes and typed relationships;
    # image parsing or recognition cannot run as an incidental restore action.
    with monkeypatch.context() as passive:
        passive.setattr(Image, 'open', lambda *a, **k: pytest.fail('Passive recovery decoded an image.'))
        digest = create_backup(app, backup)
        source.unlink()
        restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    assert len(synthetic_ocr) == count_before
    rebound = load_request(restored.root, request_id)
    assert rebound['data_dir'] == str(restored.root)
    assert Path(rebound['source']).is_relative_to(restored.root)
    assert restored.assets.resolve(sha256(raw).hexdigest()).read_bytes() == raw
    if not complete:
        assert restored.browse(workspace) == ()
        resumed = run_saved(restored, workspace, request_id)
        assert resumed['id'] == first['id'] and resumed['state'] == 'quarantined'
        assert resumed['completed'] == 2
        confirm_next(restored, workspace, request_id)
        assert run_saved(restored, workspace, request_id)['state'] == 'complete'
    else:
        assert run_saved(restored, workspace, request_id)['state'] == 'complete'
        assert len(synthetic_ocr) == count_before
    hits = restored.search(workspace, 'railway')
    assert len(hits) == 2 and all(hit.verified for hit in hits)
    assert all(hit.revision == first['id'] for hit in hits)


def test_changed_retained_image_never_reuses_source_review(tmp_path, synthetic_ocr):
    source = tmp_path / 'source.png'
    raw = make_source(source, 'PNG')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Tamper refusal')
    request_id = prepare(app, workspace, source)
    assert run_saved(app, workspace, request_id)['state'] == 'quarantined'
    pending = app.pending_review(workspace, request_id)
    retained = app.assets.resolve(sha256(raw).hexdigest())
    changed = bytearray(raw)
    changed[-1] ^= 1
    retained.write_bytes(changed)
    with pytest.raises(ValueError, match='digest|checksum|changed'):
        app.confirm_import_review(workspace, request_id, 'Stale display', confirmed=True,
                                  expected_checksum=pending['checksum'])
    assert app.browse(workspace) == ()
    with pytest.raises(ValueError):
        run_saved(app, workspace, request_id)


def test_cancel_after_real_decode_is_durable_and_reuses_same_plan(tmp_path, synthetic_ocr, monkeypatch):
    source = tmp_path / 'cancel.png'
    raw = make_source(source, 'PNG')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Cancelled image')
    request_id = prepare(app, workspace, source)
    # Prepare one canonical job first, then discard its unreviewed checkpoint
    # via the ordinary explicit retry path. Stop during the actual second decode.
    first = run_saved(app, workspace, request_id)
    assert first['state'] == 'quarantined'
    stopped = threading.Event()
    original = raster_pages._rgb

    def decode_then_cancel(*args, **kwargs):
        image = original(*args, **kwargs)
        stopped.set()
        return image

    with monkeypatch.context() as patch:
        patch.setattr(raster_pages, '_rgb', decode_then_cancel)
        result = run_saved(app, workspace, request_id, cancelled=stopped.is_set)
    assert result['state'] == 'cancelled' and result['id'] == first['id']
    assert app.browse(workspace) == ()
    assert app.assets.resolve(sha256(raw).hexdigest()).read_bytes() == raw
    assert load_request(app.root, request_id)['state'] == 'cancelled'
    resumed = run_saved(app, workspace, request_id)
    assert resumed['state'] == 'quarantined' and resumed['id'] == first['id']
    confirm_next(app, workspace, request_id)
    assert run_saved(app, workspace, request_id)['state'] == 'complete'


def test_passive_backup_rejects_raster_checkpoint_inventory_tamper(tmp_path, synthetic_ocr):
    from carr_library.ingest.pipeline import canonical, digest
    source = tmp_path / 'source.tif'
    make_source(source, 'TIFF')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Bound inventory')
    request_id = prepare(app, workspace, source)
    result = run_saved(app, workspace, request_id)
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        payload = json.loads(db.execute('SELECT payload FROM checkpoint WHERE job=? AND ordinal=1',
                                       (result['id'],)).fetchone()[0])
        payload['provenance']['frame_count'] = 999
        changed = canonical(payload)
        db.execute('UPDATE checkpoint SET payload=?,digest=? WHERE job=? AND ordinal=1',
                   (changed, digest(changed), result['id']))
    with pytest.raises(ValueError, match='physical-frame identity'):
        create_backup(app, tmp_path / 'refused-backup')


@pytest.mark.parametrize('status,method', [('candidate', 'native'), ('blank', 'render_blank')])
def test_rehashed_checkpoint_cannot_turn_raster_candidate_into_native_text(
        tmp_path, synthetic_ocr, status, method):
    from carr_library.ingest.pipeline import canonical, digest
    source = tmp_path / 'source.png'
    make_source(source, 'PNG')
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('No raster native-text authority')
    request_id = prepare(app, workspace, source)
    result = run_saved(app, workspace, request_id)
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        value = json.loads(db.execute('SELECT payload FROM checkpoint WHERE job=? AND ordinal=1',
                                     (result['id'],)).fetchone()[0])
        value['status'] = status
        value['provenance']['method'] = method
        payload = canonical(value)
        db.execute('UPDATE checkpoint SET payload=?,digest=? WHERE job=? AND ordinal=1',
                   (payload, digest(payload), result['id']))
    with pytest.raises(ValueError, match='cannot authorize'):
        run_saved(app, workspace, request_id)
    assert app.browse(workspace) == ()
    with app.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM source_page_review').fetchone()[0] == 0
