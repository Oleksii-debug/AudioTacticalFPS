"""Verified publication receipts are evidence, not reusable restore authority."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library import backup_sidecars
from carr_library.ingest.worker import load_request, validate_publication_document
from carr_library.library_backup import restore_backup
from carr_library.storage.revisions import Storage


@pytest.fixture
def completed(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Committed source')
    from reportlab.pdfgen.canvas import Canvas
    source = tmp_path / 'original.pdf'
    canvas = Canvas(str(source))
    canvas.drawString(72, 700, 'Original immutable publication evidence.')
    canvas.showPage()
    canvas.save()
    result = app.import_file(workspace, source)
    assert result['state'] == 'complete'
    request = load_request(app.root, result['request_id'])
    assert validate_publication_document(request)['committed'] is True
    return app, workspace, source, request


def test_full_backup_validates_then_removes_old_execution_receipt(completed, tmp_path):
    app, workspace, source, request = completed
    backup = tmp_path / 'backup'
    checksum = app.backup(backup)
    source.unlink()
    root = restore_backup(backup, tmp_path / 'restored', checksum)
    restored = LibraryApp(root)
    after = load_request(root, request['request_id'])
    assert 'attempt_id' not in after and 'publication' not in after
    assert after['data_dir'] == str(root)
    assert Path(after['source']).is_relative_to(root / 'originals')
    assert after['job'] == request['job'] and after['result'] == request['result']
    assert load_request(app.root, request['request_id']) == request
    result = restored.resume_import(workspace, request['request_id'])
    assert result['state'] == 'complete' and result['id'] == request['job']
    assert restored.search(workspace, 'immutable')[0].quote == 'immutable'


def test_legacy_sidecar_reader_also_clears_validated_execution_receipt(completed, tmp_path):
    app, _, _, request = completed
    stage = tmp_path / 'legacy-stage'
    stage.mkdir()
    target = tmp_path / 'restore-stage'
    target.mkdir()
    with closing(Storage(app.database)) as storage:
        manifest = backup_sidecars.snapshot(app, stage, {request['asset_sha256']}, db=storage.db)
        restored = backup_sidecars.restore(stage / 'sidecars', target, manifest,
            tmp_path / 'new-root', db=storage.db)
    path = target / 'sidecars/import-requests' / (request['request_id'] + '.json')
    value = json.loads(path.read_text(encoding='utf-8'))
    assert 'publication' not in value and 'attempt_id' not in value
    assert value['data_dir'] == str(tmp_path / 'new-root')
    assert restored['source_root'] == value['data_dir']


@pytest.mark.parametrize('field,value', [('committed', 1), ('request_sha256', '0' * 64),
                                       ('attempt_id', '0' * 32)])
def test_bad_execution_receipt_never_becomes_valid_through_rebinding(completed, tmp_path, field, value):
    app, _, _, request = completed
    changed = deepcopy(request)
    changed['publication'][field] = value
    path = app.root / 'import-requests' / (request['request_id'] + '.json')
    path.write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(ValueError):
        app.backup(tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()



@pytest.mark.parametrize('boundary', [None, 0])
def test_legacy_watermark_is_validated_then_removed_without_replaying_source(completed, tmp_path,
                                                                           boundary):
    from carr_library.ingest import worker

    app, workspace, source, request = completed
    historical = deepcopy(request)
    historical['publication_watermark'] = boundary
    historical['state'] = 'running'
    historical.pop('result')
    worker._store(app.root, historical['request_id'], historical)
    target = tmp_path / 'legacy-watermark-backup'
    checksum = app.backup(target)
    source.unlink()

    restored_root = restore_backup(target, tmp_path / 'legacy-watermark-restored', checksum)
    after = load_request(restored_root, historical['request_id'])
    assert not {'publication_watermark', 'attempt_id', 'publication',
                'attempt', 'publication_candidate'} & after.keys()
    reopened = LibraryApp(restored_root)
    resumed = reopened.resume_import(workspace, historical['request_id'])
    assert resumed['state'] == 'complete' and resumed['id'] == request['job']
    with reopened.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM revision').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1
    assert load_request(app.root, historical['request_id']) == historical


@pytest.mark.parametrize('boundary', [True, -1, 2 ** 63, '0'])
def test_invalid_legacy_watermark_prevents_backup(completed, tmp_path, boundary):
    from carr_library.ingest import worker

    app, _, _, request = completed
    changed = deepcopy(request)
    changed['publication_watermark'] = boundary
    worker._store(app.root, changed['request_id'], changed)
    target = tmp_path / 'rejected-watermark'
    with pytest.raises(ValueError, match='publication boundary'):
        app.backup(target)
    assert not target.exists()
