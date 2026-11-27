"""Real import recovery across the canonical and selected-host backup layouts."""
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.intake import snapshot
from carr_library.extract.page_ocr import Config
from carr_library.ingest.worker import load_request, prepare_request, record_progress
from carr_library.library_backup import create_backup, restore_backup, verify_backup, INCOMPLETE
from carr_library import pending_backup


def _files(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob('*') if path.is_file()}


def _resign(root, manifest):
    raw = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (root / 'manifest.json').write_bytes(raw)
    return sha256(raw).hexdigest()


def _request(tmp_path, state='queued', *, suffix='.txt'):
    app = LibraryApp(tmp_path / 'source-library')
    workspace = app.create_workspace('Exact pending sources')
    original = tmp_path / ('original' + suffix)
    original.write_text('First source page.\fSecond source page.\fThird source page.', encoding='utf-8')
    if suffix == '.html':
        original.write_text('<html><body><p>Exact HTML original.</p></body></html>', encoding='utf-8')
    metadata = dict(title='Unfinished source', authors=[], language=None, edition=None, volume=None,
                    collections=[], work_id=uuid4().hex, edition_id=uuid4().hex)
    planned = snapshot(original.parent, original.name).manifest()
    request = prepare_request(app.root, workspace, original, metadata, Config(), expected_manifest=planned)
    if state != 'queued':
        calls = 0

        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 2

        result = app._import_file(workspace, original, **metadata, cancelled=cancelled,
                                  _request_id=request['request_id'], _expected_manifest=planned)
        assert result['state'] == 'cancelled' and result['completed'] == 1
        if state == 'failed':
            with sqlite3.connect(app.root / 'imports.sqlite') as journal:
                journal.execute("UPDATE job SET state='failed',error='Synthetic retryable failure'")
            result['state'] = 'failed'
        record_progress(app.root, request['request_id'], state=state, result=result)
    return app, workspace, original, load_request(app.root, request['request_id'])


def _backup(app, folder, shape):
    digest = create_backup(app, folder)
    value = json.loads((folder / 'manifest.json').read_bytes())
    if shape == 'canonical-v4':
        # Literal canonical schema, using the reused canonical pending producer.
        # No host-only state is represented as a canonical state component.
        assert value.pop('sidecars') == []
        del value['layout']
        value.update(version=4, state=[])
        (folder / 'state').mkdir()
        digest = _resign(folder, value)
    else:
        assert value['version'] == 5 and value['layout'] == 'sidecars-pending-v1'
    return value, digest


@pytest.mark.parametrize('shape', ['canonical-v4', 'host-v5'])
@pytest.mark.parametrize('state', ['queued', 'running', 'failed'])
def test_preserve_pending_identity_rebase_and_resume(tmp_path, shape, state):
    app, workspace, original, request = _request(tmp_path, state)
    folder = tmp_path / 'backup'
    manifest, digest = _backup(app, folder, shape)
    original.unlink()
    before = _files(folder)
    assert verify_backup(folder, digest) == manifest
    assert _files(folder) == before
    target = restore_backup(folder, tmp_path / 'restored', digest)
    restored = LibraryApp(target)
    after = load_request(target, request['request_id'])
    asset = manifest['pending']['assets'][0]
    assert after == dict(request, data_dir=str(target), source=str(target / 'originals' / asset['filename']))
    assert after['metadata']['edition_id'] == request['metadata']['edition_id']
    if 'pending/imports.sqlite' in before:
        assert (target / 'imports.sqlite').read_bytes() == before['pending/imports.sqlite']
    assert _files(folder) == before
    # A subsequent backup must retain an unfinished restored import too.
    second = tmp_path / 'second-backup'
    second_digest = create_backup(restored, second)
    assert verify_backup(second, second_digest)['pending']['assets'] == manifest['pending']['assets']
    completed = restored.resume_import(workspace, request['request_id'])
    assert completed['state'] == 'complete'
    assert completed['edition'] == request['metadata']['edition_id']
    assert restored.search(workspace, 'Second')[0].quote == 'Second'


def test_no_pending_state_keeps_the_host_v4_contract(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    app.create_workspace('No pending jobs')
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    manifest = verify_backup(folder, digest)
    assert manifest['version'] == 4
    assert 'pending' not in manifest and 'layout' not in manifest
    before = _files(folder)
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert len(LibraryApp(target).workspaces()) == 1
    assert not (target / 'imports.sqlite').exists()
    assert _files(folder) == before


def test_pending_extended_format_is_retained_without_running_its_parser(tmp_path):
    app, workspace, original, request = _request(tmp_path, suffix='.html')
    folder = tmp_path / 'backup'
    manifest, digest = _backup(app, folder, 'host-v5')
    original.unlink()
    target = restore_backup(folder, tmp_path / 'restored', digest)
    retained = Path(load_request(target, request['request_id'])['source'])
    assert retained.suffix == '.html'
    assert sha256(retained.read_bytes()).hexdigest() == manifest['pending']['assets'][0]['sha256']
    assert load_request(target, request['request_id'])['state'] == request['state']


@pytest.mark.parametrize('shape', ['canonical-v4', 'host-v5'])
@pytest.mark.parametrize('change', ['traversal', 'duplicate', 'retarget', 'changed-bytes', 'unlisted-file'])
def test_pending_corruption_never_publishes_a_restored_library(tmp_path, shape, change):
    app, _, _, request = _request(tmp_path, 'failed')
    folder = tmp_path / 'backup'
    manifest, digest = _backup(app, folder, shape)
    pending = manifest['pending']
    if change == 'traversal':
        pending['files'][0]['filename'] = '../library.sqlite'
    elif change == 'duplicate':
        pending['files'].append(dict(pending['files'][0]))
    elif change == 'retarget':
        pending['sources'][0]['source'] = str(tmp_path / 'unselected.txt')
    elif change == 'changed-bytes':
        asset = folder / 'pending' / 'originals' / pending['assets'][0]['filename']
        data = asset.read_bytes()
        asset.write_bytes(b'X' + data[1:])
    else:
        (folder / 'pending' / 'unlisted.txt').write_text('Unbound bytes')
    digest = _resign(folder, manifest)
    before = _files(folder)
    with pytest.raises(ValueError):
        verify_backup(folder, digest)
    with pytest.raises(ValueError):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()
    assert _files(folder) == before


@pytest.mark.parametrize('change', ['missing-layout', 'wrong-layout', 'mixed-state', 'missing-pending'])
def test_v5_requires_its_exact_named_schema_before_writable_sqlite(tmp_path, monkeypatch, change):
    app, *_ = _request(tmp_path)
    folder = tmp_path / 'backup'
    manifest, _ = _backup(app, folder, 'host-v5')
    if change == 'missing-layout':
        del manifest['layout']
    elif change == 'wrong-layout':
        manifest['layout'] = 'unrelated-v5-backup'
    elif change == 'mixed-state':
        manifest['state'] = []
    else:
        del manifest['pending']
    digest = _resign(folder, manifest)
    monkeypatch.setattr('carr_library.library_backup.Storage',
                        lambda *a, **k: pytest.fail('Opened writable SQLite before schema rejection'))
    with pytest.raises(ValueError):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists()


@pytest.mark.parametrize('budget', [{'max_sidecar_files': 1}, {'max_sidecar_bytes': 1}])
def test_pending_files_count_toward_existing_caller_budgets(tmp_path, budget):
    app, *_ = _request(tmp_path)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    with pytest.raises(ValueError, match='budget'):
        create_backup(app, tmp_path / 'rejected-backup', **budget)
    with pytest.raises(ValueError, match='budget'):
        verify_backup(folder, digest, **budget)
    with pytest.raises(ValueError, match='budget'):
        restore_backup(folder, tmp_path / 'rejected-restore', digest, **budget)
    assert not (tmp_path / 'rejected-backup').exists()
    assert not (tmp_path / 'rejected-restore').exists()


def test_changed_planned_input_does_not_publish_a_complete_archive(tmp_path):
    app, _, original, _ = _request(tmp_path)
    original.write_text('Different source bytes after planning.', encoding='utf-8')
    with pytest.raises(ValueError, match='changed'):
        create_backup(app, tmp_path / 'rejected')
    assert not (tmp_path / 'rejected').exists()


def test_restore_staging_failure_preserves_archive_and_allows_fresh_retry(tmp_path, monkeypatch):
    app, _, _, request = _request(tmp_path)
    folder = tmp_path / 'backup'
    _, digest = _backup(app, folder, 'canonical-v4')
    before = _files(folder)
    original_write = pending_backup._write

    def fail_rebase(path, raw):
        original_write(path, raw)
        if path.parent.name == 'import-requests':
            raise OSError('Synthetic pending request staging failure')

    with monkeypatch.context() as patch:
        patch.setattr(pending_backup, '_write', fail_rebase)
        with pytest.raises(OSError, match='staging failure'):
            restore_backup(folder, tmp_path / 'restored', digest)
    assert not (tmp_path / 'restored').exists()
    assert _files(folder) == before
    target = restore_backup(folder, tmp_path / 'restored', digest)
    assert load_request(target, request['request_id'])['data_dir'] == str(target)


def test_pending_publication_failure_keeps_incomplete_guard(tmp_path, monkeypatch):
    import os
    app, *_ = _request(tmp_path)
    folder = tmp_path / 'backup'
    digest = create_backup(app, folder)
    target = tmp_path / 'restored'
    original_link = os.link

    def fail_request(source, destination, *args, **kwargs):
        if Path(destination).parent == target / 'import-requests':
            raise OSError('Synthetic pending publication failure')
        return original_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, 'link', fail_request)
    with pytest.raises(OSError, match='publication failure'):
        restore_backup(folder, target, digest)
    assert (target / INCOMPLETE).is_file()
    with pytest.raises(ValueError, match='incomplete'):
        LibraryApp(target)


def test_canonical_state_without_a_producer_is_still_rejected(tmp_path):
    app, *_ = _request(tmp_path)
    folder = tmp_path / 'backup'
    manifest, _ = _backup(app, folder, 'canonical-v4')
    raw = b'{"unrecognized":"synthetic producer state"}'
    path = folder / 'state' / 'providers' / 'settings.json'
    path.parent.mkdir()
    path.write_bytes(raw)
    manifest['state'] = [dict(filename='providers/settings.json', size_bytes=len(raw), sha256=sha256(raw).hexdigest())]
    digest = _resign(folder, manifest)
    before = _files(folder)
    with pytest.raises(ValueError, match='validated backup adapter'):
        restore_backup(folder, tmp_path / 'rejected', digest)
    assert not (tmp_path / 'rejected').exists() and _files(folder) == before


@pytest.mark.parametrize('shape', ['canonical-v4', 'host-v5'])
def test_registered_original_also_pending_is_published_once(tmp_path, shape):
    app, workspace, original, request = _request(tmp_path)
    published = app.import_file(workspace, original, title='Already published source')
    folder = tmp_path / 'backup'
    manifest, digest = _backup(app, folder, shape)
    assert len(manifest['assets']) == len(manifest['pending']['assets']) == 1
    assert manifest['assets'][0] == manifest['pending']['assets'][0]
    original.unlink()
    target = restore_backup(folder, tmp_path / 'restored', digest)
    restored = LibraryApp(target)
    originals = list((target / 'originals').iterdir())
    assert len(originals) == 1 and originals[0].stat().st_nlink == 1
    assert load_request(target, request['request_id'])['source'] == str(originals[0])
    completed = restored.resume_import(workspace, request['request_id'])
    assert completed['edition'] != published['edition']
    assert completed['state'] == 'complete'
    assert len(restored.browse(workspace)) == 2
    assert len(list((target / 'originals').iterdir())) == 1
