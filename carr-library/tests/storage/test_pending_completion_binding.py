"""Preserve canonical completed-import source binding on the current backup owner."""
from hashlib import sha256
from pathlib import Path

import pytest

from carr_library.application import LibraryApp
from carr_library.ingest.worker import load_request


@pytest.fixture
def app(tmp_path):
    return LibraryApp(tmp_path / 'source-library')


@pytest.mark.parametrize('change', ['source', 'result', 'missing-source'])
def test_completed_docx_request_cannot_rebind_its_published_source(app, tmp_path, change):
    """A complete receipt must refer to the same original as its saved request."""
    from docx import Document
    from carr_library.ingest import worker
    from carr_library.library_backup import create_backup, restore_backup

    workspace = app.create_workspace('Complete request source binding')
    original = tmp_path / 'published.docx'
    document = Document()
    document.add_paragraph('Original authorized publication.')
    document.save(original)
    completed = app.import_file(workspace, original)
    assert completed['state'] == 'complete'
    request = load_request(app.root, completed['request_id'])
    assert 'job' not in request and 'expected_manifest' not in request
    original_digest = request['asset_sha256']
    retained = app.assets.resolve(original_digest)
    original_bytes = retained.read_bytes()
    # Old completed DOCX requests legitimately have neither a planned manifest
    # nor a journal job. Preserve their exact source and cached receipt.
    valid_backup = tmp_path / 'valid-backup'
    checksum = create_backup(app, valid_backup)
    restored = LibraryApp(restore_backup(valid_backup, tmp_path / 'valid-restore', checksum))
    restored_request = load_request(restored.root, request['request_id'])
    assert Path(restored_request['source']).read_bytes() == original_bytes
    assert restored.resume_import(workspace, request['request_id']) == completed

    replacement = tmp_path / 'different.docx'
    document = Document()
    document.add_paragraph('Different source must not back the completed publication.')
    document.save(replacement)
    changed_bytes = replacement.read_bytes()
    other_digest = sha256(changed_bytes).hexdigest()
    assert other_digest != original_digest
    app.assets.retain(changed_bytes,
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document', other_digest)
    if change == 'source':
        request['asset_sha256'] = other_digest
    elif change == 'result':
        request['result']['asset'] = other_digest
    else:
        del request['asset_sha256']
    worker._store(app.root, request['request_id'], request)

    destination = tmp_path / 'invalid-backup'
    with pytest.raises(ValueError, match='source|publication'):
        create_backup(app, destination)
    assert not destination.exists()
    assert retained.read_bytes() == original_bytes
    with app.read(workspace) as db:
        assert db.execute('SELECT asset FROM revision WHERE id=?',
                          (completed['id'],)).fetchone()[0] == original_digest
