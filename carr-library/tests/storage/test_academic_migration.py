"""Real SQLite upgrade and backup checks preserve existing source evidence."""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog.membership import Catalog
from carr_library.catalog.registration import Registration, register_batch
from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.citations.historical import historical_loader
from carr_library.citations.text_attestation import CONFIG, strict_text_attestor
from carr_library.ingest.retained_assets import RetainedAssets
from carr_library.library_backup import restore_backup
from carr_library.research.academic_library import AcademicLibrary
from carr_library.research.saved_evidence import SavedEvidenceStore
from carr_library.research.topic_tree import TopicTree
from carr_library.storage import revisions
from carr_library.storage.revisions import (Page, Storage, CURRENT_SCHEMA_VERSION,
                                            _expected_signature, _schema_signature)
from carr_library.storage.schema_identity import expected_schema_fingerprint


RAW = 'Точна цитата. Original research source.'
DATA = RAW.encode('utf-8')
ASSET = sha256(DATA).hexdigest()


def test_previous_released_schema_fingerprints_are_unchanged():
    # Captured independently from canonical PR72, before the V4 implementation.
    assert expected_schema_fingerprint(1) == 'eceb346fe12f1a81c5312d5ed1e90c26c918cce3bc4847b467a94346c0e1a3d7'
    assert expected_schema_fingerprint(2) == 'fbcce1797998d80e338d585b40046df9adf822f803538c0f50359d2bb7265120'
    assert expected_schema_fingerprint(3) == 'dc848dde294833a50e4f71ec6ccd008a369b037f38714d92888e110f1aef9533'
    # Captured from the exact pre-receipt 3722c4ce storage implementation.
    assert expected_schema_fingerprint(4) == 'cd1006b2764a3c494aaced5ed9a0515ab3884eda7bed4f5e081d190e4474c409'


def v3_library(root):
    """Create a real previous-version library with a historical saved quotation."""
    root.mkdir()
    (root / 'originals').mkdir()
    RetainedAssets(root / 'originals').retain(DATA, 'text/plain', ASSET)
    path = root / 'library.sqlite'
    with closing(Storage(path, schema_version=3)) as storage:
        db = storage.db
        with storage.transaction():
            for workspace in ('a', 'b'):
                db.execute('INSERT INTO workspace VALUES(?)', (workspace,))
                db.execute('INSERT INTO workspace_meta VALUES(?,?,?,?)',
                           (workspace, 'Library ' + workspace, 'Research', int(workspace == 'b')))
            register_batch(Catalog(db, 'a'), (Registration('work', 'edition',
                {'sha256': ASSET, 'mime': 'text/plain', 'size': len(DATA)},
                'Original title', ('Original author',), 'uk', None, None, ()),))
            storage.stage_revision('a', 'edition', 'old', ASSET, CONFIG, [Page(RAW)])
            db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                       ('a', 'edition', 'old', 'Original author', 'Original title', None, None))
            TopicTree(db, 'a').create('topic', 'Source evidence')
            identity = Identity('a', 'edition', ASSET, 'old')
            metadata = Metadata('Original author', 'Original title')
            anchor = make_anchor(identity, Locator('logical_segment', 1), RAW, 0, 12,
                                 metadata, text_verified=True)
            attest = strict_text_attestor(db, 'a', lambda ident: DATA)
            store = SavedEvidenceStore(db, 'a', historical_loader(db, 'a', attest),
                lambda ident, meta: ident == identity and meta == metadata)
            store.save('topic', 'quote', anchor, 'Separate research note')
            storage.stage_revision('a', 'edition', 'current', ASSET, CONFIG, [Page(RAW)])
            db.execute('INSERT INTO source_metadata VALUES(?,?,?,?,?,?,?)',
                       ('a', 'edition', 'current', 'Original author', 'Original title', None, None))
        storage.check()
    return path


def source_rows(db):
    names = ('workspace', 'workspace_meta', 'catalog_asset', 'catalog_work',
             'catalog_author', 'catalog_edition', 'revision', 'page', 'sealed',
             'active', 'indexed', 'source_metadata', 'research_topic',
             'research_evidence', 'source_page_review')
    return {name: tuple(tuple(row) for row in db.execute(f'SELECT * FROM {name}'))
            for name in names}


def test_exact_v3_upgrade_is_idempotent_and_preserves_historical_sources(tmp_path):
    path = v3_library(tmp_path / 'data')
    with closing(Storage(path, schema_version=3)) as old:
        before = source_rows(old.db)
        assert _schema_signature(old.db) == _expected_signature(3)
    with closing(Storage(path, schema_version=4)) as upgraded:
        upgraded.check()
        assert upgraded.schema_version == 4
        assert upgraded.db.execute('PRAGMA user_version').fetchone()[0] == 4
        assert _schema_signature(upgraded.db) == _expected_signature(4)
        assert source_rows(upgraded.db) == before
        assert upgraded.db.execute('SELECT count(*) FROM academic_reference').fetchone()[0] == 0
    app = LibraryApp(path.parent)
    evidence, = app.evidence('a', 'topic')
    assert evidence.anchor.identity.revision == 'old'
    assert evidence.anchor.quote == RAW[:12]
    assert evidence.note == 'Separate research note'
    assert app.search('a', 'Original')[0].revision == 'current'
    with pytest.raises(ValueError, match='archived'):
        app.search('b', 'Original')
    with closing(Storage(path)) as reopened:
        assert source_rows(reopened.db) == before


@pytest.mark.parametrize('damage', ['layout', 'research_data'])
def test_invalid_v3_is_rejected_without_partial_upgrade(tmp_path, damage):
    path = v3_library(tmp_path / 'data')
    with closing(sqlite3.connect(path)) as db:
        if damage == 'layout':
            db.execute('CREATE TABLE unknown_extension(value TEXT)')
        else:
            db.execute("UPDATE research_evidence SET note=?", (b'Invalid binary note',))
        db.commit()
    before = path.read_bytes()
    with pytest.raises(ValueError):
        Storage(path, schema_version=4)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 3
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='academic_reference'").fetchone() is None


def test_interrupted_academic_ddl_rolls_back_tables_and_version(tmp_path, monkeypatch):
    path = v3_library(tmp_path / 'data')
    before = path.read_bytes()
    monkeypatch.setattr(revisions, 'ACADEMIC_SCHEMA',
        revisions.ACADEMIC_SCHEMA + '\nSELECT no_such_migration_function();\n')
    with pytest.raises(sqlite3.OperationalError, match='no such function'):
        Storage(path, schema_version=4)
    assert path.read_bytes() == before
    with closing(Storage(path, schema_version=3)) as old:
        old.check()
        assert old.db.execute("SELECT 1 FROM sqlite_master WHERE name='academic_reference'").fetchone() is None


@pytest.mark.parametrize('version', [1, 2])
def test_older_storage_has_no_implicit_guessed_upgrade(tmp_path, version):
    path = tmp_path / 'old.sqlite'
    with closing(Storage(path, schema_version=version)):
        pass
    before = path.read_bytes()
    with pytest.raises(ValueError, match='schema version'):
        Storage(path, schema_version=4)
    assert path.read_bytes() == before


@pytest.mark.parametrize('manifest_version', [1, 2])
@pytest.mark.parametrize('old_version', [3, 4, 5])
def test_previous_backups_restore_unchanged_then_migrate_normally(tmp_path, manifest_version, old_version):
    path = v3_library(tmp_path / 'source')
    backup = tmp_path / 'legacy-backup'
    backup.mkdir()
    (backup / 'originals').mkdir()
    with closing(Storage(path, schema_version=old_version)) as storage:
        storage.backup(backup / 'library.sqlite')
    filename = ASSET + '.txt'
    shutil.copyfile(path.parent / 'originals' / filename, backup / 'originals' / filename)
    snapshot = backup / 'library.sqlite'
    manifest = dict(version=manifest_version, kind='carr-library', schema_version=old_version,
        database=dict(filename='library.sqlite', size_bytes=snapshot.stat().st_size,
                      sha256=sha256(snapshot.read_bytes()).hexdigest()),
        assets=[dict(filename=filename, sha256=ASSET, mime='text/plain', size_bytes=len(DATA))])
    if manifest_version == 2:
        manifest['schema_fingerprint'] = expected_schema_fingerprint(old_version)
    raw = (json.dumps(manifest, sort_keys=True) + '\n').encode('utf-8')
    (backup / 'manifest.json').write_bytes(raw)
    before = snapshot.read_bytes()
    target = restore_backup(backup, tmp_path / 'restored', sha256(raw).hexdigest())
    assert snapshot.read_bytes() == before
    with closing(sqlite3.connect(target / 'library.sqlite')) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == old_version
    app = LibraryApp(target)
    evidence, = app.evidence('a', 'topic')
    assert evidence.anchor.quote == RAW[:12]
    assert evidence.anchor.identity.revision == 'old'
    assert evidence.note == 'Separate research note'
    assert (target / 'originals' / filename).read_bytes() == DATA
    assert snapshot.read_bytes() == before
    with app.read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == CURRENT_SCHEMA_VERSION


def test_v4_backup_restores_reading_data_and_archives_without_changing_quotes(tmp_path):
    path = v3_library(tmp_path / 'data')
    app = LibraryApp(path.parent)
    original_evidence, = app.evidence('a', 'topic')
    with app.read() as db:
        original_sources = source_rows(db)
    with app.write('a') as db:
        library = AcademicLibrary(db, 'a')
        reference = library.create(title='Research article', authors=('New author',),
            item_type='article', year='2026', container_title='Journal', volume='12',
            issue='3', pages='45–51', doi='10.5555/example', edition_id='edition',
            notes='Independent reading notes', tags=('history', 'Україна'),
            reading_status='reading', progress=35, priority='high', rating=4)
        archived = library.create(title='Archived reference without an attachment')
        archived = library.archive(archived.id, expected=archived)
    app.restore_workspace('b')
    with app.write('b') as db:
        separate = AcademicLibrary(db, 'b').create(title='Other library',
            doi='10.5555/example', notes='Private independent notes',
            reading_status='read', progress=100, rating=5)
    app.archive_workspace('b')
    directory = tmp_path / 'backup'
    digest = app.backup(directory)
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert manifest['schema_version'] == CURRENT_SCHEMA_VERSION
    assert manifest['schema_fingerprint'] == expected_schema_fingerprint(CURRENT_SCHEMA_VERSION)
    restored = LibraryApp(restore_backup(directory, tmp_path / 'restored', digest))
    assert restored.evidence('a', 'topic') == (original_evidence,)
    with restored.read('a') as db:
        library = AcademicLibrary(db, 'a')
        assert library.get(reference.id) == reference
        assert library.get(archived.id) == archived
        assert len(library.list()) == 1
        assert len(library.list(include_archived=True)) == 2
        with pytest.raises(ValueError, match='unavailable'):
            library.get(separate.id)
        assert source_rows(db) == original_sources
    with pytest.raises(ValueError, match='archived'):
        with restored.read('b'):
            pass
    restored.restore_workspace('b')
    with restored.read('b') as db:
        assert AcademicLibrary(db, 'b').get(separate.id) == separate
    assert (restored.assets_dir / (ASSET + '.txt')).read_bytes() == DATA


@pytest.mark.parametrize(('field', 'value'), [
    ('authors', 'not-json'),
    ('tags', '["valid", 2]'),
    ('search_text', 'forged search text'),
    ('edition_id', 'edition'),
])
def test_v4_backup_rejects_corrupt_academic_data_in_archived_workspace(tmp_path, field, value):
    path = v3_library(tmp_path / 'data')
    app = LibraryApp(path.parent)
    app.restore_workspace('b')
    with app.write('b') as db:
        reference = AcademicLibrary(db, 'b').create(title='Archived library reference')
    app.archive_workspace('b')
    # Simulate corruption outside the application. The edition exists only in A.
    assert field in ('authors', 'tags', 'search_text', 'edition_id')
    with closing(sqlite3.connect(app.database)) as db:
        db.execute(f'UPDATE academic_reference SET {field}=? WHERE id=?', (value, reference.id))
        db.commit()
    before = app.database.read_bytes()
    destination = tmp_path / 'rejected-backup'
    with pytest.raises(ValueError):
        app.backup(destination)
    assert not destination.exists()
    assert app.database.read_bytes() == before


# Carry forward the CARR-05 process-exit regression from b26a800e into
# this already mandatory CI suite, using the same populated V3 fixture.
@pytest.mark.parametrize("mode", ["DELETE", "WAL"])
@pytest.mark.parametrize("committed", [False, True])
def test_process_exit_during_v4_migration(tmp_path, mode, committed):
    path = v3_library(tmp_path / "data")
    with closing(Storage(path, schema_version=3)) as old:
        assert old.db.execute("PRAGMA journal_mode=" + mode).fetchone()[0] == mode.lower()
        before = source_rows(old.db)

    # Launch the exact candidate Storage in another process; explicitly expose
    # the same source root as the parent even when pytest uses its own sys.path.
    env = os.environ.copy()
    source_root = str(Path(revisions.__file__).resolve().parents[2])
    # The exact source checkout also owns the bootstrap package used by the
    # application imports in this test module; never fall back to an old wheel.
    project_root = str(Path(source_root).parent)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, project_root, env.get("PYTHONPATH"))))
    result = subprocess.run(
        [sys.executable, '-B', str(Path(__file__).resolve()), str(path), str(int(committed))],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 73, (result.returncode, result.stdout, result.stderr)

    expected = 4 if committed else 3
    with closing(Storage(path, schema_version=expected)) as recovered:
        assert recovered.schema_version == expected
        recovered.check()
        assert _schema_signature(recovered.db) == _expected_signature(expected)
        assert source_rows(recovered.db) == before
        assert recovered.get_page("a", "old", 1)["raw"] == RAW
        assert recovered.get_page("b", "old", 1) is None
        assert recovered.db.execute("SELECT revision FROM active_page WHERE workspace='a'").fetchone()[0] == "current"
        assert bool(recovered.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='academic_reference'"
        ).fetchone()) == committed

    # Retry after an aborted upgrade, or reopen after a committed one.
    target = tmp_path / "snapshot.sqlite"
    with closing(Storage(path, schema_version=4)) as upgraded:
        upgraded.check()
        assert source_rows(upgraded.db) == before
        assert upgraded.db.execute("SELECT count(*) FROM academic_reference").fetchone()[0] == 0
        upgraded.backup(target)
    assert target.read_bytes()[18:20] == b"\x01\x01"
    assert not any(Path(str(target) + suffix).exists() for suffix in ("-wal", "-shm", "-journal"))
    with closing(Storage(target, schema_version=4)) as restored:
        restored.check()
        assert source_rows(restored.db) == before
        assert restored.get_page("a", "old", 1)["raw"] == RAW


@pytest.mark.parametrize('old_version', (3, 4))
@pytest.mark.parametrize('damage', ('layout', 'research_data'))
def test_invalid_previous_schema_is_not_upgraded_for_publication_receipts(tmp_path, old_version, damage):
    path = v3_library(tmp_path / 'data')
    with closing(Storage(path, schema_version=old_version)):
        pass
    with sqlite3.connect(path) as db:
        if damage == 'layout':
            db.execute('CREATE TABLE unknown_extension(value TEXT)')
        else:
            db.execute('UPDATE research_evidence SET note=?', (b'Invalid binary note',))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        Storage(path, schema_version=5)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == old_version
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='import_publication'").fetchone() is None


@pytest.mark.parametrize('old_version', (3, 4))
@pytest.mark.parametrize('mode', ('DELETE', 'WAL'))
@pytest.mark.parametrize('committed', (False, True))
def test_process_exit_during_import_receipt_migration_preserves_exact_old_data(
        tmp_path, old_version, mode, committed):
    path = v3_library(tmp_path / 'data')
    with closing(Storage(path, schema_version=old_version)) as old:
        assert old.db.execute('PRAGMA journal_mode=' + mode).fetchone()[0] == mode.lower()
        before = source_rows(old.db)
        if old_version == 4:
            with old.transaction():
                AcademicLibrary(old.db, 'a').create(title='An existing reading record',
                    notes='Preserve this independent note.')
            academic = tuple(tuple(row) for row in old.db.execute('SELECT * FROM academic_reference'))
        else:
            academic = ()
    source_root = str(Path(revisions.__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((source_root, str(Path(source_root).parent))))
    result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), str(path),
                             str(int(committed)), '5'],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, (result.returncode, result.stdout, result.stderr)
    expected = 5 if committed else old_version
    with closing(Storage(path, schema_version=expected)) as recovered:
        recovered.check()
        assert source_rows(recovered.db) == before
        assert recovered.schema_version == expected
        assert _schema_signature(recovered.db) == _expected_signature(expected)
        assert bool(recovered.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='import_publication'").fetchone()) == committed
    # Retry is safe after rollback; opening an already committed upgrade is
    # idempotent. Historical sources never gain fabricated import ownership.
    with closing(Storage(path, schema_version=5)) as current:
        current.check()
        assert source_rows(current.db) == before
        assert tuple(tuple(row) for row in current.db.execute('SELECT * FROM academic_reference')) == academic
        assert current.db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0



@pytest.mark.parametrize('old_version', (3, 4, 5))
def test_registration_upgrade_preserves_sources_and_existing_publication_receipts(tmp_path, old_version):
    path = v3_library(tmp_path / 'data')
    receipt = ('current', 'a' * 32, 'b' * 32, 'c' * 64)
    with closing(Storage(path, schema_version=old_version)) as old:
        before = source_rows(old.db)
        if old_version >= 4:
            with old.transaction():
                AcademicLibrary(old.db, 'a').create(title='Existing reading record',
                    notes='Preserved private note.')
        if old_version == 5:
            with old.transaction():
                old.db.execute('INSERT INTO import_publication VALUES(?,?,?,?)', receipt)
        academic = (tuple(tuple(row) for row in old.db.execute('SELECT * FROM academic_reference'))
                    if old_version >= 4 else ())
    with closing(Storage(path, schema_version=6)) as upgraded:
        upgraded.check()
        assert source_rows(upgraded.db) == before
        assert _schema_signature(upgraded.db) == _expected_signature(6)
        assert tuple(tuple(row) for row in upgraded.db.execute('SELECT * FROM academic_reference')) == academic
        assert tuple(tuple(row) for row in upgraded.db.execute('SELECT * FROM import_publication')) == (
            (receipt,) if old_version == 5 else ())
        assert upgraded.db.execute('SELECT count(*) FROM import_registration').fetchone()[0] == 0
    with closing(Storage(path, schema_version=6)) as reopened:
        reopened.check()
        assert source_rows(reopened.db) == before


@pytest.mark.parametrize('damage', ('layout', 'research_data'))
def test_invalid_v5_is_not_upgraded_to_registration_receipts(tmp_path, damage):
    path = v3_library(tmp_path / 'data')
    with closing(Storage(path, schema_version=5)):
        pass
    with sqlite3.connect(path) as db:
        if damage == 'layout':
            db.execute('CREATE TABLE unknown_extension(value TEXT)')
        else:
            db.execute('UPDATE research_evidence SET note=?', (b'Invalid binary note',))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        Storage(path, schema_version=6)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='import_registration'").fetchone() is None


@pytest.mark.parametrize('mode', ('DELETE', 'WAL'))
@pytest.mark.parametrize('committed', (False, True))
def test_process_exit_during_registration_upgrade_preserves_v5_receipts(
        tmp_path, mode, committed):
    path = v3_library(tmp_path / 'data')
    receipt = ('current', 'a' * 32, 'b' * 32, 'c' * 64)
    with closing(Storage(path, schema_version=5)) as old:
        assert old.db.execute('PRAGMA journal_mode=' + mode).fetchone()[0] == mode.lower()
        with old.transaction():
            old.db.execute('INSERT INTO import_publication VALUES(?,?,?,?)', receipt)
        before = source_rows(old.db)
    source_root = str(Path(revisions.__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((source_root, str(Path(source_root).parent))))
    result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()), str(path),
                             str(int(committed)), '6'],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, (result.returncode, result.stdout, result.stderr)
    expected = 6 if committed else 5
    with closing(Storage(path, schema_version=expected)) as recovered:
        recovered.check()
        assert source_rows(recovered.db) == before
        assert _schema_signature(recovered.db) == _expected_signature(expected)
        assert tuple(recovered.db.execute('SELECT * FROM import_publication').fetchone()) == receipt
        assert bool(recovered.db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='import_registration'").fetchone()) == committed
    with closing(Storage(path, schema_version=6)) as current:
        current.check()
        assert source_rows(current.db) == before
        assert tuple(current.db.execute('SELECT * FROM import_publication').fetchone()) == receipt
        assert current.db.execute('SELECT count(*) FROM import_registration').fetchone()[0] == 0


if __name__ == "__main__":
    path, committed, *target = sys.argv[1:]
    target_version = int(target[0]) if target else 4
    connect = sqlite3.connect

    class CrashConnection(sqlite3.Connection):
        def commit(self):
            if self.in_transaction and self.execute("PRAGMA user_version").fetchone()[0] == target_version:
                # Both DDL and version must already be visible in the transaction.
                assert self.execute("SELECT 1 FROM sqlite_master WHERE name='academic_reference'").fetchone()
                if target_version >= 5:
                    assert self.execute("SELECT 1 FROM sqlite_master WHERE name='import_publication'").fetchone()
                if target_version >= 6:
                    assert self.execute("SELECT 1 FROM sqlite_master WHERE name='import_registration'").fetchone()
                if committed == "1":
                    super().commit()
                os._exit(73)
            return super().commit()

    def crash_connect(*args, **kwargs):
        kwargs["factory"] = CrashConnection
        return connect(*args, **kwargs)

    revisions.sqlite3.connect = crash_connect
    with closing(Storage(path, schema_version=target_version)):
        raise AssertionError("migration did not reach crash checkpoint")
