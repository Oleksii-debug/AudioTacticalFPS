"""Enclosing manifest and publication integration for resumable import backups."""
from uuid import uuid4

from carr_library.application import LibraryApp
from carr_library.catalog.intake import snapshot
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import prepare_request, record_progress, load_request
from carr_library.library_backup import create_backup, restore_backup


def test_complete_backup_survives_lost_original_and_resumes_identical_import(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Resumable source')
    original = tmp_path / 'original.txt'
    original.write_text('First original page.\fSecond original page.\fThird original page.', encoding='utf-8')
    metadata = dict(title='Unfinished book', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = snapshot(original.parent, original.name).manifest()
    request = prepare_request(app.root, workspace, original, metadata, Config(), expected_manifest=planned)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 2
    initial = app._import_file(workspace, original, **metadata, cancelled=cancelled,
                              _request_id=request['request_id'], _expected_manifest=planned)
    assert initial['completed'] == 1
    record_progress(app.root, request['request_id'], state='cancelled', result=initial)
    digest = create_backup(app, tmp_path / 'backup')
    original.unlink()
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest))
    rebound = load_request(restored.root, request['request_id'])
    assert rebound['data_dir'] == str(restored.root)
    completed = restored.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete'
    assert completed['id'] == initial['id'] and completed['edition'] == initial['edition']
    assert restored.search(workspace, 'Second')[0].quote == 'Second'


def test_complete_backup_preserves_prepared_request_before_first_worker_launch(tmp_path):
    """A queued request may have no journal/checkpoint or retained original yet."""
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Queued source')
    original = tmp_path / 'queued.txt'
    original.write_text('Queued original survives relocation.', encoding='utf-8')
    metadata = dict(title='Queued book', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = snapshot(original.parent, original.name).manifest()
    request = prepare_request(app.root, workspace, original, metadata, Config(), expected_manifest=planned)
    digest = create_backup(app, tmp_path / 'backup')
    original.unlink()
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest))
    rebound = load_request(restored.root, request['request_id'])
    assert rebound['data_dir'] == str(restored.root)
    assert rebound['expected_manifest'] == planned
    from pathlib import Path
    retained = Path(rebound['source'])
    assert retained.is_relative_to(restored.root)
    assert retained.read_bytes() == b'Queued original survives relocation.'
    completed = restored.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete'
    assert completed['edition'] == metadata['edition_id']
    assert restored.search(workspace, 'relocation')[0].quote == 'relocation'

