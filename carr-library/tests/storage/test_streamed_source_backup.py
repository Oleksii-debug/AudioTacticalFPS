from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.extract.page_ocr import Config
from carr_library.ingest.streamed_text import TextProfile, inspect_text
from carr_library.ingest.worker import prepare_request, load_request, record_progress
from carr_library.library_backup import create_backup, restore_backup, verify_backup
from carr_library.pending_backup import validate_pending


def prepared(tmp_path, *, large=False):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Portable streamed source')
    source = tmp_path / 'Original.txt'
    with source.open('wb') as output:
        output.write(b'Portableneedle original evidence\r\n')
        if large:
            block = ('Synthetic capacity record. Український текст.\r\n' * 512).encode('utf-8')
            while output.tell() <= 64 * 1024**2:
                output.write(block)
        else:
            output.write(b'\fSecond field\fThird field')
    profile = TextProfile(65 * 1024**2)
    manifest = inspect_text(source, profile).manifest()
    metadata = dict(title='Portable exact source', authors=[], language='uk', edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    request = prepare_request(app.root, workspace, source, metadata, Config(),
                              expected_manifest=manifest, source_profile=profile)
    return app, workspace, source, request


def read_manifest(directory):
    return json.loads((directory / 'manifest.json').read_bytes())


def test_actual_large_prepared_request_restore_resume_rebackup_and_verified_quote(tmp_path):
    app, workspace, source, request = prepared(tmp_path, large=True)
    assert source.stat().st_size > 64 * 1024**2
    archive = tmp_path / 'first backup'
    digest = create_backup(app, archive)
    manifest = read_manifest(archive)
    pending = manifest['pending']
    assert pending['version'] == 3 and pending['scan_pages'] == []
    assert pending['source_profiles'] == [dict(sha256=request['expected_manifest']['sha256'],
                                               profile=request['source_profile'])]
    before = {path.relative_to(archive).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
              for path in archive.rglob('*') if path.is_file()}
    verify_backup(archive, digest)
    assert before == {path.relative_to(archive).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
                      for path in archive.rglob('*') if path.is_file()}
    source.unlink()
    restored = LibraryApp(restore_backup(archive, tmp_path / 'restored', digest))
    rebound = load_request(restored.root, request['request_id'])
    assert rebound['version'] == 2 and rebound['source_profile'] == request['source_profile']
    assert Path(rebound['source']).is_relative_to(restored.assets_dir)
    assert not any(key in rebound for key in ('attempt', 'publication_candidate', 'publication_watermark'))
    completed = restored.resume_import(workspace, request['request_id'], timeout=180)
    assert completed['state'] == 'complete', completed
    assert completed['asset'] == request['expected_manifest']['sha256']
    hit = restored.search_page(workspace, 'Portableneedle original evidence', limit=1).results[0]
    assert hit.verified and restored.copy(workspace, hit.ref, hit.revision, 'quote') == 'Portableneedle original evidence'
    second = tmp_path / 'completed backup'
    second_digest = create_backup(restored, second)
    second_manifest = read_manifest(second)
    assert second_manifest['assets'][0]['size_bytes'] > 64 * 1024**2
    assert second_manifest['pending']['source_profiles'] == pending['source_profiles']
    verify_backup(second, second_digest)


def test_partial_streamed_journal_restores_without_discarding_checked_fragments(tmp_path, monkeypatch):
    from carr_library.ingest.streamed_ingest import StreamedTextIngest
    app, workspace, source, request = prepared(tmp_path)
    real = StreamedTextIngest.run
    def one(self, *args, **kwargs):
        return real(self, *args, **dict(kwargs, max_steps=1))
    monkeypatch.setattr(StreamedTextIngest, 'run', one)
    initial = app._import_file(workspace, source, **request['metadata'], _request_id=request['request_id'],
        _source_profile=request['source_profile'], _expected_manifest=request['expected_manifest'])
    assert initial['completed'] == 1 and initial['state'] == 'pending'
    record_progress(app.root, request['request_id'], state='cancelled', result=initial)
    archive = tmp_path / 'backup'
    checksum = create_backup(app, archive)
    source.unlink()
    restored = LibraryApp(restore_backup(archive, tmp_path / 'restored', checksum))
    result = restored.resume_import(workspace, request['request_id'])
    assert result['state'] == 'complete' and result['id'] == initial['id']
    assert result['completed'] == 3


@pytest.mark.parametrize('change', ['larger-profile', 'missing-profile', 'wrong-asset', 'legacy-request'])
def test_rehashed_manifest_cannot_invent_streamed_source_admission(tmp_path, change):
    app, workspace, source, request = prepared(tmp_path)
    archive = tmp_path / 'backup'
    create_backup(app, archive)
    manifest = read_manifest(archive)
    pending = manifest['pending']
    if change == 'larger-profile':
        pending['source_profiles'][0]['profile']['maximum_bytes'] = 128 * 1024**2
    elif change == 'missing-profile':
        pending['source_profiles'] = []
    elif change == 'wrong-asset':
        pending['source_profiles'][0]['sha256'] = 'a' * 64
    else:
        relative = f"import-requests/{request['request_id']}.json"
        path = archive / 'pending' / relative
        record = json.loads(path.read_bytes())
        record['version'] = 1
        raw = (json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        path.write_bytes(raw)
        entry = next(item for item in pending['files'] if item['filename'] == relative)
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    raw = (json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    (archive / 'manifest.json').write_bytes(raw)
    with pytest.raises(ValueError):
        verify_backup(archive, sha256(raw).hexdigest())
    assert not (tmp_path / 'activated').exists()


def test_self_rehashed_streamed_checkpoint_is_compared_to_original_at_backup(tmp_path, monkeypatch):
    from carr_library.ingest.streamed_ingest import StreamedTextIngest
    from carr_library.ingest.pipeline import canonical, digest
    app, workspace, source, request = prepared(tmp_path)
    run = StreamedTextIngest.run
    monkeypatch.setattr(StreamedTextIngest, 'run', lambda self, *a, **k: run(self, *a, **dict(k, max_steps=1)))
    initial = app._import_file(workspace, source, **request['metadata'], _request_id=request['request_id'],
                              _source_profile=request['source_profile'])
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        value = json.loads(db.execute('SELECT payload FROM checkpoint').fetchone()[0])
        value['raw_text'] = 'Invented quotation'
        raw = canonical(value)
        db.execute('UPDATE checkpoint SET payload=?,digest=?', (raw, digest(raw)))
    with pytest.raises(ValueError, match='exact original'):
        create_backup(app, tmp_path / 'refused')
    assert not (tmp_path / 'refused').exists()


def test_cancellation_during_streamed_pending_copy_does_not_publish_archive(tmp_path, monkeypatch):
    from carr_library import pending_backup
    app, workspace, source, request = prepared(tmp_path)
    real = pending_backup._copy
    def stopped(incoming, target, entry, cancelled):
        return real(incoming, target, entry, lambda: True)
    monkeypatch.setattr(pending_backup, '_copy', stopped)
    with pytest.raises(InterruptedError):
        create_backup(app, tmp_path / 'cancelled')
    assert not (tmp_path / 'cancelled').exists()
    assert source.exists() and load_request(app.root, request['request_id'])['state'] == 'pending'


def test_mixed_streamed_request_and_folder_cursor_restore_keep_external_scan_inactive(tmp_path, monkeypatch):
    app, workspace, source, request = prepared(tmp_path)
    folder = tmp_path / 'selected folder'
    folder.mkdir()
    for number in range(2):
        (folder / f'{number}.txt').write_text(f'Scan source {number}', encoding='utf-8')
    from carr_library.ingest import batch_scan
    scan = batch_scan.scan_paths_page
    monkeypatch.setattr(batch_scan, 'scan_paths_page',
        lambda *a, **k: scan(*a, **dict(k, max_files=1)))
    batch = app.plan_import_batch(workspace, [folder], paged=True)
    assert batch['scan']['cursor'] is not None
    archive = tmp_path / 'mixed backup'
    digest = create_backup(app, archive)
    pending = read_manifest(archive)['pending']
    assert pending['version'] == 3 and len(pending['scan_pages']) == 1
    restored = LibraryApp(restore_backup(archive, tmp_path / 'restored mixed', digest))
    saved = restored.import_batch(workspace, batch['batch_id'])
    assert saved['scan']['review_required'] is True
    with pytest.raises(ValueError, match='Unscanned external sources'):
        restored.continue_import_scan(workspace, batch['batch_id'])
    assert restored.resume_import(workspace, request['request_id'])['state'] == 'complete'


@pytest.mark.parametrize('component_version', [1, 2])
def test_released_pending_v1_and_typed_v2_scans_restore_without_external_authority(
        tmp_path, monkeypatch, component_version):
    from carr_library.ingest import batch_scan
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Saved bounded scan')
    folder = tmp_path / 'external'
    folder.mkdir()
    for number in range(2):
        (folder / f'{number}.txt').write_text(f'Original scanned source {number}.', encoding='utf-8')
    scan = batch_scan.scan_paths_page
    monkeypatch.setattr(batch_scan, 'scan_paths_page',
        lambda *a, **k: scan(*a, **dict(k, max_files=1)))
    batch = app.plan_import_batch(workspace, [folder], paged=True)
    assert batch['scan']['cursor'] is not None
    archive = tmp_path / 'archive'
    checksum = create_backup(app, archive)
    manifest = read_manifest(archive)
    assert manifest['pending']['version'] == 2
    summary, = manifest['pending']['scan_pages']
    assert summary == dict(batch_id=batch['batch_id'], previous_batch_id=None,
        has_continuation=True, next_batch_id=batch['scan']['next_batch_id'],
        unscanned_sources='external_not_copied', restore_traversal='inactive')
    if component_version == 1:
        # Released r6 used pending-v1 even for the existing batch-v2 cursor.
        manifest['pending']['version'] = 1
        del manifest['pending']['scan_pages']
        raw = (json.dumps(manifest, sort_keys=True, ensure_ascii=False,
                          separators=(',', ':')) + '\n').encode('utf-8')
        (archive / 'manifest.json').write_bytes(raw)
        checksum = sha256(raw).hexdigest()
    verify_backup(archive, checksum)
    restored = LibraryApp(restore_backup(archive, tmp_path / 'restored', checksum))
    saved = restored.import_batch(workspace, batch['batch_id'])
    assert saved['scan']['cursor'] == batch['scan']['cursor']
    assert saved['scan']['review_required'] is True
    with pytest.raises(ValueError, match='Unscanned external sources'):
        restored.continue_import_scan(workspace, batch['batch_id'])
    second = tmp_path / 'second archive'
    verify_backup(second, create_backup(restored, second))


def test_typed_text_backup_preserves_independent_large_pdf_request_budget(tmp_path):
    import importlib.util
    from carr_library.catalog.streaming_pdf import FileAsset
    app, workspace, text, text_request = prepared(tmp_path)
    fixture_path = Path(__file__).parents[1] / 'ingest' / 'streaming_pdf_fixture.py'
    spec = importlib.util.spec_from_file_location('_carr_typed_backup_pdf_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    source = fixture.write_pdf(tmp_path / 'large.pdf')
    original = FileAsset.capture(source)
    assert original.size > 64 * 1024**2
    metadata = dict(title='Independent bounded PDF', authors=[], language=None,
        edition=None, volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    pdf_request = prepare_request(app.root, workspace, source, metadata, Config(),
        expected_manifest=original.manifest(), pdf_max_bytes=96 * 1024**2)
    archive = tmp_path / 'mixed archive'
    checksum = create_backup(app, archive)
    manifest = read_manifest(archive)
    assert manifest['pending']['version'] == 3
    assert manifest['pending']['source_profiles'] == [
        dict(sha256=text_request['expected_manifest']['sha256'], profile=text_request['source_profile'])]
    verify_backup(archive, checksum)
    source.unlink()
    text.unlink()
    restored = LibraryApp(restore_backup(archive, tmp_path / 'restored', checksum))
    pdf = load_request(restored.root, pdf_request['request_id'])
    streamed = load_request(restored.root, text_request['request_id'])
    assert pdf['version'] == 1 and pdf['pdf_max_bytes'] == 96 * 1024**2
    assert 'source_profile' not in pdf and 'pdf_max_bytes' not in streamed
    assert FileAsset.capture(Path(pdf['source'])).manifest() == original.manifest()
    assert inspect_text(Path(streamed['source']), TextProfile(65 * 1024**2)).manifest() == (
        text_request['expected_manifest'])
