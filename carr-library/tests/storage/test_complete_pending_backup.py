"""Enclosing manifest and publication integration for resumable import backups."""
from uuid import uuid4

import pytest

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


def _prepared_backup(tmp_path, *, suffix='.txt', text='Prepared recovery preserves original evidence.'):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Prepared recovery')
    original = tmp_path / ('queued' + suffix)
    original.write_text(text, encoding='utf-8')
    metadata = dict(title='Prepared book', authors=[], language=None, edition=None,
                    volume=None, collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    request = prepare_request(app.root, workspace, original, metadata, Config(),
        expected_manifest=snapshot(original.parent, original.name).manifest())
    return app, workspace, original, request


def test_canonical_state_and_pending_v4_remains_readable(tmp_path):
    import json
    from hashlib import sha256
    app, workspace, original, request = _prepared_backup(tmp_path)
    backup = tmp_path / 'canonical-v4'
    create_backup(app, backup)
    manifest = json.loads((backup / 'manifest.json').read_bytes())
    assert manifest.pop('sidecars') == []
    manifest['state'] = []
    (backup / 'state').mkdir()
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')) + '\n').encode('utf-8')
    (backup / 'manifest.json').write_bytes(raw)
    original.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', sha256(raw).hexdigest()))
    rebound = load_request(restored.root, request['request_id'])
    assert {k: v for k, v in rebound.items() if k not in ('source', 'data_dir')} == {
        k: v for k, v in request.items() if k not in ('source', 'data_dir')}
    assert restored.resume_import(workspace, request['request_id'])['state'] == 'complete'
    assert restored.search(workspace, 'evidence')[0].quote == 'evidence'
    assert (backup / 'manifest.json').read_bytes() == raw


def test_unlisted_pending_bytes_block_enclosing_restore(tmp_path):
    import pytest
    app, _, _, _ = _prepared_backup(tmp_path)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    (backup / 'pending' / 'unlisted.json').write_text('{}', encoding='utf-8')
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='unexpected files'):
        restore_backup(backup, destination, digest)
    assert not destination.exists()


def test_pending_component_bytes_count_in_complete_backup_budget(tmp_path):
    import pytest
    app, _, original, _ = _prepared_backup(tmp_path)
    backup = tmp_path / 'backup'
    create_backup(app, backup)
    total = sum(path.stat().st_size for path in backup.rglob('*') if path.is_file())
    destination = tmp_path / 'one-byte-short'
    with pytest.raises(ValueError, match='budget|byte limit'):
        create_backup(app, destination, max_bytes=total - 1)
    assert not destination.exists()
    assert original.read_text(encoding='utf-8') == 'Prepared recovery preserves original evidence.'
    exact = tmp_path / 'exact'
    digest = create_backup(app, exact, max_bytes=total)
    assert restore_backup(exact, tmp_path / 'restored', digest).is_dir()


def test_cancel_during_pending_publication_leaves_no_completion_manifest(tmp_path, monkeypatch):
    import os
    import pytest
    app, _, original, _ = _prepared_backup(tmp_path)
    destination = tmp_path / 'cancelled-backup'
    original_link = os.link
    published_pending = False
    def link(source, target, *args, **kwargs):
        nonlocal published_pending
        result = original_link(source, target, *args, **kwargs)
        if target.is_relative_to(destination / 'pending'):
            published_pending = True
        return result
    monkeypatch.setattr(os, 'link', link)
    with pytest.raises(InterruptedError):
        create_backup(app, destination, cancelled=lambda: published_pending)
    assert published_pending and not (destination / 'manifest.json').exists()
    assert original.read_text(encoding='utf-8') == 'Prepared recovery preserves original evidence.'


@pytest.mark.parametrize('suffix,text', [
    ('.html', '<html><body><p>Retained evidence survives.</p></body></html>'),
    ('.md', '# Retained evidence\n\nEvidence survives.'),
    ('.eml', 'From: synthetic@example.invalid\nTo: local@example.invalid\n'
             'Subject: Retained evidence\nContent-Type: text/plain; charset=utf-8\n\nEvidence survives.\n'),
])
def test_prepared_extended_format_survives_relocation_and_resumes(tmp_path, suffix, text):
    app, workspace, original, request = _prepared_backup(tmp_path, suffix=suffix, text=text)
    backup = tmp_path / 'backup'
    digest = create_backup(app, backup)
    expected = original.read_bytes()
    original.unlink()
    restored = LibraryApp(restore_backup(backup, tmp_path / 'restored', digest))
    from pathlib import Path
    rebound = load_request(restored.root, request['request_id'])
    assert Path(rebound['source']).read_bytes() == expected
    assert rebound['expected_manifest'] == request['expected_manifest']
    assert restored.resume_import(workspace, request['request_id'])['state'] == 'complete'
    assert restored.search(workspace, 'survives')[0].quote == 'survives'


def test_same_size_pending_change_with_coarse_timestamps_blocks_completion(tmp_path, monkeypatch):
    from carr_library import pending_backup
    app, _, _, request = _prepared_backup(tmp_path)
    path = app.root / 'import-requests' / (request['request_id'] + '.json')
    before = path.read_bytes()
    after = before.replace(b'Prepared book', b'Replaced book')
    assert after != before and len(after) == len(before)
    original_stamp = pending_backup._stamp
    monkeypatch.setattr(pending_backup, '_stamp', lambda info: original_stamp(info)[:5] + (0, 0))
    checkpoints = pending_backup._source_checkpoints
    def changed(*args, **kwargs):
        result = checkpoints(*args, **kwargs)
        path.write_bytes(after)
        return result
    monkeypatch.setattr(pending_backup, '_source_checkpoints', changed)
    destination = tmp_path / 'rejected'
    with pytest.raises(ValueError, match='state changed'):
        create_backup(app, destination)
    assert path.read_bytes() == after
    assert not destination.exists()
