"""Synthetic source-locator regressions for PR #23 contract boundary."""
import sqlite3
import pytest
from carr_library.storage.revisions import Page, Storage


def publish(s, revision, pages):
    s.publish_revision('a', 'edition', revision, 'a'*64, 'fixture', pages)


@pytest.mark.parametrize('pages', [
    [Page('logical', None, 'iv')],
    [Page('logical'), Page('physical', 2)],
    [Page('physical', 1), Page('logical')],
    [Page('later', 2), Page('earlier', 1)],
    [Page('fractional', 1.5)],
    [Page('empty label', 1, '', True)],
])
def test_invalid_revision_rolls_back(tmp_path, pages):
    s = Storage(tmp_path/'db')
    try:
        publish(s, 'old', [Page('old needle', 1)])
        with pytest.raises(sqlite3.IntegrityError):
            publish(s, 'bad', pages)
        assert s.get_page('a', 'bad', 1) is None
        assert s.db.execute('SELECT revision FROM active').fetchone()[0] == 'old'
        assert s.db.execute('SELECT raw FROM indexed').fetchone()[0] == 'old needle'
        s.check()
    finally:
        s.close()


def test_history_and_backup(tmp_path):
    s = Storage(tmp_path/'db')
    try:
        publish(s, 'physical', [Page('Міст', 1, 'iv', True), Page('river', 3)])
        publish(s, 'logical', [Page('розділ'), Page('next')])
        s.backup(tmp_path/'copy')
    finally:
        s.close()
    c = Storage(tmp_path/'copy')
    try:
        c.check()
        assert c.get_page('a','physical',1)['printed'] == 'iv'
        assert c.get_page('a','physical',2)['physical'] == 3
        assert c.get_page('a','logical',1)['physical'] is None
        assert c.get_page('b','physical',1) is None
    finally:
        c.close()


def test_bad_historical_locator_refuses_backup(tmp_path):
    s = Storage(tmp_path/'db')
    try:
        publish(s, 'old', [Page('first', 1), Page('second', 2)])
        publish(s, 'active', [Page('new')])
        # Synthetic database corruption; not an exposed user operation.
        trigger = s.db.execute("SELECT sql FROM sqlite_master WHERE name='page_bu'").fetchone()[0]
        s.db.execute('DROP TRIGGER page_bu')
        s.db.execute("UPDATE page SET physical=3 WHERE revision='old' AND ordinal=1")
        # Restore exact schema so this test still exercises locator corruption.
        s.db.execute(trigger)
        assert s.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        with pytest.raises(sqlite3.IntegrityError):
            s.check()
        with pytest.raises(sqlite3.IntegrityError):
            s.backup(tmp_path/'copy')
        assert not (tmp_path/'copy').exists()
    finally:
        s.close()


_LEGACY_LOCATOR_SEQUENCE = """
    WITH ordered AS (
        SELECT ordinal, physical, printed, verified,
            row_number() OVER w AS expected,
            lag(physical) OVER w AS previous_physical,
            lag(physical IS NULL) OVER w AS previous_logical
        FROM page WHERE (? IS NULL OR revision=?)
        WINDOW w AS (PARTITION BY revision ORDER BY ordinal)
    )
    SELECT 1 FROM ordered WHERE ordinal != expected
        OR (physical IS NULL AND (printed IS NOT NULL OR verified != 0))
        OR (physical IS NOT NULL AND typeof(physical) != 'integer')
        OR (printed IS NOT NULL AND (typeof(printed)!='text' OR instr(printed,char(0))>0))
        OR (verified=1 AND (printed IS NULL OR trim(printed, char(9,10,13,32))=''))
        OR (expected>1 AND previous_logical != (physical IS NULL))
        OR (physical IS NOT NULL AND physical <= previous_physical)
    LIMIT 1
"""
_LEGACY_LOCATOR_LABELS = (
    'SELECT physical,printed FROM page WHERE verified=1 AND (? IS NULL OR revision=?)')


def _observed_locator_queries(store, revision):
    real, statements = store.db, []
    class ObservedConnection:
        def execute(self, sql, parameters=()):
            statements.append((sql, tuple(parameters)))
            return real.execute(sql, parameters)
    store.db = ObservedConnection()
    try:
        store._check_locators(revision)
    finally:
        store.db = real
    assert len(statements) == 2
    return statements


def _locator_query_profile(db, sql, parameters):
    from hashlib import sha256

    steps = 0
    def progress():
        nonlocal steps
        steps += 1
        return 0
    source = None
    db.set_progress_handler(progress, 1)
    try:
        source = db.execute(sql, parameters)
        rows = tuple(tuple(row) for row in source)
    finally:
        if source is not None:
            source.close()
        db.set_progress_handler(None, 0)
    plan = tuple(tuple(row) for row in db.execute('EXPLAIN QUERY PLAN ' + sql, parameters))
    return rows, dict(sql_sha256=sha256(sql.encode('utf-8')).hexdigest(),
                      vm_steps=steps, rows=len(rows), query_plan=plan)


def test_scoped_locator_queries_use_existing_index_without_omitting_global_history(
        tmp_path, record_testsuite_property):
    from contextlib import closing
    import json

    path = tmp_path / 'library.db'
    with closing(Storage(path)) as store:
        # Genuine sealed historical revisions; no SQL-seeded replacement corpus.
        for number in range(24):
            publish(store, f'history-{number:02d}', [
                Page(f'Original history {number}:{page}', page + 1)
                for page in range(40)])
        publish(store, 'target', [
            Page('Target first', 1, 'iv', True),
            Page('Target second', 3, 'С. 12', True),
            Page('Target third', 7, '頁十二', True)])
        store.check()
        before = path.read_bytes()
        legacy_sql = (_LEGACY_LOCATOR_SEQUENCE, _LEGACY_LOCATOR_LABELS)
        profiles = {}
        for revision in ('target', None):
            statements = _observed_locator_queries(store, revision)
            query_profiles = []
            for (current_sql, parameters), old_sql in zip(statements, legacy_sql):
                current_rows, current_profile = _locator_query_profile(
                    store.db, current_sql, parameters)
                old_rows, old_profile = _locator_query_profile(
                    store.db, old_sql, (revision, revision))
                assert current_rows == old_rows
                assert current_profile['vm_steps'] > 0
                if revision is not None:
                    assert current_profile['vm_steps'] < old_profile['vm_steps']
                query_profiles.append(dict(current=current_profile, legacy=old_profile))
            profiles['all-history' if revision is None else revision] = query_profiles
        assert path.read_bytes() == before
        assert store.db.execute('SELECT count(*) FROM page').fetchone()[0] == 963
        assert store.get_page('a', 'history-00', 1)['raw'] == 'Original history 0:0'
        record_testsuite_property('locator_sql_profiles', json.dumps(
            dict(scope='real sealed rows; query work only', profiles=profiles), sort_keys=True))
        store.check()


@pytest.mark.parametrize('damage', [
    'ordinal-gap', 'physical-order', 'mixed-kind', 'verified-label'
])
def test_scoped_success_cannot_hide_corrupt_inactive_locator_from_global_check(
        tmp_path, damage):
    from contextlib import closing

    path = tmp_path / 'library.db'
    with closing(Storage(path)) as store:
        publish(store, 'history', [Page('Historical first', 1, 'iv', True),
                                   Page('Historical second', 2)])
        publish(store, 'current', [Page('Current logical first'), Page('Current logical second')])
        trigger = store.db.execute(
            "SELECT sql FROM sqlite_master WHERE name='page_bu'").fetchone()[0]
        store.db.execute('DROP TRIGGER page_bu')
        try:
            if damage == 'ordinal-gap':
                store.db.execute("UPDATE page SET ordinal=3 WHERE revision='history' AND ordinal=2")
            elif damage == 'physical-order':
                store.db.execute("UPDATE page SET physical=3 WHERE revision='history' AND ordinal=1")
            elif damage == 'mixed-kind':
                store.db.execute("UPDATE page SET physical=NULL,printed=NULL,verified=0 "
                                 "WHERE revision='history' AND ordinal=1")
            else:
                store.db.execute("UPDATE page SET printed=? WHERE revision='history' AND ordinal=1",
                                 ('iv\nForged citation',))
        finally:
            store.db.execute(trigger)
        before = path.read_bytes()
        assert store.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        store._check_locators('current')
        with pytest.raises(sqlite3.IntegrityError):
            store._check_locators('history')
        with pytest.raises(sqlite3.IntegrityError):
            store._check_locators()
        with pytest.raises(sqlite3.IntegrityError):
            store.check()
        destination = tmp_path / 'refused-backup.db'
        with pytest.raises(sqlite3.IntegrityError):
            store.backup(destination)
        assert not destination.exists()
        assert path.read_bytes() == before
        assert store.db.execute('SELECT revision FROM active').fetchone()[0] == 'current'
        assert [row[0] for row in store.db.execute('SELECT raw FROM indexed ORDER BY id')] == [
            'Current logical first', 'Current logical second']


def test_scoped_locator_query_remains_interruptible_without_mutating_source(tmp_path):
    from contextlib import closing

    path = tmp_path / 'library.db'
    with closing(Storage(path)) as store:
        publish(store, 'source', [Page(f'Exact source {page}', page + 1) for page in range(40)])
        before = path.read_bytes()
        calls = 0
        def stop():
            nonlocal calls
            calls += 1
            return 1
        store.db.set_progress_handler(stop, 1)
        try:
            with pytest.raises(sqlite3.OperationalError, match='interrupted'):
                store._check_locators('source')
        finally:
            store.db.set_progress_handler(None, 0)
        assert calls > 0 and path.read_bytes() == before
        store.check()
        assert store.get_page('a', 'source', 40)['raw'] == 'Exact source 39'
