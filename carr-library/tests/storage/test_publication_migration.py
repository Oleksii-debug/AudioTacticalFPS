"""Exact released V5 identity, real migration rollback and native process exit."""
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
from carr_library.storage.schema_identity import expected_schema_fingerprint, schema_fingerprint


# Literal accepted DDL from canonical c9cdacd; independent of the production
# constant being tested. The previous draft below is deliberately incompatible.
_CANONICAL_IMPORT_PUBLICATION_SCHEMA = '''
CREATE TABLE import_publication(
 revision TEXT PRIMARY KEY NOT NULL REFERENCES sealed(revision),
 request_id TEXT NOT NULL CHECK(length(request_id)=32 AND request_id NOT GLOB '*[^0-9a-f]*'),
 attempt TEXT NOT NULL CHECK(length(attempt)=32 AND attempt NOT GLOB '*[^0-9a-f]*'),
 sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
 UNIQUE(request_id,attempt)
);
CREATE TRIGGER import_publication_no_update BEFORE UPDATE ON import_publication
BEGIN SELECT RAISE(ABORT,'immutable import publication'); END;
CREATE TRIGGER import_publication_no_delete BEFORE DELETE ON import_publication
BEGIN SELECT RAISE(ABORT,'immutable import publication'); END;
CREATE TRIGGER import_publication_no_replace BEFORE INSERT ON import_publication
WHEN EXISTS(SELECT 1 FROM import_publication WHERE revision=NEW.revision)
BEGIN SELECT RAISE(ABORT,'import publication already exists'); END;
'''
_OLD_DRAFT_PUBLICATION_SCHEMA = '''
CREATE TABLE publication_receipt(
 request_id TEXT NOT NULL CHECK(length(request_id)=32 AND request_id NOT GLOB '*[^0-9a-f]*'),
 attempt_id TEXT NOT NULL CHECK(length(attempt_id)=32 AND attempt_id NOT GLOB '*[^0-9a-f]*'),
 workspace TEXT NOT NULL, edition TEXT NOT NULL, revision TEXT NOT NULL UNIQUE,
 asset TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
 PRIMARY KEY(request_id,attempt_id),
 FOREIGN KEY(workspace,edition,asset) REFERENCES catalog_edition(workspace,id,asset),
 FOREIGN KEY(workspace,edition,revision) REFERENCES revision(workspace,edition,id)
   DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(revision) REFERENCES sealed(revision) DEFERRABLE INITIALLY DEFERRED
);
CREATE TRIGGER publication_receipt_bi BEFORE INSERT ON publication_receipt
 WHEN EXISTS(SELECT 1 FROM sealed WHERE revision=new.revision)
 BEGIN SELECT RAISE(ABORT,'receipt requires a new publication'); END;
CREATE TRIGGER publication_receipt_bu BEFORE UPDATE ON publication_receipt
 BEGIN SELECT RAISE(ABORT,'immutable publication receipt'); END;
CREATE TRIGGER publication_receipt_bd BEFORE DELETE ON publication_receipt
 BEGIN SELECT RAISE(ABORT,'immutable publication receipt'); END;
'''
_CANONICAL_V5_FINGERPRINT = 'c445a077a95ba6e52384646ea91854dcea4522dab6a14388b654bfbf82311220'


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


def test_canonical_v5_schema_signature_and_fingerprint_remain_exact():
    assert revisions.IMPORT_PUBLICATION_SCHEMA == _CANONICAL_IMPORT_PUBLICATION_SCHEMA
    assert expected_schema_fingerprint(5) == _CANONICAL_V5_FINGERPRINT
    with closing(sqlite3.connect(':memory:')) as reference:
        reference.executescript(revisions.SCHEMA_V4 + _CANONICAL_IMPORT_PUBLICATION_SCHEMA
                                + '\nPRAGMA user_version=5;\n')
        assert _schema_signature(reference) == _expected_signature(5)
        assert schema_fingerprint(reference) == _CANONICAL_V5_FINGERPRINT


@pytest.mark.parametrize('opener', ('explicit_storage', 'detected_storage', 'application'))
def test_incompatible_draft_v5_is_rejected_without_changing_database_bytes(tmp_path, opener):
    path = previous_library(tmp_path / 'library', 4)
    with closing(sqlite3.connect(path)) as db:
        before_sources = source_rows(db)
        # Reproduce the exact earlier eight-column V5 layout with retained V4
        # library data. Version 5 alone grants no permission to relabel it.
        db.executescript(_OLD_DRAFT_PUBLICATION_SCHEMA + '\nPRAGMA user_version=5;\n')
        assert [row[1] for row in db.execute('PRAGMA table_info(publication_receipt)')] == [
            'request_id', 'attempt_id', 'workspace', 'edition', 'revision', 'asset', 'payload', 'digest']
        old_signature = _schema_signature(db)
        assert schema_fingerprint(db) != _CANONICAL_V5_FINGERPRINT
    before = path.read_bytes()
    with pytest.raises(ValueError, match='schema'):
        if opener == 'application':
            LibraryApp(path.parent)
        elif opener == 'explicit_storage':
            with closing(Storage(path, schema_version=5)):
                pass
        else:
            with closing(Storage(path)):
                pass
    assert path.read_bytes() == before
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 5
        assert _schema_signature(db) == old_signature
        assert source_rows(db) == before_sources
        assert db.execute('SELECT count(*) FROM publication_receipt').fetchone()[0] == 0
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='import_publication'").fetchone()


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
        assert db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0
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
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='import_publication'").fetchone()


@pytest.mark.parametrize('version', (3, 4))
@pytest.mark.parametrize('failure', ('ddl', 'final_validation'))
def test_migration_failure_rolls_back_the_entire_chain(tmp_path, monkeypatch, version, failure):
    path = previous_library(tmp_path / 'library', version)
    before = path.read_bytes()
    if failure == 'ddl':
        monkeypatch.setattr(revisions, 'IMPORT_PUBLICATION_SCHEMA',
            revisions.IMPORT_PUBLICATION_SCHEMA + '\nSELECT absent_receipt_migration();\n')
        expected = sqlite3.OperationalError
    else:
        check = Storage._check
        def reject(storage):
            check(storage)
            if storage.schema_version == 5:
                raise ValueError('Final receipt validation failed.')
        monkeypatch.setattr(Storage, '_check', reject)
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
        "            assert self.execute(\"SELECT 1 FROM sqlite_master WHERE name='import_publication'\").fetchone()",
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
        assert upgraded.db.execute('SELECT count(*) FROM import_publication').fetchone()[0] == 0
