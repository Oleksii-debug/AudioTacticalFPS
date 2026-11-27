"""Exact V4 -> V5 receipts, with real migration rollback and native process exit."""
from contextlib import closing
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys

import pytest

from carr_library.application import LibraryApp
from carr_library.storage import revisions
from carr_library.storage.revisions import Storage, _expected_signature, _schema_signature
from carr_library.storage.schema_identity import expected_schema_fingerprint


_helpers = runpy.run_path(str(Path(__file__).with_name('test_academic_migration.py')))
source_rows, v3_library = (_helpers[name] for name in ('source_rows', 'v3_library'))


def previous_library(root, version):
    path = v3_library(root)
    if version == 4:
        with closing(Storage(path, schema_version=4)):
            pass
    return path


def test_released_v4_schema_fingerprint_remains_exact():
    # Captured from ed633f3 before this V5 receipt implementation.
    assert expected_schema_fingerprint(4) == 'cd1006b2764a3c494aaced5ed9a0515ab3884eda7bed4f5e081d190e4474c409'


@pytest.mark.parametrize('version', (3, 4))
def test_app_migrates_exact_old_schema_without_manufacturing_receipts(tmp_path, version):
    path = previous_library(tmp_path / 'library', version)
    with closing(Storage(path, schema_version=version)) as old:
        before = source_rows(old.db)
    app = LibraryApp(path.parent)
    with app.read() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
        assert _schema_signature(db) == _expected_signature(5)
        assert source_rows(db) == before
        assert db.execute('SELECT count(*) FROM publication_receipt').fetchone()[0] == 0
    assert app.evidence('a', 'topic')[0].anchor.identity.revision == 'old'
    with closing(Storage(path, schema_version=5)) as reopened:
        reopened.check()
        assert source_rows(reopened.db) == before


@pytest.mark.parametrize('version', (3, 4))
@pytest.mark.parametrize('damage', ('extra_table', 'missing_trigger', 'damaged_evidence'))
def test_invalid_prior_schema_or_data_is_not_legitimized_by_migration(tmp_path, version, damage):
    path = previous_library(tmp_path / 'library', version)
    with closing(sqlite3.connect(path)) as db:
        if damage == 'extra_table':
            db.execute('CREATE TABLE unknown_extension(value TEXT)')
        elif damage == 'missing_trigger':
            db.execute('DROP TRIGGER revision_bu')
        else:
            db.execute('UPDATE research_evidence SET note=?', (b'Invalid binary note',))
        db.commit()
    before = path.read_bytes()
    with pytest.raises(ValueError):
        Storage(path, schema_version=5)
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == version
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='publication_receipt'").fetchone()


@pytest.mark.parametrize('version', (3, 4))
@pytest.mark.parametrize('failure', ('ddl', 'final_validation'))
def test_migration_failure_rolls_back_the_entire_chain(tmp_path, monkeypatch, version, failure):
    path = previous_library(tmp_path / 'library', version)
    before = path.read_bytes()
    if failure == 'ddl':
        monkeypatch.setattr(revisions, 'PUBLICATION_SCHEMA',
            revisions.PUBLICATION_SCHEMA + '\nSELECT absent_receipt_migration();\n')
        expected = sqlite3.OperationalError
    else:
        def reject(db):
            raise ValueError('Final receipt validation failed.')
        monkeypatch.setattr('carr_library.storage.publication_receipts.check_receipts', reject)
        expected = ValueError
    with pytest.raises(expected):
        Storage(path, schema_version=5)
    assert path.read_bytes() == before
    with closing(Storage(path, schema_version=version)) as old:
        old.check()
        assert _schema_signature(old.db) == _expected_signature(version)


@pytest.mark.parametrize('version', (3, 4))
@pytest.mark.parametrize('mode', ('DELETE', 'WAL'))
@pytest.mark.parametrize('committed', (False, True))
def test_native_exit_during_receipt_migration(tmp_path, version, mode, committed):
    path = previous_library(tmp_path / 'library', version)
    with closing(Storage(path, schema_version=version)) as old:
        assert old.db.execute('PRAGMA journal_mode=' + mode).fetchone()[0] == mode.lower()
        before = source_rows(old.db)
    source_root = Path(revisions.__file__).resolve().parents[2]
    script = '\n'.join([
        'import os,sqlite3,sys',
        'from carr_library.storage import revisions',
        'connect=sqlite3.connect',
        'class ExitConnection(sqlite3.Connection):',
        '    def commit(self):',
        "        if self.in_transaction and self.execute('PRAGMA user_version').fetchone()[0] == 5:",
        "            assert self.execute(\"SELECT 1 FROM sqlite_master WHERE name='publication_receipt'\").fetchone()",
        "            if sys.argv[2] == '1': super().commit()",
        '            os._exit(73)',
        '        return super().commit()',
        'def guarded_connect(*args, **kwargs):',
        "    kwargs['factory']=ExitConnection",
        '    return connect(*args, **kwargs)',
        'revisions.sqlite3.connect=guarded_connect',
        'revisions.Storage(sys.argv[1],schema_version=5)',
    ])
    env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(source_root), str(source_root.parent))))
    result = subprocess.run([sys.executable, '-B', '-c', script, str(path), str(int(committed))],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 73, (result.stdout, result.stderr)
    expected = 5 if committed else version
    with closing(Storage(path, schema_version=expected)) as recovered:
        recovered.check()
        assert _schema_signature(recovered.db) == _expected_signature(expected)
        assert source_rows(recovered.db) == before
    with closing(Storage(path, schema_version=5)) as upgraded:
        upgraded.check()
        assert source_rows(upgraded.db) == before
        assert upgraded.db.execute('SELECT count(*) FROM publication_receipt').fetchone()[0] == 0
