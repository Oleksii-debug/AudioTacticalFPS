"""Real bounded backup admission, full validation and interruption contracts.

The larger fixtures prove individual admission boundaries, not release capacity
or sustained import throughput. Full 50k measurements are a separate gate.
"""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.backup_capacity import LARGE_SOURCE_PROFILE, capacity
from carr_library import backup_sidecars, library_backup, pending_backup
from carr_library.catalog.membership import Catalog
from carr_library.ingest.pipeline import canonical, digest
from carr_library.ingest.writer_guard import exclusive_writer
from carr_library.storage import revisions
from carr_library.storage.revisions import Storage, Page, _sqlite_cancellation

LARGE = LARGE_SOURCE_PROFILE


def populated(root, count=1):
    app = LibraryApp(root)
    workspace = app.create_workspace('Capacity boundary')
    results = []
    for index in range(count):
        results.append(app.add_text(workspace, 'Original ' + str(index),
            'Exact original ' + str(index) + '\fSecond segment ' + str(index)))
    assert all(row['state'] == 'complete' for row in results)
    return app, workspace, results


def resign(folder, change):
    path = folder / 'manifest.json'
    value = json.loads(path.read_bytes())
    change(value)
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')) + '\n').encode('utf-8')
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


def test_default_and_named_allowances_are_finite_independent_and_wire_neutral():
    normal = capacity()
    assert normal == dict(database=512 * 1024**2, journal=512 * 1024**2,
        assets=8 * 1024**3, pending=8 * 1024**3, manifest=8 * 1024**2,
        asset_count=10000, jobs=10000, files=25000, checkpoints=100000,
        journal_seconds=60)
    assert capacity(LARGE) == dict(database=64 * 1024**3, journal=32 * 1024**3,
        assets=128 * 1024**3, pending=64 * 1024**3, manifest=64 * 1024**2,
        asset_count=100000, jobs=100000, files=250000, checkpoints=2097152,
        journal_seconds=3600)
    normal['jobs'] = 0
    assert capacity()['jobs'] == pending_backup.MAX_JOBS == 10000
    assert library_backup.MAX_ASSETS == 10000
    assert library_backup.MAX_DATABASE == 512 * 1024**2
    assert library_backup.MAX_MANIFEST == 8 * 1024**2


@pytest.mark.parametrize('bad', [True, False, 1, {}, [], '', 'auto', 'large-source-v2'])
def test_invalid_profile_refuses_before_source_or_destination_access(tmp_path, bad):
    missing = tmp_path / 'absent'
    class UnopenedApp:
        @property
        def root(self):
            pytest.fail('Invalid profile touched the live library.')
    with pytest.raises(ValueError, match='profile'):
        library_backup.create_backup(UnopenedApp(), missing, capacity_profile=bad)
    with pytest.raises(ValueError, match='profile'):
        library_backup.verify_backup(missing, 'a' * 64, capacity_profile=bad)
    with pytest.raises(ValueError, match='profile'):
        library_backup.restore_backup(missing, tmp_path / 'target', 'a' * 64,
                                      capacity_profile=bad)
    assert list(tmp_path.iterdir()) == []


def test_explicit_profile_preserves_exact_small_common4_pending1_bytes(tmp_path):
    app, _, _ = populated(tmp_path / 'source')
    first, second = tmp_path / 'ordinary', tmp_path / 'selected'
    ordinary = app.backup(first)
    selected = app.backup(second, capacity_profile=LARGE)
    assert selected == ordinary
    assert (first / 'manifest.json').read_bytes() == (second / 'manifest.json').read_bytes()
    value = library_backup.verify_backup(second, selected, capacity_profile=LARGE)
    assert value['version'] == 4 and value['pending']['version'] == 1
    assert 'capacity_profile' not in value and 'capacity_profile' not in value['pending']
    assert library_backup.verify_backup(second, selected) == value


def test_actual_10001_registered_originals_need_explicit_profile_and_restore(tmp_path):
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Registered original boundary')
    expected = {}
    # Real catalog originals, deliberately not an ingestion-throughput fixture.
    with exclusive_writer(app.root), closing(Storage(app.database)) as storage:
        with storage.transaction():
            catalog = Catalog(storage.db, workspace)
            catalog.add_work('capacity-work', 'Retained originals')
            for index in range(10001):
                raw = ('Retained capacity input %05d' % index).encode('utf-8')
                identity = sha256(raw).hexdigest()
                app.assets.retain(raw, 'text/plain', identity)
                catalog.add_edition('capacity-edition-%05d' % index, 'capacity-work',
                    dict(sha256=identity, size=len(raw), mime='text/plain'))
                expected[identity + '.txt'] = raw
        storage.check()
    ordinary, selected = tmp_path / 'ordinary', tmp_path / 'selected'
    with pytest.raises(ValueError, match='asset count'):
        app.backup(ordinary)
    assert not ordinary.exists()
    identity = app.backup(selected, capacity_profile=LARGE)
    with pytest.raises(ValueError):
        library_backup.verify_backup(selected, identity)
    manifest = library_backup.verify_backup(selected, identity, capacity_profile=LARGE)
    assert len(manifest['assets']) == 10001 and manifest['version'] == 4
    retired = tmp_path / 'retired-source'
    app.root.rename(retired)
    target = tmp_path / 'restored'
    library_backup.restore_backup(selected, target, identity, capacity_profile=LARGE)
    restored = LibraryApp(target)
    with restored.read(workspace) as db:
        assert db.execute('SELECT count(*) FROM catalog_edition').fetchone()[0] == 10001
    assert {p.name for p in restored.assets_dir.iterdir()} == set(expected)
    for name, raw in expected.items():
        assert (restored.assets_dir / name).read_bytes() == raw
    assert not app.root.exists() and retired.exists()


def test_actual_10001_valid_pending_jobs_keep_every_identity_after_restore(tmp_path):
    app, workspace, _ = populated(tmp_path / 'source')
    with closing(sqlite3.connect(app.root / 'imports.sqlite')) as db:
        row = db.execute('SELECT * FROM job').fetchone()
        assert row is not None
        asset, config, count = row[3], row[4], row[5]
        rows = []
        for index in range(10000):
            edition = 'capacity-pending-%05d' % index
            identity = digest(canonical([workspace, edition, asset, config]))
            rows.append((identity, workspace, edition, asset, config, count, None, 'pending', None))
        db.executemany('INSERT INTO job VALUES(?,?,?,?,?,?,?,?,?)', rows)
        db.commit()
        before = db.execute('SELECT * FROM job ORDER BY id').fetchall()
    ordinary, selected = tmp_path / 'ordinary', tmp_path / 'selected'
    with pytest.raises(ValueError, match='row budget'):
        app.backup(ordinary)
    assert not ordinary.exists()
    identity = app.backup(selected, capacity_profile=LARGE)
    assert library_backup.verify_backup(selected, identity, capacity_profile=LARGE)['pending']['version'] == 1
    # Both default verify and restore must reapply the default pending limit.
    with pytest.raises(ValueError, match='row budget'):
        library_backup.verify_backup(selected, identity)
    target = tmp_path / 'restored'
    with pytest.raises(ValueError, match='row budget'):
        library_backup.restore_backup(selected, target, identity)
    assert not target.exists()
    library_backup.restore_backup(selected, target, identity, capacity_profile=LARGE)
    with closing(sqlite3.connect(target / 'imports.sqlite')) as db:
        assert db.execute('SELECT * FROM job ORDER BY id').fetchall() == before


def test_pending_file_allowance_is_explicit_and_does_not_admit_unknown_payloads(tmp_path):
    files = [dict(filename='import-requests/%032x.json' % index, size_bytes=2, sha256='a' * 64)
             for index in range(25001)]
    value = dict(version=1, data_dir=str(tmp_path), files=files, assets=[], sources=[])
    with pytest.raises(ValueError, match='manifest'):
        pending_backup.validate_pending(value)
    pending_backup.validate_pending(value, capacity_profile=LARGE)
    invalid = deepcopy(value)
    invalid['files'][-1]['filename'] = 'unrecognized-state.json'
    with pytest.raises(ValueError, match='file manifest'):
        pending_backup.validate_pending(invalid, capacity_profile=LARGE)
    invalid = deepcopy(value)
    invalid['files'][-1]['size_bytes'] = pending_backup.MAX_REQUEST + 1
    with pytest.raises(ValueError, match='byte budget'):
        pending_backup.validate_pending(invalid, capacity_profile=LARGE)


@pytest.mark.parametrize('component', ['complete', 'pending'])
def test_rehashed_manifest_cannot_grant_a_capacity_profile(tmp_path, component):
    app, _, _ = populated(tmp_path / 'source')
    folder = tmp_path / 'backup'
    app.backup(folder)
    def change(value):
        record = value if component == 'complete' else value['pending']
        record['capacity_profile'] = LARGE
    identity = resign(folder, change)
    for profile in (None, LARGE):
        with pytest.raises(ValueError):
            library_backup.restore_backup(folder, tmp_path / 'target', identity,
                                          capacity_profile=profile)
        assert not (tmp_path / 'target').exists()


@pytest.mark.parametrize('damage', ['source-bytes', 'schema', 'checkpoint'])
def test_large_allowance_preserves_actual_content_and_schema_checks(tmp_path, damage):
    app, _, _ = populated(tmp_path / 'source')
    folder = tmp_path / 'backup'
    identity = app.backup(folder, capacity_profile=LARGE)
    if damage == 'source-bytes':
        path = next((folder / 'originals').iterdir())
        raw = path.read_bytes()
        path.write_bytes(b'X' + raw[1:])
    elif damage == 'schema':
        with closing(sqlite3.connect(folder / 'library.sqlite')) as db:
            db.execute('CREATE TABLE unauthorized(value)')
            db.commit()
        def change(value):
            raw = (folder / 'library.sqlite').read_bytes()
            value['database'].update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
        identity = resign(folder, change)
    else:
        journal = folder / 'pending/imports.sqlite'
        with closing(sqlite3.connect(journal)) as db:
            row = db.execute('SELECT job,ordinal,payload FROM checkpoint LIMIT 1').fetchone()
            value = json.loads(row[2])
            value['raw_text'] = 'Different retained text'
            payload = canonical(value)
            db.execute('UPDATE checkpoint SET payload=?,digest=? WHERE job=? AND ordinal=?',
                       (payload, digest(payload), row[0], row[1]))
            db.commit()
        def change(value):
            raw = journal.read_bytes()
            item = next(item for item in value['pending']['files'] if item['filename'] == 'imports.sqlite')
            item.update(size_bytes=len(raw), sha256=sha256(raw).hexdigest())
        identity = resign(folder, change)
    with pytest.raises((ValueError, sqlite3.Error)):
        library_backup.restore_backup(folder, tmp_path / 'target', identity, capacity_profile=LARGE)
    assert not (tmp_path / 'target').exists()


def test_grouped_jobs_validate_every_actual_source_without_cross_asset_rescans(tmp_path, monkeypatch):
    app, _, results = populated(tmp_path / 'source', 8)
    actual = pending_backup._source_checkpoints
    visits = []
    def checked(asset, raw, jobs, journal, *, cancelled=lambda: False):
        visits.append((asset['sha256'], tuple(jobs)))
        assert all(row['asset'] == asset['sha256'] for row in jobs.values())
        return actual(asset, raw, jobs, journal, cancelled=cancelled)
    monkeypatch.setattr(pending_backup, '_source_checkpoints', checked)
    folder = tmp_path / 'backup'
    identity = app.backup(folder, capacity_profile=LARGE)
    assert len(visits) == 8 and sum(len(row[1]) for row in visits) == 8
    assert {row[1][0] for row in visits} == {row['id'] for row in results}
    visits.clear()
    library_backup.restore_backup(folder, tmp_path / 'restored', identity, capacity_profile=LARGE)
    assert len(visits) == 8 and sum(len(row[1]) for row in visits) == 8


def test_actual_sqlite_copy_cancellation_preserves_original_exception_and_no_output(tmp_path):
    class StopCopy(InterruptedError):
        pass
    sentinel = StopCopy('Actual online copy was cancelled.')
    source = Storage(tmp_path / 'source.sqlite')
    try:
        source.publish_revision('w', 'e', 'r', 'a' * 64, '{}',
                                (Page('Actual database page ' * 100000),))
        real = source.db
        copied = []
        class ObservedConnection:
            def __getattr__(self, name):
                return getattr(real, name)
            def backup(self, target, **kwargs):
                progress = kwargs['progress']
                def observed(status, remaining, total):
                    copied.append((remaining, total))
                    return progress(status, remaining, total)
                return real.backup(target, **{**kwargs, 'progress': observed})
        source.db = ObservedConnection()
        def cancelled():
            if copied:
                raise sentinel
            return False
        with pytest.raises(StopCopy) as caught:
            source.backup(tmp_path / 'cancelled.sqlite', cancelled=cancelled,
                          maximum_bytes=64 * 1024**2)
        assert caught.value is sentinel and copied
        assert not (tmp_path / 'cancelled.sqlite').exists()
        assert not list(tmp_path.glob('.carr-backup-*'))
        source.db = real
        source.check()
    finally:
        source.close()


def test_actual_sqlite_vm_preserves_single_callback_exception_and_clears_handler():
    sentinel = InterruptedError('One actual VM checkpoint.')
    calls = []
    with closing(sqlite3.connect(':memory:')) as db:
        def checkpoint():
            calls.append(True)
            if len(calls) == 2:
                raise sentinel
        with pytest.raises(InterruptedError) as caught:
            with _sqlite_cancellation(db, checkpoint):
                db.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000) SELECT sum(x) FROM n').fetchone()
        assert caught.value is sentinel and len(calls) == 2
        # The same connection is usable; the owned interrupt handler is gone.
        assert db.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n').fetchone()[0] == 50005000
        assert len(calls) == 2


def test_actual_database_size_bound_refuses_before_snapshot_publication(tmp_path):
    with closing(Storage(tmp_path / 'source.sqlite')) as source:
        source.publish_revision('w', 'e', 'r', 'a' * 64, '{}', (Page('Original'),))
        with pytest.raises(ValueError, match='byte budget'):
            source.backup(tmp_path / 'too-small.sqlite', maximum_bytes=1)
        assert not (tmp_path / 'too-small.sqlite').exists()
        source.check()


def test_cancellable_snapshot_hash_reads_real_blocks_without_mutating_source(tmp_path):
    path = tmp_path / 'snapshot.sqlite'
    raw = b'Database fixture bytes.' * 150000
    path.write_bytes(raw)
    calls = []
    def stop():
        calls.append(True)
        return len(calls) >= 2
    with pytest.raises(InterruptedError):
        library_backup._snapshot_hash(path, stop)
    assert len(calls) == 2 and path.read_bytes() == raw


def test_explicit_profile_keeps_same_byte_competitor_as_no_clobber_conflict(tmp_path, monkeypatch):
    app, _, _ = populated(tmp_path / 'source')
    target = tmp_path / 'backup'
    actual = library_backup.os.link
    competed = []
    def competing(source, destination, *args, **kwargs):
        if Path(destination) == target / 'manifest.json':
            raw = Path(source).read_bytes()
            Path(destination).write_bytes(raw)
            competed.append(raw)
        return actual(source, destination, *args, **kwargs)
    with monkeypatch.context() as race:
        race.setattr(library_backup.os, 'link', competing)
        with pytest.raises(FileExistsError):
            app.backup(target, capacity_profile=LARGE)
    assert len(competed) == 1 and (target / 'manifest.json').read_bytes() == competed[0]
    # Existing byte validity never changes this invocation's conflict verdict.
    manifest = library_backup.verify_backup(target, sha256(competed[0]).hexdigest(),
                                           capacity_profile=LARGE)
    assert manifest['version'] == 4


def test_entry_cancel_and_total_budget_keep_no_published_directory(tmp_path):
    app, _, _ = populated(tmp_path / 'source')
    with pytest.raises(InterruptedError):
        app.backup(tmp_path / 'cancelled', capacity_profile=LARGE, cancelled=lambda: True)
    with pytest.raises(ValueError, match='byte budget'):
        app.backup(tmp_path / 'budget', capacity_profile=LARGE, max_bytes=1)
    assert not (tmp_path / 'cancelled').exists() and not (tmp_path / 'budget').exists()


@pytest.mark.parametrize('operation', ['snapshot', 'verify', 'restore'])
def test_cancel_during_actual_first_raw_hash_stops_before_next_large_page(tmp_path, monkeypatch, operation):
    first = 'First exact large source row. ' * 40000 + 'END1'
    second = 'Second exact large source row. ' * 40000 + 'END2'
    app = LibraryApp(tmp_path / 'source')
    workspace = app.create_workspace('Actual per-row validation cancellation')
    result = app.add_text(workspace, 'Two actual source rows', first + '\f' + second)
    assert result['state'] == 'complete'
    bundle = tmp_path / 'accepted-backup'
    expected = app.backup(bundle, capacity_profile=LARGE)
    actual = revisions.sha256
    hashed = []
    stopped = []
    def observed(data=b''):
        value = actual(data)  # The accepted first raw row really is hashed.
        if data == first.encode('utf-8'):
            hashed.append('first')
            stopped.append(True)
        elif data == second.encode('utf-8'):
            hashed.append('second')
        return value
    target = tmp_path / 'must-not-publish'
    with monkeypatch.context() as cancel:
        cancel.setattr(revisions, 'sha256', observed)
        with pytest.raises(InterruptedError):
            if operation == 'snapshot':
                with closing(Storage(app.database)) as storage:
                    storage.backup(target, cancelled=lambda: bool(stopped))
            elif operation == 'verify':
                library_backup.verify_backup(bundle, expected, capacity_profile=LARGE,
                                             cancelled=lambda: bool(stopped))
            else:
                library_backup.restore_backup(bundle, target, expected, capacity_profile=LARGE,
                                              cancelled=lambda: bool(stopped))
    assert hashed == ['first'] and stopped == [True]
    assert not target.exists()
    assert not list(tmp_path.glob('.carr-backup-*'))
    assert not list(tmp_path.glob('.carr-library-restore-*'))
    assert library_backup.verify_backup(bundle, expected, capacity_profile=LARGE)['version'] == 4
