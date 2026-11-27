"""Actual SQLite publication/session boundaries; no synthetic PASS evidence."""
from contextlib import closing
from hashlib import sha256
import sqlite3

import pytest

from carr_library.application import LibraryApp, _CatalogPublisher
from carr_library.catalog.registration import Registration
from carr_library.storage.revisions import CURRENT_SCHEMA, Page, Storage


@pytest.fixture
def library(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Verified publication')
    with closing(Storage(app.database, schema_version=CURRENT_SCHEMA)) as storage:
        yield app, workspace, storage


def _publisher(library, identity, *, raw=None, edition=None):
    app, workspace, storage = library
    edition = edition or f'edition-{identity}'
    raw = raw if raw is not None else f'Exact authored source {edition}.'
    data = raw.encode('utf-8')
    digest = sha256(data).hexdigest()
    app.assets.retain(data, 'text/plain', digest)
    record = Registration(
        f'work-{edition}', edition,
        dict(sha256=digest, mime='text/plain', size=len(data)),
        f'Book {edition}', ('Fixture Author',),
    )
    publisher = _CatalogPublisher(storage, record)
    return publisher, workspace, edition, f'revision-{identity}', digest, raw


def _publish(library, identity, *, raw=None, edition=None, pages=None, hook=None):
    publisher, workspace, edition, revision, digest, text = _publisher(
        library, identity, raw=raw, edition=edition)
    if hook is not None:
        publisher.before_commit = hook
    publisher.publish_revision(
        workspace, edition, revision, digest, 'fixture-v1',
        pages if pages is not None else [Page(text)],
    )
    return publisher, revision


def _raw(storage, revision):
    row = storage.db.execute('SELECT raw FROM page WHERE revision=?', (revision,)).fetchone()
    return row[0] if row is not None else None


def _damage_raw(db, revision):
    triggers = list(db.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='page'"))
    for name, _ in triggers:
        assert name.replace('_', '').isalnum()
        db.execute(f'DROP TRIGGER "{name}"')
    db.execute('UPDATE page SET raw=? WHERE revision=?', ('changed same-host bytes', revision))
    for _, sql in triggers:
        db.execute(sql)


def test_known_independent_commits_reuse_snapshot_and_finish_with_full_check(library, monkeypatch):
    app, workspace, storage = library
    checked = []
    real_check = storage._check

    def full_check():
        checked.append(storage.db.total_changes)
        return real_check()

    monkeypatch.setattr(storage, '_check', full_check)
    with storage.incremental_publications():
        for number in range(12):
            publisher, revision = _publish(library, number)
            publisher.check()
            # A second actual connection sees each book's committed result,
            # before the next book or the session itself completes.
            with closing(sqlite3.connect(app.database)) as reader:
                row = reader.execute('SELECT raw FROM page WHERE revision=?', (revision,)).fetchone()
                assert row == (f'Exact authored source edition-{number}.',)
        assert len(checked) == 1
    assert len(checked) == 2
    storage.check()


def test_failed_later_book_preserves_the_previous_committed_book(library):
    _, _, storage = library

    def fail(workspace, revision):
        raise RuntimeError('stop after real staging')

    with pytest.raises(RuntimeError, match='real staging'):
        with storage.incremental_publications():
            _publish(library, 'first')
            _publish(library, 'second', hook=fail)
    assert _raw(storage, 'revision-first') == 'Exact authored source edition-first.'
    assert _raw(storage, 'revision-second') is None
    storage.check()


def test_new_revision_removes_old_active_index_but_preserves_history(library):
    _, workspace, storage = library
    with storage.incremental_publications():
        _publish(library, 'old', edition='same', raw='retained source')
        _publish(library, 'new', edition='same', raw='retained source',
                 pages=[Page('reviewed current extraction')])
        assert storage.get_page(workspace, 'revision-old', 1)['raw'] == 'retained source'
        rows = storage.db.execute(
            'SELECT p.revision,i.raw FROM indexed i JOIN page p ON p.id=i.id').fetchall()
        assert [tuple(row) for row in rows] == [
            ('revision-new', 'reviewed current extraction')]
    storage.check()


@pytest.mark.parametrize('channel', ['same_connection', 'external_connection'])
def test_unknown_committed_raw_change_is_revalidated_before_next_book(library, channel):
    app, _, storage = library
    with pytest.raises(ValueError, match='raw digest mismatch'):
        with storage.incremental_publications():
            _publish(library, 'old')
            if channel == 'same_connection':
                with storage.transaction():
                    _damage_raw(storage.db, 'revision-old')
            else:
                with sqlite3.connect(app.database) as external:
                    external.execute('PRAGMA foreign_keys=ON')
                    external.execute('PRAGMA recursive_triggers=ON')
                    _damage_raw(external, 'revision-old')
            _publish(library, 'must-not-appear')
    assert _raw(storage, 'revision-must-not-appear') is None


@pytest.mark.parametrize('damage', ['foreign_raw', 'foreign_index', 'fts_shadow'])
def test_custom_callback_corruption_rolls_back_the_whole_new_transaction(library, damage):
    _, _, storage = library
    with storage.incremental_publications():
        _publish(library, 'old')

        def mutate(workspace, revision):
            if damage == 'foreign_raw':
                _damage_raw(storage.db, 'revision-old')
            elif damage == 'foreign_index':
                storage.db.execute(
                    'DELETE FROM indexed WHERE id IN '
                    '(SELECT id FROM page WHERE revision=?)', ('revision-old',))
            else:
                storage.db.execute("INSERT INTO page_fts(page_fts) VALUES('delete-all')")

        with pytest.raises((ValueError, sqlite3.DatabaseError)):
            _publish(library, 'rejected', hook=mutate)
        assert _raw(storage, 'revision-old') == 'Exact authored source edition-old.'
        assert _raw(storage, 'revision-rejected') is None
        # The next accepted book must revalidate after the rollback.
        _publish(library, 'accepted')
    storage.check()


def test_raw_write_from_page_iterator_is_not_adopted_as_known_delta(library):
    _, _, storage = library
    with storage.incremental_publications():
        _publish(library, 'old')

        def pages():
            storage.db.execute(
                'DELETE FROM indexed WHERE id IN '
                '(SELECT id FROM page WHERE revision=?)', ('revision-old',))
            yield Page('candidate text')

        with pytest.raises(ValueError, match='active index content mismatch'):
            _publish(library, 'rejected', pages=pages())
        assert _raw(storage, 'revision-rejected') is None
        assert storage.db.execute(
            'SELECT 1 FROM indexed i JOIN page p ON p.id=i.id '
            'WHERE p.revision=?', ('revision-old',)).fetchone()
    storage.check()


def test_same_connection_fts_control_write_is_not_a_trusted_snapshot(library):
    _, _, storage = library
    with pytest.raises(sqlite3.DatabaseError):
        with storage.incremental_publications():
            _publish(library, 'old')
            storage.db.execute("INSERT INTO page_fts(page_fts) VALUES('delete-all')")
            _publish(library, 'rejected')
    assert _raw(storage, 'revision-rejected') is None


def test_late_external_commit_is_not_adopted_by_postcommit_version_read(library, monkeypatch):
    app, _, storage = library
    real_transaction = storage.transaction
    inject = {'armed': False, 'done': False}

    from contextlib import contextmanager

    @contextmanager
    def late_writer():
        with real_transaction():
            yield
        if inject['armed'] and not inject['done']:
            inject['done'] = True
            with sqlite3.connect(app.database) as external:
                external.execute('PRAGMA foreign_keys=ON')
                external.execute('PRAGMA recursive_triggers=ON')
                _damage_raw(external, 'revision-old')

    with pytest.raises(ValueError, match='raw digest mismatch'):
        with storage.incremental_publications():
            _publish(library, 'old')
            monkeypatch.setattr(storage, 'transaction', late_writer)
            inject['armed'] = True
            _publish(library, 'committed-before-external')
            _publish(library, 'rejected')
    assert inject['done']
    assert _raw(storage, 'revision-committed-before-external') is not None
    assert _raw(storage, 'revision-rejected') is None


def test_backup_inside_session_keeps_full_validation(library, tmp_path):
    _, _, storage = library
    with pytest.raises(ValueError, match='raw digest mismatch'):
        with storage.incremental_publications():
            _publish(library, 'old')
            with storage.transaction():
                _damage_raw(storage.db, 'revision-old')
            storage.backup(tmp_path / 'must-not-publish.sqlite')
    assert not (tmp_path / 'must-not-publish.sqlite').exists()


def test_nested_session_and_cross_identity_staging_are_rejected(library):
    _, workspace, storage = library
    with storage.incremental_publications():
        with pytest.raises(ValueError, match='nested'):
            with storage.incremental_publications():
                pass

        def pages():
            storage.stage_revision(
                workspace, 'other', 'unrelated', 'b' * 64, 'fixture-v1', [Page('foreign')])
            yield Page('candidate')

        with pytest.raises(ValueError, match='another identity'):
            _publish(library, 'rejected', pages=pages())
        assert _raw(storage, 'unrelated') is None
        assert _raw(storage, 'revision-rejected') is None
    storage.check()


def test_reopening_never_inherits_a_prior_validation_stamp(library):
    app, _, storage = library
    with storage.incremental_publications():
        _publish(library, 'old')
    with sqlite3.connect(app.database) as external:
        external.execute('PRAGMA foreign_keys=ON')
        external.execute('PRAGMA recursive_triggers=ON')
        _damage_raw(external, 'revision-old')
    with closing(Storage(app.database, schema_version=CURRENT_SCHEMA)) as reopened:
        with pytest.raises(ValueError, match='raw digest mismatch'):
            with reopened.incremental_publications():
                pass
