"""Real process-exit regression for the PR86 V3-to-V4 migration.

Reuse the existing fully populated historical-quotation fixture and production
Storage transaction. The child only injects process exit at SQLite commit.
"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from carr_library.storage import revisions
from carr_library.storage.revisions import Storage, _expected_signature, _schema_signature
from test_academic_migration import RAW, source_rows, v3_library


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
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (source_root, env.get("PYTHONPATH"))))
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(path), str(int(committed))],
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


if __name__ == "__main__":
    path, committed = sys.argv[1:]
    connect = sqlite3.connect

    class CrashConnection(sqlite3.Connection):
        def commit(self):
            if self.in_transaction and self.execute("PRAGMA user_version").fetchone()[0] == 4:
                # Both DDL and version must already be visible in the transaction.
                assert self.execute("SELECT 1 FROM sqlite_master WHERE name='academic_reference'").fetchone()
                if committed == "1":
                    super().commit()
                os._exit(73)
            return super().commit()

    def crash_connect(*args, **kwargs):
        kwargs["factory"] = CrashConnection
        return connect(*args, **kwargs)

    revisions.sqlite3.connect = crash_connect
    with closing(Storage(path, schema_version=4)):
        raise AssertionError("migration did not reach crash checkpoint")
