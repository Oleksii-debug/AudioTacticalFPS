"""Actual profile2/page snapshots preserve passive restore and source authority."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_budget import BackupBudget, MIB
from carr_library import library_backup as full
from carr_library.ingest.batches import ImportBatchStore
from carr_library.ingest.pipeline import canonical
from carr_library.ingest.resource_budget import ImportBudget
from carr_library.ingest.streamed_text import TextProfile
from carr_library.ingest.worker import load_request


def import_policy(**changes):
    return replace(ImportBudget(source_bytes=2*MIB, segment_bytes=MIB,
        segments=10_000, steps=2, seconds=120, memory_bytes=768*MIB,
        library_bytes=64*MIB, temporary_bytes=128*MIB), **changes)


def backup_policy(**changes):
    return replace(BackupBudget(memory_bytes=768*MIB, staging_bytes=256*MIB,
                                seconds=300), **changes)


def pages(tmp_path, *, selected=True):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Exact saved source pages')
    folder = tmp_path / 'selected-sources'
    folder.mkdir()
    first = folder / 'a.txt'
    first.write_bytes('First Ω.\r\n\fSecond Ї.\n\fThird exact source.'.encode('utf-8'))
    (folder / 'b.txt').write_bytes(first.read_bytes())
    (folder / 'c.txt').write_text('Independent retained source.', encoding='utf-8')
    kwargs = {'import_budget': import_policy()} if selected else {}
    one = app.plan_import_page(workspace, [folder], max_files=1, **kwargs)
    two = app.continue_import_scan(workspace, one['batch_id'], max_files=1)
    three = app.continue_import_scan(workspace, two['batch_id'], max_files=1)
    assert one['rows'][0]['state'] == three['rows'][0]['state'] == 'queued'
    assert two['rows'][0]['duplicate_of'] == (
        'batch:' + one['batch_id'] + ':' + one['rows'][0]['row_id'])
    return app, workspace, folder, (one, two, three)


def backup(app, tmp_path):
    destination = tmp_path / 'backup'
    checksum = full.create_backup(app, destination, backup_budget=backup_policy())
    manifest = full.verify_backup(destination, checksum, backup_budget=backup_policy())
    return destination, checksum, manifest


def identities(record):
    return tuple(tuple(row[key] for key in ('row_id', 'request_id', 'work_id', 'edition_id'))
                 for row in record['rows'])


def test_partial_profile2_and_scoped_pages_restore_retained_bytes_without_external_scan(
        tmp_path, monkeypatch):
    from carr_library.ingest import batch_scan
    app, workspace, folder, plans = pages(tmp_path)
    first = app.run_import_batch(workspace, plans[0]['batch_id'])
    assert first['state'] == 'queued' and first['rows'][0]['result']['state'] == 'pending'
    assert first['rows'][0]['result']['completed'] == 2
    request = load_request(app.root, first['rows'][0]['request_id'])
    profile = TextProfile(import_policy().source_bytes, import_policy())
    original = app.assets.resolve(request['asset_sha256'], profile=profile).read_bytes()
    destination, checksum, manifest = backup(app, tmp_path)
    pending = manifest['pending']
    assert pending['version'] == 3 and len(pending['scan_pages']) == 3
    assert all(row['profile']['schema'] == 'carr-streamed-text-profile/2'
               for row in pending['source_profiles'])
    assert 'backup_budget' not in manifest
    assert [row['filename'] for row in pending['scan_pages']] == sorted(
        'import-batches/' + plan['batch_id'] + '.json' for plan in plans)
    shutil.rmtree(folder)
    actual_scandir = batch_scan.os.scandir
    external_reads = []
    def forbid_old_root(value):
        candidate = Path(value)
        if candidate == folder or candidate.is_relative_to(folder):
            external_reads.append(candidate)
            pytest.fail('Passive restore or a revoked continuation opened an old external root.')
        return actual_scandir(value)
    class ScanOS:
        def __getattr__(self, name):
            return getattr(os, name)
        def scandir(self, value):
            return forbid_old_root(value)
    monkeypatch.setattr(batch_scan, 'os', ScanOS())
    target = tmp_path / 'restored'
    full.restore_backup(destination, target, checksum, backup_budget=backup_policy())
    restored = LibraryApp(target)
    rebound = [restored.import_batch(workspace, plan['batch_id']) for plan in plans]
    for before, after in zip(plans, rebound):
        assert identities(after) == identities(before)
        assert after['source_profile'] == before['source_profile']
        assert after['scan']['review_required'] is True
        assert after['scan']['cursor'] == before['scan']['cursor']
        assert after['scan']['start_sha256'] == before['scan']['start_sha256']
        assert Path(after['rows'][0]['source']).parent == restored.assets_dir
    assert rebound[1]['rows'][0]['duplicate_of'] == plans[1]['rows'][0]['duplicate_of']
    before = (target / 'import-batches' / (plans[0]['batch_id'] + '.json')).read_bytes()
    with pytest.raises(ValueError, match='restor|review|fresh'):
        restored.continue_import_scan(workspace, plans[0]['batch_id'])
    assert (target / 'import-batches' / (plans[0]['batch_id'] + '.json')).read_bytes() == before
    current = load_request(target, request['request_id'])
    assert current['source_profile'] == request['source_profile']
    assert current['source'] == rebound[0]['rows'][0]['source']
    assert all(key not in current for key in
               ('attempt', 'attempt_id', 'publication', 'publication_candidate'))
    final = restored.run_import_batch(workspace, plans[0]['batch_id'])
    assert final['state'] == 'complete' and final['rows'][0]['result']['completed'] == 3
    assert final['rows'][0]['result']['id'] == first['rows'][0]['result']['id']
    assert restored.assets.resolve(request['asset_sha256'], profile=profile).read_bytes() == original
    hit, = restored.search(workspace, 'Third exact source')
    assert restored.copy(workspace, hit.ref, hit.revision, 'quote') == 'Third exact source'
    independent = restored.run_import_batch(workspace, plans[2]['batch_id'])
    assert independent['state'] == 'complete'
    # A second genuine backup validates restored cross-page relations too.
    second = tmp_path / 'second-backup'
    second_checksum = full.create_backup(restored, second, backup_budget=backup_policy())
    assert full.verify_backup(second, second_checksum,
        backup_budget=backup_policy())['pending']['version'] == 3
    assert not external_reads


def test_profile2_journal_above_old_fragment_limit_restores_and_resumes_exact_job(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Actual 8193 logical fragments')
    source = tmp_path / 'fragments.txt'
    source.write_bytes(b'actual checkpoint\f' * 8192 + b'last actual fragment')
    selected = import_policy(steps=1)
    first = app.import_file(workspace, source, import_budget=selected)
    assert first['state'] == 'pending' and first['completed'] == 1
    destination, checksum, manifest = backup(app, tmp_path)
    with closing(sqlite3.connect(destination / 'pending' / 'imports.sqlite')) as journal:
        assert journal.execute('SELECT count FROM job').fetchone()[0] == 8193
        assert journal.execute('SELECT count(*) FROM checkpoint').fetchone()[0] == 1
    source.unlink()
    target = tmp_path / 'restored'
    full.restore_backup(destination, target, checksum, backup_budget=backup_policy())
    restored = LibraryApp(target)
    second = restored.resume_import(workspace, first['request_id'])
    assert second['state'] == 'pending' and second['completed'] == 2
    assert second['id'] == first['id']
    assert load_request(target, first['request_id'])['source_profile'] == (
        TextProfile(selected.source_bytes, selected).to_dict())
    with restored.read(workspace) as catalog:
        assert catalog.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
    assert manifest['pending']['version'] == 3


def test_unprofiled_pages_keep_component2_and_passive_restored_roots(tmp_path):
    app, workspace, folder, plans = pages(tmp_path, selected=False)
    destination, checksum, manifest = backup(app, tmp_path)
    assert manifest['pending']['version'] == 2
    assert 'source_profiles' not in manifest['pending']
    shutil.rmtree(folder)
    target = tmp_path / 'restored'
    full.restore_backup(destination, target, checksum, backup_budget=backup_policy())
    restored = LibraryApp(target)
    record = restored.import_batch(workspace, plans[0]['batch_id'])
    assert record['version'] == 2 and record['scan']['review_required'] is True
    assert 'source_profile' not in record
    completed = restored.run_import_batch(workspace, plans[0]['batch_id'])
    assert completed['state'] == 'complete'
    assert identities(completed) == identities(plans[0])


def rewrite_component(destination, manifest, plan, change, *, refresh_scan=True):
    name = 'import-batches/' + plan['batch_id'] + '.json'
    path = destination / 'pending' / name
    value = json.loads(path.read_bytes())
    change(value)
    raw = (canonical(value) + '\n').encode('utf-8')
    path.write_bytes(raw)
    entry = next(row for row in manifest['pending']['files'] if row['filename'] == name)
    entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    if refresh_scan:
        pin = next(row for row in manifest['pending']['scan_pages'] if row['filename'] == name)
        pin['scan_sha256'] = sha256(canonical(value['scan']).encode('utf-8')).hexdigest()


@pytest.mark.parametrize('change', [
    'scan-pin', 'missing-parent', 'changed-policy', 'changed-duplicate',
    'profile-inventory', 'request-profile',
])
def test_rehashed_page_profile_tampering_is_refused_before_restore_publication(tmp_path, change):
    app, workspace, folder, plans = pages(tmp_path)
    if change == 'request-profile':
        started = app.run_import_batch(workspace, plans[0]['batch_id'])
        assert started['rows'][0]['result']['state'] == 'pending'
    destination, _, original = backup(app, tmp_path)
    manifest = deepcopy(original)
    pending = manifest['pending']
    if change == 'scan-pin':
        pending['scan_pages'][0]['scan_sha256'] = '0' * 64
    elif change == 'missing-parent':
        name = 'import-batches/' + plans[0]['batch_id'] + '.json'
        (destination / 'pending' / name).unlink()
        pending['files'] = [row for row in pending['files'] if row['filename'] != name]
        pending['sources'] = [row for row in pending['sources'] if row['filename'] != name]
        pending['scan_pages'] = [row for row in pending['scan_pages'] if row['filename'] != name]
    elif change == 'changed-policy':
        rewrite_component(destination, manifest, plans[1],
            lambda value: value['source_profile']['import_budget'].__setitem__('steps', 1))
    elif change == 'changed-duplicate':
        rewrite_component(destination, manifest, plans[1],
            lambda value: value['rows'][0].__setitem__('duplicate_of',
                'batch:' + plans[0]['batch_id'] + ':' + plans[2]['rows'][0]['row_id']))
    elif change == 'profile-inventory':
        pending['source_profiles'][0]['profile']['import_budget']['steps'] = 1
    else:
        name = 'import-requests/' + plans[0]['rows'][0]['request_id'] + '.json'
        path = destination / 'pending' / name
        value = json.loads(path.read_bytes())
        value['source_profile']['import_budget']['steps'] = 1
        raw = (canonical(value) + '\n').encode('utf-8')
        path.write_bytes(raw)
        entry = next(row for row in pending['files'] if row['filename'] == name)
        entry.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
    raw = (canonical(manifest) + '\n').encode('utf-8')
    (destination / 'manifest.json').write_bytes(raw)
    checksum = sha256(raw).hexdigest()
    target = tmp_path / 'must-not-publish'
    source_before = {path.name: path.read_bytes() for path in folder.iterdir()}
    with pytest.raises(ValueError):
        full.restore_backup(destination, target, checksum, backup_budget=backup_policy())
    assert not target.exists()
    assert not list(tmp_path.glob('.carr-library-restore-*'))
    assert {path.name: path.read_bytes() for path in folder.iterdir()} == source_before
    with app.read(workspace) as catalog:
        assert catalog.execute('SELECT count(*) FROM revision').fetchone()[0] == 0
