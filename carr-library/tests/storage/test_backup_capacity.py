"""Real SQLite backup progress, resource limits and unchanged publication checks."""
from contextlib import closing
from pathlib import Path
import sqlite3
import threading

import pytest

from carr_library.storage import revisions as module
from carr_library.storage.revisions import Storage, Page


def populated(tmp_path):
    storage = Storage(tmp_path / 'source.db')
    storage.publish_revision('workspace', 'edition', 'revision', 'a' * 64,
        'fixture', [Page(('Railway source bytes. ' * 200_000), None)])
    storage.check()
    return storage


class ObservedConnection:
    """Delegate the actual SQLite backup; observe its genuine page callbacks."""
    def __init__(self, connection, observe, *, pages=256):
        self.connection, self.observe = connection, observe
        self.pages = pages

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def backup(self, destination, **options):
        assert options['pages'] == self.pages
        callback = options['progress']
        def progress(status, remaining, total):
            self.observe(status, remaining, total, destination)
            return callback(status, remaining, total)
        return self.connection.backup(destination, **dict(options, progress=progress))


def assert_no_publication(tmp_path, target):
    assert not target.exists()
    assert not list(tmp_path.glob('.carr-backup-*'))


def test_real_sqlite_copy_is_chunked_and_keeps_full_integrity_validation(tmp_path):
    with closing(populated(tmp_path)) as storage:
        observed = []
        storage.db = ObservedConnection(storage.db,
            lambda status, remaining, total, destination: observed.append((remaining, total)))
        target = tmp_path / 'copy.db'
        storage.backup(target, cancelled=lambda: False)
        assert len(observed) > 1 and observed[-1][0] == 0
        assert any(remaining > 0 for remaining, _ in observed)
        with closing(Storage(target)) as copied:
            copied.check()
            assert copied.get_page('workspace', 'revision', 1)['raw'] == storage.get_page(
                'workspace', 'revision', 1)['raw']
            assert copied.db.execute(
                "SELECT count(*) FROM page_fts WHERE page_fts MATCH 'Railway'").fetchone()[0] == 1


@pytest.mark.parametrize('failure', ['cancel', 'deadline'])
def test_actual_copy_progress_cancels_without_publishing_or_changing_source(tmp_path, failure):
    with closing(populated(tmp_path)) as storage:
        original = (tmp_path / 'source.db').read_bytes()
        ready = threading.Event()
        observations = []
        def observe(status, remaining, total, destination):
            observations.append((remaining, total))
            if remaining > 0:
                ready.set()
        storage.db = ObservedConnection(storage.db, observe)
        error = TimeoutError('actual configured deadline')
        def stopped():
            if ready.is_set() and failure == 'deadline':
                raise error
            return ready.is_set()
        target = tmp_path / 'cancelled.db'
        with pytest.raises(TimeoutError if failure == 'deadline' else InterruptedError) as caught:
            storage.backup(target, cancelled=stopped)
        if failure == 'deadline':
            assert caught.value is error
        assert observations and observations[0][0] > 0
        assert_no_publication(tmp_path, target)
        assert (tmp_path / 'source.db').read_bytes() == original
        storage.check()


@pytest.mark.parametrize('failure', ['cancel', 'deadline'])
def test_actual_copied_database_validation_progress_preserves_stop_reason(
        tmp_path, monkeypatch, failure):
    with closing(populated(tmp_path)) as storage:
        # A single multi-MiB page exercises the copy callbacks, but its
        # validation runs too few SQLite VM operations to exercise the
        # progress handler. Add real distinct rows for this validation case.
        storage.publish_revision('workspace', 'validation-edition',
            'validation-revision', 'b' * 64, 'validation-fixture',
            [Page(f'Validation row {number}: Railway source bytes.', None)
             for number in range(2000)])
        storage.check()
        actual_check = Storage._check
        validating = False
        seen, finished = [], []
        def checked(self):
            nonlocal validating
            filename = Path(self.db.execute('PRAGMA database_list').fetchone()[2])
            if filename.name == 'snapshot.db' and filename.resolve().is_relative_to(tmp_path.resolve()):
                validating = True
            result = actual_check(self)
            if validating:
                finished.append(True)
            return result
        monkeypatch.setattr(Storage, '_check', checked)
        error = TimeoutError('deadline during the actual full integrity checker')
        def stopped():
            if validating:
                seen.append(True)
                if failure == 'deadline':
                    raise error
                return True
            return False
        target = tmp_path / 'validation-cancelled.db'
        with pytest.raises(TimeoutError if failure == 'deadline' else InterruptedError) as caught:
            storage.backup(target, cancelled=stopped)
        if failure == 'deadline':
            assert caught.value is error
        assert seen and not finished
        assert_no_publication(tmp_path, target)
        validating = False
        storage.check()


def test_database_over_budget_is_refused_before_copy(tmp_path):
    with closing(populated(tmp_path)) as storage:
        def forbidden(*args):
            pytest.fail('A known oversized database must not begin copying.')
        storage.db = ObservedConnection(storage.db, forbidden)
        target = tmp_path / 'too-large.db'
        with pytest.raises(ValueError, match='byte allowance'):
            storage.backup(target, maximum_bytes=1024)
        assert_no_publication(tmp_path, target)
        storage.check()


def test_actual_final_snapshot_growth_is_rechecked_before_publication(tmp_path, monkeypatch):
    with closing(populated(tmp_path)) as storage:
        page_size = storage.db.execute('PRAGMA page_size').fetchone()[0]
        maximum = storage.db.execute('PRAGMA page_count').fetchone()[0] * page_size
        checked = Storage.check
        grown = []
        def grow_after_real_check(self):
            result = checked(self)
            filename = Path(self.db.execute('PRAGMA database_list').fetchone()[2])
            if filename.name == 'snapshot.db' and filename.resolve().is_relative_to(tmp_path.resolve()):
                # Real caller-owned private file growth after the exact full check.
                with filename.open('ab') as stream:
                    stream.write(b'X' * page_size)
                    stream.flush()
                grown.append(filename)
            return result
        monkeypatch.setattr(Storage, 'check', grow_after_real_check)
        target = tmp_path / 'grown.db'
        with pytest.raises(ValueError, match='byte allowance'):
            storage.backup(target, maximum_bytes=maximum)
        assert grown and not grown[0].exists()
        assert_no_publication(tmp_path, target)


def test_existing_target_is_never_replaced_with_explicit_limits(tmp_path):
    with closing(populated(tmp_path)) as storage:
        target = tmp_path / 'exists.db'
        target.write_bytes(b'Existing destination is independently owned.')
        original = target.read_bytes()
        with pytest.raises(FileExistsError):
            storage.backup(target, maximum_bytes=64 * 1024**2)
        assert target.read_bytes() == original
        assert not list(tmp_path.glob('.carr-backup-*'))


def test_full_snapshot_fts_check_is_not_weakened_by_explicit_limits(tmp_path):
    with closing(populated(tmp_path)) as storage:
        storage.db.execute("INSERT INTO page_fts(page_fts) VALUES('delete-all')")
        target = tmp_path / 'corrupt.db'
        with pytest.raises(sqlite3.DatabaseError):
            storage.backup(target, maximum_bytes=64 * 1024**2)
        assert_no_publication(tmp_path, target)


@pytest.mark.parametrize('maximum', [True, 0, -1, 1.5, 2**63])
def test_snapshot_limit_is_an_exact_positive_bounded_integer(tmp_path, maximum):
    with closing(Storage(tmp_path / 'source.db')) as storage:
        target = tmp_path / 'invalid.db'
        with pytest.raises(ValueError, match='allowance'):
            storage.backup(target, maximum_bytes=maximum)
        assert_no_publication(tmp_path, target)


def test_default_backup_keeps_the_existing_native_call_shape(tmp_path):
    with closing(populated(tmp_path)) as storage:
        options_seen = []
        class LegacyConnection:
            def __getattr__(self, name):
                return getattr(original, name)
            def backup(self, destination, **options):
                options_seen.append(dict(options))
                return original.backup(destination, **options)
        original = storage.db
        storage.db = LegacyConnection()
        target = tmp_path / 'legacy.db'
        storage.backup(target)
        assert options_seen == [{}]
        with closing(Storage(target)) as copied:
            copied.check()
            assert copied.get_page('workspace', 'revision', 1)['raw'] == storage.get_page(
                'workspace', 'revision', 1)['raw']


def test_first_sqlite_batch_fits_a_cap_smaller_than_256_pages(tmp_path):
    with closing(Storage(tmp_path / 'small.db')) as storage:
        page_size = storage.db.execute('PRAGMA page_size').fetchone()[0]
        page_count = storage.db.execute('PRAGMA page_count').fetchone()[0]
        assert 0 < page_count < 256
        observed = []
        storage.db = ObservedConnection(storage.db,
            lambda status, remaining, total, destination: observed.append((remaining, total)),
            pages=page_count)
        target = tmp_path / 'bounded-small.db'
        maximum = page_count * page_size
        storage.backup(target, maximum_bytes=maximum)
        assert observed and observed[-1][0] == 0
        assert all(total * page_size <= maximum for _, total in observed)
        assert target.stat().st_size <= maximum
        with closing(Storage(target)) as copied:
            copied.check()


@pytest.mark.parametrize('failure', ['cancel', 'deadline'])
def test_selected_existing_database_check_keeps_stop_reason_and_releases_handler(
        tmp_path, monkeypatch, failure):
    with closing(populated(tmp_path)) as storage:
        actual = Storage._check
        active = False
        calls = []
        error = TimeoutError('selected existing database work allowance')
        def checking(self):
            nonlocal active
            if self is storage:
                active = True
            return actual(self)
        def cancelled():
            if active:
                calls.append(True)
                if failure == 'deadline':
                    raise error
                return True
            return False
        monkeypatch.setattr(Storage, '_check', checking)
        with pytest.raises(TimeoutError if failure == 'deadline' else InterruptedError) as caught:
            storage.check(cancelled=cancelled)
        if failure == 'deadline':
            assert caught.value is error
        assert calls
        before = len(calls)
        # A subsequent ordinary validation must not inherit this callback.
        storage.check()
        assert len(calls) == before
        assert not storage.db.in_transaction
