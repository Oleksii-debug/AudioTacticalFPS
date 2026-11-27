"""Exact canonical seven-field imports survive the verified-pack extension."""
from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import snapshot
from carr_library.extract.page_ocr import Config
from carr_library.ingest import worker
from carr_library.ingest.publication_identity import request_identity, validate_configuration
from carr_library.library_backup import create_backup, restore_backup, verify_backup


def prepared(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Canonical pending input')
    source = tmp_path / 'Україна.txt'
    source.write_text('Unchanged first source.\fExact second source.', encoding='utf-8')
    metadata = dict(title='Canonical source', authors=[], language='uk', edition=None,
                    volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    request = worker.prepare_request(app.root, workspace, source, metadata, Config(),
                                    expected_manifest=snapshot(source.parent, source.name).manifest())
    for field in ('model_pack_path', 'model_pack_sha256'):
        del request['config'][field]
    worker._store(app.root, request['request_id'], request)
    return app, workspace, source, request


@pytest.mark.parametrize('fields', [7, 9], ids=['canonical-seven', 'verified-pack-nine'])
def test_config_reader_preserves_exact_released_fields(fields):
    value = asdict(Config())
    if fields == 7:
        for name in ('model_pack_path', 'model_pack_sha256'):
            del value[name]
    before = dict(value)
    effective = validate_configuration(value)
    assert value == before and len(value) == fields
    assert effective.model_pack_path is None and effective.model_pack_sha256 is None
    assert asdict(effective) == asdict(Config())


@pytest.mark.parametrize('change', ['missing', 'unknown', 'half-pack', 'boolean-budget'],
                         ids=['partial-core', 'foreign-key', 'partial-pack', 'wrong-budget-type'])
def test_compatibility_does_not_admit_partial_or_ambiguous_configuration(change):
    value = asdict(Config())
    if change == 'missing':
        del value['dpi']
    elif change == 'unknown':
        value['execute'] = 'not an extraction option'
    elif change == 'half-pack':
        del value['model_pack_sha256']
    else:
        value['timeout'] = True
    with pytest.raises(ValueError):
        validate_configuration(value)


def test_real_child_preserves_legacy_config_while_owning_its_receipt(tmp_path):
    app, workspace, source, planned = prepared(tmp_path)
    original = source.read_bytes()
    frozen = request_identity(planned)
    assert json.loads(frozen)['config'] == planned['config']
    result = app.resume_import(workspace, planned['request_id'])
    assert result['state'] == 'complete', result
    saved = worker.load_request(app.root, planned['request_id'])
    assert saved['config'] == planned['config'] and len(saved['config']) == 7
    with app.read(workspace) as db:
        receipt = db.execute('SELECT request_id,attempt,sha256 FROM import_publication '
                             'WHERE revision=?', (result['id'],)).fetchone()
        assert tuple(receipt) == (planned['request_id'], saved['attempt'],
                                  saved['publication_candidate']['sha256'])
    assert source.read_bytes() == original


def test_legacy_pending_and_published_receipts_survive_restore_without_old_attempt_authority(tmp_path):
    app, workspace, source, planned = prepared(tmp_path)
    original = source.read_bytes()
    folder = tmp_path / 'queued-backup'
    digest = create_backup(app, folder)
    assert verify_backup(folder, digest)['schema_version'] == 5
    source.unlink()
    restored = LibraryApp(restore_backup(folder, tmp_path / 'restored', digest))
    rebound = worker.load_request(restored.root, planned['request_id'])
    assert rebound['config'] == planned['config']
    assert Path(rebound['source']).read_bytes() == original
    completed = restored.resume_import(workspace, planned['request_id'])
    assert completed['state'] == 'complete', completed
    with restored.read(workspace) as db:
        receipt_before = tuple(db.execute(
            'SELECT revision,request_id,attempt,sha256 FROM import_publication').fetchone())
    second = tmp_path / 'complete-backup'
    second_digest = create_backup(restored, second)
    final = LibraryApp(restore_backup(second, tmp_path / 'again', second_digest))
    final_request = worker.load_request(final.root, planned['request_id'])
    assert final_request['config'] == planned['config']
    assert 'attempt' not in final_request and 'publication_candidate' not in final_request
    with final.read(workspace) as db:
        assert tuple(db.execute('SELECT revision,request_id,attempt,sha256 '
                                 'FROM import_publication').fetchone()) == receipt_before
    assert final.search(workspace, 'second')[0].quote == 'second'
