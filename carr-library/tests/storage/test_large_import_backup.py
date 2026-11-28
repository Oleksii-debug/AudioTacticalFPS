"""Caller-selected backup resources over the unchanged common4 producer."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_budget import BackupBudget, MIB, GIB
from carr_library import backup_budget as budgets, library_backup as backup
from carr_library.ingest.resource_budget import ImportBudget
from carr_library.ingest import worker


def policy(**changes):
    return replace(BackupBudget(memory_bytes=768*MIB, staging_bytes=GIB,
        seconds=300), **changes)


def digest(path):
    value = sha256()
    with Path(path).open('rb') as stream:
        while data := stream.read(MIB):
            value.update(data)
    return value.hexdigest()


def populated(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact retained original')
    result = app.add_text(workspace, 'Small source', 'First exact Ω.\r\n\fSecond exact Ї.')
    assert result['state'] == 'complete'
    return app, workspace


def test_explicit_policy_keeps_existing_common4_wire_and_default_limits(tmp_path):
    app, workspace = populated(tmp_path)
    old = tmp_path / 'old-default'
    selected = tmp_path / 'selected'
    original = backup.create_backup(app, old)
    current = backup.create_backup(app, selected, backup_budget=policy())
    assert original == current
    assert (old / 'manifest.json').read_bytes() == (selected / 'manifest.json').read_bytes()
    manifest = json.loads((selected / 'manifest.json').read_bytes())
    assert manifest['version'] == 4 and 'backup_budget' not in manifest
    target = backup.restore_backup(selected, tmp_path / 'restored', current)
    restored = LibraryApp(target)
    hit, = restored.search(workspace, 'Second exact')
    assert restored.copy(workspace, hit.ref, hit.revision, 'quote') == 'Second exact'


@pytest.mark.parametrize('operation', ['create', 'verify', 'restore'])
def test_explicit_capacity_is_checked_before_content_read_or_publication(
        tmp_path, monkeypatch, operation):
    app, workspace = populated(tmp_path)
    folder = tmp_path / 'backup'
    checksum = backup.create_backup(app, folder)
    target = tmp_path / 'refused'
    observed = []
    def unavailable():
        observed.append(True)
        return 1
    monkeypatch.setattr(budgets, 'available_memory', unavailable)
    if operation != 'create':
        monkeypatch.setattr(backup, '_read_manifest',
            lambda *args, **kwargs: pytest.fail('Preflight must precede manifest decoding.'))
    with pytest.raises(ValueError, match='free memory'):
        if operation == 'create':
            backup.create_backup(app, target, backup_budget=policy())
        elif operation == 'verify':
            backup.verify_backup(folder, checksum, backup_budget=policy())
        else:
            backup.restore_backup(folder, target, checksum, backup_budget=policy())
    assert observed and not target.exists()


def test_selected_staging_disk_requirement_is_observed_before_copy(tmp_path, monkeypatch):
    app, workspace = populated(tmp_path)
    current = budgets.shutil.disk_usage(tmp_path)
    monkeypatch.setattr(budgets.shutil, 'disk_usage',
        lambda path: type(current)(current.total, current.used, 1))
    target = tmp_path / 'no-space'
    with pytest.raises(ValueError, match='free disk'):
        backup.create_backup(app, target, backup_budget=policy())
    assert not target.exists()


def test_archive_cannot_supply_a_larger_resource_policy(tmp_path):
    app, workspace = populated(tmp_path)
    folder = tmp_path / 'backup'
    backup.create_backup(app, folder)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest['backup_budget'] = policy(original_bytes=2*GIB, assets=50_000).to_dict()
    raw = (json.dumps(manifest, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (folder / 'manifest.json').write_bytes(raw)
    destination = tmp_path / 'unauthorized'
    with pytest.raises((ValueError, RuntimeError)):
        backup.restore_backup(folder, destination, sha256(raw).hexdigest(), backup_budget=policy())
    assert not destination.exists()


def test_paged_scan_roundtrip_retains_profile_ids_and_requires_new_external_review(
        tmp_path, monkeypatch):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Explicit reviewed scan')
    source = tmp_path / 'selected'
    source.mkdir()
    for index in range(3):
        (source / ('book-%d.txt' % index)).write_text(
            'Distinct original %d.\r\n' % index, encoding='utf-8')
    selected = ImportBudget(source_bytes=MIB, segment_bytes=MIB, segments=8,
        steps=9, seconds=120, memory_bytes=512*MIB,
        library_bytes=64*MIB, temporary_bytes=128*MIB)
    first = app.plan_import_page(workspace, [source], max_files=1, import_budget=selected)
    second = app.continue_import_scan(workspace, first['batch_id'], max_files=1)
    assert first['scan']['next'] == second['batch_id']
    saved = [app.import_batch(workspace, item['batch_id']) for item in (first, second)]
    assert saved[-1]['scan']['cursor']['complete'] is False
    expected = [[(row['row_id'], row['request_id'], row['work_id'], row['edition_id'],
                 row['manifest']) for row in item['rows']] for item in saved]
    folder = tmp_path / 'backup'
    checksum = backup.create_backup(app, folder, backup_budget=policy())
    before = {str(path.relative_to(folder)): digest(path)
              for path in folder.rglob('*') if path.is_file()}
    shutil.rmtree(source)
    shutil.rmtree(app.root)
    with monkeypatch.context() as blocked:
        blocked.setattr(worker, '_invoke', lambda *a, **k: pytest.fail('Restore must remain passive.'))
        target = backup.restore_backup(folder, tmp_path / 'restored', checksum,
                                       backup_budget=policy())
    restored = LibraryApp(target)
    for index, item in enumerate(saved):
        current = restored.import_batch(workspace, item['batch_id'])
        assert current['version'] == 3 and current['import_budget'] == selected.to_dict()
        assert [(row['row_id'], row['request_id'], row['work_id'], row['edition_id'],
                 row['manifest']) for row in current['rows']] == expected[index]
        assert current['scan']['review_required'] is True
        assert current['scan']['cursor'] == item['scan']['cursor']
    with pytest.raises(ValueError, match='Restored scan roots require a fresh explicit selection'):
        restored.continue_import_scan(workspace, second['batch_id'], max_files=1)
    # The already-retained reviewed page remains an explicit runnable operation.
    finished = restored.run_import_batch(workspace, first['batch_id'], max_items=1)
    assert finished['counts']['complete'] == 1
    assert {str(path.relative_to(folder)): digest(path)
            for path in folder.rglob('*') if path.is_file()} == before


def test_real_66mib_pending_source_roundtrip_and_explicit_same_job_completion(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Large original recovery')
    source = tmp_path / 'large.txt'
    count = 66
    with source.open('wb') as stream:
        for index in range(count):
            raw = ('Original segment %03d Ω.\r\n' % index).encode('utf-8')
            # Purpose-written actual bytes: one 1 MiB record, no sparse file.
            stream.write(raw)
            stream.write(b' ' * (MIB - len(raw)))
            if index + 1 < count:
                stream.write(b'\f')
    source_digest, source_size = digest(source), source.stat().st_size
    assert source_size > 64*MIB
    selected = ImportBudget(source_bytes=68*MIB, segment_bytes=2*MIB,
        segments=count, steps=count, seconds=300, memory_bytes=512*MIB,
        library_bytes=GIB, temporary_bytes=256*MIB)
    first = app.import_file(workspace, source, title='Large exact source', import_budget=selected)
    assert first['state'] == 'pending' and first['completed'] == count, first
    request = worker.load_request(app.root, first['request_id'])
    assert request['job'] == first['id'] and request['asset_sha256'] == source_digest
    with pytest.raises((ValueError, RuntimeError)):
        backup.create_backup(app, tmp_path / 'default-too-small')
    chosen = policy(original_bytes=68*MIB, segments=count,
        journal_bytes=256*MIB, pending_bytes=512*MIB, asset_bytes=512*MIB)
    folder = tmp_path / 'large-backup'
    checksum = backup.create_backup(app, folder, backup_budget=chosen)
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert manifest['version'] == 4 and manifest['pending']['assets'][0]['sha256'] == source_digest
    assert 'backup_budget' not in manifest
    source.unlink()
    shutil.rmtree(app.root)
    with pytest.raises((ValueError, RuntimeError)):
        backup.restore_backup(folder, tmp_path / 'default-restore', checksum)
    assert not (tmp_path / 'default-restore').exists()
    target = backup.restore_backup(folder, tmp_path / 'large-restored', checksum,
                                   backup_budget=chosen)
    restored = LibraryApp(target)
    recovered = worker.load_request(restored.root, first['request_id'])
    assert recovered['import_budget'] == selected.to_dict()
    assert recovered['job'] == first['id']
    assert 'attempt' not in recovered and 'publication_candidate' not in recovered
    original = restored.assets.resolve(source_digest, max_bytes=68*MIB)
    assert original.stat().st_size == source_size and digest(original) == source_digest
    final = restored.resume_import(workspace, first['request_id'])
    assert final['state'] == 'complete' and final['id'] == first['id'], final
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM page WHERE revision=?', (final['id'],)).fetchone()[0] == count
        first_raw = db.execute('SELECT raw FROM page WHERE revision=? AND ordinal=1', (final['id'],)).fetchone()[0]
        last_raw = db.execute('SELECT raw FROM page WHERE revision=? AND ordinal=?',
                              (final['id'], count)).fetchone()[0]
        assert first_raw.startswith('Original segment 000 Ω.\r\n')
        assert last_raw.startswith('Original segment 065 Ω.\r\n')
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 1
    assert digest(original) == source_digest


def test_actual_1001_sealed_segments_restore_only_under_explicit_count_policy(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('More retained logical segments')
    source = tmp_path / 'segments.txt'
    raw = '\f'.join('Exact segment %04d.\r\n' % index for index in range(1001)).encode()
    source.write_bytes(raw)
    selected = ImportBudget(source_bytes=MIB, segment_bytes=4096, segments=1001,
        steps=1002, seconds=300, memory_bytes=512*MIB,
        library_bytes=128*MIB, temporary_bytes=128*MIB)
    result = app.import_file(workspace, source, title='Logical segment identity', import_budget=selected)
    assert result['state'] == 'complete' and result['completed'] == 1001, result
    chosen = policy(segments=1001, checkpoints=2000)
    folder = tmp_path / 'segments-backup'
    with pytest.raises((ValueError, RuntimeError)):
        backup.create_backup(app, tmp_path / 'legacy-count')
    checksum = backup.create_backup(app, folder, backup_budget=chosen)
    target = backup.restore_backup(folder, tmp_path / 'segments-restored', checksum,
                                   backup_budget=chosen)
    restored = LibraryApp(target)
    assert restored.assets.resolve(sha256(raw).hexdigest()).read_bytes() == raw
    with restored.read(workspace) as db:
        rows = list(db.execute('SELECT ordinal,raw FROM page WHERE revision=? ORDER BY ordinal',
                               (result['id'],)))
        assert len(rows) == 1001
        assert rows[-1][1] == 'Exact segment 1000.\r\n'


def test_disposable_search_cache_is_not_a_backup_producer(tmp_path):
    from carr_library.backup_sidecars import _capture
    from carr_library.state_backup import snapshot_state
    app, workspace = populated(tmp_path)
    cache = app.root / 'search-cache'
    cache.mkdir()
    (cache / 'scope-v1-foreign.sqlite').write_bytes(b'Disposable derived search index')
    active = cache / '.active-private'
    active.mkdir()
    (active / 'results.sqlite').write_bytes(b'Disposable ranked results')
    captured, _ = _capture(app.root)
    assert not any(name.startswith('search-cache/') for name in captured)
    state = tmp_path / 'historical-state'
    entries = snapshot_state(app.root / 'state', state)
    assert not any('search-cache' in item['filename'] for item in entries)
    folder = tmp_path / 'backup'
    checksum = backup.create_backup(app, folder)
    target = backup.restore_backup(folder, tmp_path / 'restored', checksum)
    assert not (folder / 'search-cache').exists() and not (target / 'search-cache').exists()
    assert (active / 'results.sqlite').read_bytes() == b'Disposable ranked results'

def test_catalog_metadata_budget_is_enforced_before_retaining_the_next_original(
        tmp_path, monkeypatch):
    app, workspace = populated(tmp_path)
    chosen = BackupBudget(manifest_bytes=1, memory_bytes=512*MIB + 8,
                          staging_bytes=GIB)
    monkeypatch.setattr(backup, 'source_snapshot',
        lambda *args, **kwargs: pytest.fail('Reject metadata accumulation before original copying.'))
    target = tmp_path / 'metadata-bound'
    with pytest.raises(ValueError, match='memory allowance'):
        backup.create_backup(app, target, backup_budget=chosen)
    assert not target.exists()


def test_publication_checks_cancellation_between_actual_original_links(tmp_path, monkeypatch):
    import os
    app, workspace = populated(tmp_path)
    app.add_text(workspace, 'Another source', 'Another independently retained original.')
    target = tmp_path / 'cancelled-publication'
    actual_link = os.link
    linked = []
    def link(source, destination, *args, **kwargs):
        result = actual_link(source, destination, *args, **kwargs)
        if Path(destination).parent == target / 'originals':
            linked.append(Path(destination))
        return result
    monkeypatch.setattr(os, 'link', link)
    with pytest.raises(InterruptedError):
        backup.create_backup(app, target, backup_budget=policy(),
                             cancelled=lambda: bool(linked))
    assert len(linked) == 1
    assert target.is_dir() and not (target / 'manifest.json').exists()
    assert len(list(app.assets_dir.glob('*.txt'))) == 2


def test_restore_checks_cancellation_after_stage_cleanup_before_completion_marker(tmp_path):
    app, workspace = populated(tmp_path)
    folder = tmp_path / 'backup'
    checksum = backup.create_backup(app, folder)
    target = tmp_path / 'cancelled-at-final-marker'
    observed = []
    def stopped():
        ready = ((target / backup.INCOMPLETE).exists()
                 and not list(tmp_path.glob('.carr-library-restore-*')))
        if ready:
            observed.append(True)
        return ready
    with pytest.raises(InterruptedError):
        backup.restore_backup(folder, target, checksum, backup_budget=policy(), cancelled=stopped)
    assert observed and (target / backup.INCOMPLETE).is_file()
    with pytest.raises(ValueError, match='incomplete'):
        LibraryApp(target)
