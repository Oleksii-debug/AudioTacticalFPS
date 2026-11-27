"""Real SQLite V5-to-V6 upgrades preserve publications and register later attempts."""
from collections import Counter
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from carr_library.research.academic_library import AcademicLibrary
from carr_library.storage import revisions
from carr_library.storage.revisions import (
    CURRENT_SCHEMA_VERSION, Storage, _expected_signature, _schema_signature,
)
from carr_library.storage.schema_identity import expected_schema_fingerprint

# The existing populated historical-source fixture is also used by the released
# V3/V4/V5 migration regressions. No second catalog assembler is introduced.
from importlib.util import module_from_spec, spec_from_file_location

_fixture_spec = spec_from_file_location(
    '_carr_academic_migration_fixture', Path(__file__).with_name('test_academic_migration.py'))
_fixture_module = module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
v3_library = _fixture_module.v3_library


_PUBLICATIONS = (
    ('old', 'a' * 32, 'b' * 32, 'c' * 64),
    ('current', 'd' * 32, 'e' * 32, 'f' * 64),
)
_INSERT = ('INSERT INTO import_registration'
           '(revision,request_id,attempt,sha256,changed) VALUES(?,?,?,?,?)')
_REGISTRATION = ('current', '1' * 32, '2' * 32, '3' * 64, 1)


def _rows(db, names=None):
    """Compare every old table, including FTS shadow rows, without row order."""
    if names is None:
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    result = {}
    for name in names:
        quoted = '"' + name.replace('"', '""') + '"'
        result[name] = Counter(tuple(row) for row in db.execute('SELECT * FROM ' + quoted))
    return result


def _populated(root, version=5):
    path = v3_library(root)
    with closing(Storage(path, schema_version=version)) as old:
        with old.transaction():
            old.db.execute("INSERT INTO catalog_collection(workspace,id,name) VALUES"
                           "('a','preserved','Preserved collection')")
            old.db.execute("INSERT INTO catalog_membership VALUES('a','preserved','edition')")
            if version >= 4:
                AcademicLibrary(old.db, 'a').create(
                    title='Historical reading record', notes='Keep this independent note.',
                    tags=('migration', 'Україна'), reading_status='reading', progress=35)
            if version >= 5:
                old.db.executemany('INSERT INTO import_publication VALUES(?,?,?,?)', _PUBLICATIONS)
        old.check()
    return path


def _has_registration(db):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='import_registration'").fetchone() is not None


def test_all_released_schema_fingerprints_including_v5_are_unchanged():
    expected = {
        1: 'eceb346fe12f1a81c5312d5ed1e90c26c918cce3bc4847b467a94346c0e1a3d7',
        2: 'fbcce1797998d80e338d585b40046df9adf822f803538c0f50359d2bb7265120',
        3: 'dc848dde294833a50e4f71ec6ccd008a369b037f38714d92888e110f1aef9533',
        4: 'cd1006b2764a3c494aaced5ed9a0515ab3884eda7bed4f5e081d190e4474c409',
        5: 'c445a077a95ba6e52384646ea91854dcea4522dab6a14388b654bfbf82311220',
    }
    assert CURRENT_SCHEMA_VERSION == 6
    for version, fingerprint in expected.items():
        assert expected_schema_fingerprint(version) == fingerprint


@pytest.mark.parametrize('old_version', (3, 4, 5))
def test_checked_upgrade_preserves_every_old_row_and_has_no_invented_registration(
        tmp_path, old_version):
    path = _populated(tmp_path / 'data', old_version)
    with closing(Storage(path, schema_version=old_version)) as old:
        before = _rows(old.db)
    with closing(Storage(path, schema_version=6)) as upgraded:
        upgraded.check()
        assert upgraded.schema_version == 6
        assert _schema_signature(upgraded.db) == _expected_signature(6)
        assert _rows(upgraded.db, before) == before
        assert upgraded.db.execute('SELECT count(*) FROM import_registration').fetchone()[0] == 0
        publications = tuple(tuple(row) for row in upgraded.db.execute(
            'SELECT * FROM import_publication ORDER BY revision'))
        expected_publications = tuple(sorted(_PUBLICATIONS)) if old_version == 5 else ()
        assert publications == expected_publications
        complete = _rows(upgraded.db)
    with closing(Storage(path, schema_version=6)) as reopened:
        reopened.check()
        assert _rows(reopened.db) == complete
        assert _schema_signature(reopened.db) == _expected_signature(6)


def test_v5_open_without_requested_upgrade_preserves_the_released_version(tmp_path):
    path = _populated(tmp_path / 'data')
    with closing(Storage(path)) as old:
        assert old.schema_version == 5
        assert not _has_registration(old.db)
        assert tuple(tuple(row) for row in old.db.execute(
            'SELECT * FROM import_publication ORDER BY revision')) == tuple(sorted(_PUBLICATIONS))


@pytest.mark.parametrize('old_version', (3, 4, 5))
@pytest.mark.parametrize('damage', ('layout', 'research_data'))
def test_invalid_old_catalog_is_rejected_before_upgrade_and_preserved(
        tmp_path, old_version, damage):
    path = _populated(tmp_path / 'data', old_version)
    with sqlite3.connect(path) as db:
        if damage == 'layout':
            db.execute('CREATE TABLE unknown_extension(value TEXT)')
        else:
            db.execute('UPDATE research_evidence SET note=?', (b'Invalid binary note',))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        Storage(path, schema_version=6)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == old_version
        assert not _has_registration(db)


@pytest.mark.parametrize('damage', ('fingerprint', 'missing_revision'))
def test_corrupted_v5_publication_data_is_not_legitimized_by_upgrade(tmp_path, damage):
    path = _populated(tmp_path / 'data')
    with sqlite3.connect(path) as db:
        if damage == 'fingerprint':
            trigger, = db.execute("SELECT sql FROM sqlite_master WHERE "
                                  "name='import_publication_no_update'").fetchone()
            db.execute('PRAGMA ignore_check_constraints=ON')
            db.execute('DROP TRIGGER import_publication_no_update')
            db.execute("UPDATE import_publication SET sha256=? WHERE revision='old'", ('g' * 64,))
            db.execute(trigger)
        else:
            # Simulate an external writer that did not enable foreign keys.
            db.execute('INSERT INTO import_publication VALUES(?,?,?,?)',
                       ('missing', '4' * 32, '5' * 32, '6' * 64))
    before = path.read_bytes()
    with pytest.raises(ValueError, match='integrity'):
        Storage(path, schema_version=6)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
        assert not _has_registration(db)


@pytest.mark.parametrize(('column', 'value'), (
    ('request_id', 'a' * 32 + '\x00suffix'),
    ('attempt', 'b' * 32 + '\x00'),
    ('sha256', 'c' * 64 + '\x00suffix'),
    ('request_id', b'a' * 32),
    ('attempt', b'b' * 32),
    ('sha256', b'c' * 64),
))
def test_full_v5_receipt_strings_are_checked_without_changing_frozen_ddl(
        tmp_path, column, value):
    # V4 has no publication receipts; its legitimate V5 migration adds an
    # empty receipt table. Insert a malformed row accepted by frozen V5 SQL.
    path = _populated(tmp_path / 'data', 4)
    with closing(Storage(path, schema_version=5)) as old:
        fields = dict(zip(('revision', 'request_id', 'attempt', 'sha256'), _PUBLICATIONS[0]))
        fields[column] = value
        old.db.execute('INSERT INTO import_publication VALUES(?,?,?,?)', tuple(fields.values()))
        assert _schema_signature(old.db) == _expected_signature(5)
    before = path.read_bytes()
    with pytest.raises(ValueError, match='publication or registration receipt'):
        Storage(path, schema_version=6)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
        assert not _has_registration(db)
        assert tuple(db.execute('SELECT * FROM import_publication').fetchone()) == tuple(fields.values())


@pytest.mark.parametrize('old_version', (3, 4, 5))
def test_failure_after_new_ddl_rolls_back_the_entire_migration_chain(
        tmp_path, monkeypatch, old_version):
    path = _populated(tmp_path / 'data', old_version)
    with closing(Storage(path, schema_version=old_version)) as old:
        rows = _rows(old.db)
    before = path.read_bytes()
    monkeypatch.setattr(revisions, 'IMPORT_REGISTRATION_SCHEMA',
        revisions.IMPORT_REGISTRATION_SCHEMA + '\nSELECT no_such_registration_function();\n')
    with pytest.raises(sqlite3.OperationalError, match='no such function'):
        Storage(path, schema_version=6)
    assert path.read_bytes() == before
    with closing(Storage(path, schema_version=old_version)) as old:
        old.check()
        assert not _has_registration(old.db)
        assert _rows(old.db) == rows
        assert _schema_signature(old.db) == _expected_signature(old_version)


@pytest.mark.parametrize(('column', 'value'), [
    ('revision', None), ('revision', 'unsealed-or-missing'),
    ('request_id', None), ('request_id', 'a' * 31), ('request_id', 'a' * 33),
    ('request_id', 'A' * 32), ('request_id', 'g' * 32), ('request_id', b'a' * 32),
    ('request_id', 'a' * 31 + '\x00'), ('request_id', 'a' * 32 + '\x00suffix'),
    ('attempt', None), ('attempt', 'b' * 31), ('attempt', 'B' * 32),
    ('attempt', 'g' * 32), ('attempt', b'b' * 32), ('attempt', 'b' * 32 + '\x00'),
    ('sha256', None), ('sha256', 'c' * 63), ('sha256', 'c' * 65),
    ('sha256', 'C' * 64), ('sha256', 'g' * 64), ('sha256', b'c' * 64),
    ('sha256', 'c' * 64 + '\x00suffix'),
    ('changed', None), ('changed', -1), ('changed', 2), ('changed', 0.5),
    ('changed', 'invalid'), ('changed', b'1'),
])
def test_registration_constraints_reject_invalid_receipts(tmp_path, column, value):
    path = _populated(tmp_path / 'data')
    with closing(Storage(path, schema_version=6)) as storage:
        values = dict(zip(('revision', 'request_id', 'attempt', 'sha256', 'changed'), _REGISTRATION))
        values[column] = value
        with pytest.raises(sqlite3.IntegrityError):
            storage.db.execute(_INSERT, tuple(values.values()))
        assert storage.db.execute('SELECT count(*) FROM import_registration').fetchone()[0] == 0
        storage.check()


@pytest.mark.parametrize(('column', 'value'), (
    ('request_id', '1' * 32 + '\x00suffix'),
    ('attempt', b'2' * 32),
    ('sha256', '3' * 64 + '\x00suffix'),
    ('changed', b'1'),
    ('changed', 0.5),
    ('changed', 2),
))
def test_registration_runtime_check_rejects_rows_from_an_external_unchecked_writer(
        tmp_path, column, value):
    path = _populated(tmp_path / 'data')
    with closing(Storage(path, schema_version=6)) as storage:
        fields = dict(zip(('revision', 'request_id', 'attempt', 'sha256', 'changed'), _REGISTRATION))
        fields[column] = value
        storage.db.execute('PRAGMA ignore_check_constraints=ON')
        storage.db.execute(_INSERT, tuple(fields.values()))
        # No pragma changes or guessed normalization may legitimize the row.
        with pytest.raises(ValueError):
            storage.check()


def test_separate_requests_and_attempts_can_register_the_same_sealed_revision(tmp_path):
    path = _populated(tmp_path / 'data')
    with closing(Storage(path, schema_version=6)) as storage:
        rows = (
            _REGISTRATION,
            ('current', '1' * 32, '4' * 32, '5' * 64, 0),
            ('current', '6' * 32, '2' * 32, '7' * 64, 1),
            ('old', '8' * 32, '9' * 32, 'a' * 64, 0),
        )
        with storage.transaction():
            storage.db.executemany(_INSERT, rows)
        assert Counter(tuple(row) for row in storage.db.execute(
            'SELECT * FROM import_registration')) == Counter(rows)
        assert tuple(tuple(row) for row in storage.db.execute(
            'SELECT * FROM import_publication ORDER BY revision')) == tuple(sorted(_PUBLICATIONS))
        storage.check()


@pytest.mark.parametrize('statement', (
    "UPDATE import_registration SET changed=0",
    "DELETE FROM import_registration",
    "INSERT OR REPLACE INTO import_registration VALUES('old',?,?,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',0)",
    "REPLACE INTO import_registration VALUES('old',?,?,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',0)",
))
def test_registration_cannot_be_mutated_deleted_or_replaced(tmp_path, statement):
    path = _populated(tmp_path / 'data')
    with closing(Storage(path, schema_version=6)) as storage:
        storage.db.execute(_INSERT, _REGISTRATION)
        parameters = _REGISTRATION[1:3] if '?' in statement else ()
        with pytest.raises(sqlite3.IntegrityError):
            storage.db.execute(statement, parameters)
        assert tuple(tuple(row) for row in storage.db.execute(
            'SELECT * FROM import_registration')) == (_REGISTRATION,)
        storage.check()


@pytest.mark.parametrize('statement', (
    "UPDATE import_publication SET sha256='bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'",
    "DELETE FROM import_publication",
    "INSERT OR REPLACE INTO import_publication VALUES('old',?,?,?)",
))
def test_original_publication_receipts_remain_immutable_after_upgrade(tmp_path, statement):
    path = _populated(tmp_path / 'data')
    with closing(Storage(path, schema_version=6)) as storage:
        parameters = ('1' * 32, '2' * 32, '3' * 64) if '?' in statement else ()
        with pytest.raises(sqlite3.IntegrityError):
            storage.db.execute(statement, parameters)
        assert tuple(tuple(row) for row in storage.db.execute(
            'SELECT * FROM import_publication ORDER BY revision')) == tuple(sorted(_PUBLICATIONS))
        storage.check()


@pytest.mark.parametrize('mode', ('DELETE', 'WAL'))
@pytest.mark.parametrize('checkpoint', ('during_ddl', 'before_commit', 'after_commit'))
def test_process_exit_during_v5_registration_migration_has_atomic_recovery(
        tmp_path, mode, checkpoint):
    path = _populated(tmp_path / 'data')
    with closing(Storage(path, schema_version=5)) as old:
        assert old.db.execute('PRAGMA journal_mode=' + mode).fetchone()[0] == mode.lower()
        before = _rows(old.db)
    source_root = str(Path(revisions.__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(
        (source_root, str(Path(source_root).parent))))
    result = subprocess.run(
        [sys.executable, '-B', str(Path(__file__).resolve()), str(path), checkpoint],
        env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, (result.returncode, result.stdout, result.stderr)
    committed = checkpoint == 'after_commit'
    expected = 6 if committed else 5
    with closing(Storage(path, schema_version=expected)) as recovered:
        recovered.check()
        assert recovered.schema_version == expected
        assert _schema_signature(recovered.db) == _expected_signature(expected)
        assert _has_registration(recovered.db) is committed
        assert _rows(recovered.db, before) == before
    # Retry after recovery is safe, and an already committed migration is
    # idempotent. None of the historical records gains an invented registration.
    with closing(Storage(path, schema_version=6)) as current:
        current.check()
        assert _rows(current.db, before) == before
        assert current.db.execute('SELECT count(*) FROM import_registration').fetchone()[0] == 0
        target = tmp_path / 'snapshot.sqlite'
        current.backup(target)
    with closing(Storage(target, schema_version=6)) as restored:
        restored.check()
        assert _rows(restored.db, before) == before
        assert restored.db.execute('SELECT count(*) FROM import_registration').fetchone()[0] == 0


if __name__ == '__main__':
    path, checkpoint = sys.argv[1:]
    connect = sqlite3.connect

    class CrashConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            result = super().execute(sql, parameters)
            if checkpoint == 'during_ddl' and sql.lstrip().startswith('CREATE TABLE import_registration('):
                assert self.in_transaction
                assert super().execute('PRAGMA user_version').fetchone()[0] == 5
                os._exit(73)
            return result

        def commit(self):
            if self.in_transaction and self.execute('PRAGMA user_version').fetchone()[0] == 6:
                assert self.execute('SELECT count(*) FROM import_registration').fetchone()[0] == 0
                assert self.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 2
                if checkpoint == 'after_commit':
                    super().commit()
                elif checkpoint != 'before_commit':
                    raise AssertionError('unknown migration checkpoint')
                os._exit(73)
            return super().commit()

    def crash_connect(*args, **kwargs):
        kwargs['factory'] = CrashConnection
        return connect(*args, **kwargs)

    revisions.sqlite3.connect = crash_connect
    with closing(Storage(path, schema_version=6)):
        raise AssertionError('migration did not reach its process-exit checkpoint')
