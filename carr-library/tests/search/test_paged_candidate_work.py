"""Actual SQLite and retained-source guards for call-local candidate work.

These fixtures measure bounded duplicate computation, not corpus performance.
Every public page still has its original fresh lookahead snapshot and source
attestation. No native, sustained-ingest or two-million-result PASS is claimed.
"""
from collections import Counter
from contextlib import closing, contextmanager
from hashlib import sha256
import sqlite3
from threading import Event

import pytest

import carr_library.application as application
from carr_library.application import LibraryApp
from carr_library.search import literal


def _app(tmp_path, count=100, *, collection=False):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Fresh page37 source')
    selected = app.create_collection(workspace, 'Selected') if collection else None
    text = '\f'.join(
        f'Needle Straße Україна Ω segment {number:03d}.  exact source.\n'
        for number in range(1, count + 1))
    imported = app.add_text(workspace, 'Exact retained source', text,
                            edition_id='edition', work_id='work',
                            collections=(selected,) if selected else ())
    assert imported['state'] == 'complete'
    return app, workspace, imported, text, selected


def _track_work(monkeypatch):
    original = literal._CandidateWork
    owners, indexes = [], []

    class Tracked(original):
        def __init__(self):
            super().__init__()
            owners.append(self)

        def _bind(self, rows, stopped, check):
            index = super()._bind(rows, stopped, check)
            if not any(index is old for old in indexes):
                indexes.append(index)
            return index

    monkeypatch.setattr(literal, '_CandidateWork', Tracked)
    return owners, indexes


def _closed(owners, indexes):
    assert owners and indexes
    assert all(owner._closed and owner._rows is None and not owner._maps
               and owner._index is None and owner._map_characters == 0 for owner in owners)
    for index in indexes:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            index.execute('SELECT 1')


def _observe_reads(monkeypatch, *, before_second=lambda: None, second_ready=lambda db: None):
    original = LibraryApp._search_read
    reads = []

    @contextmanager
    def observe(self, workspace, timeout, cancelled):
        number = len(reads) + 1
        if number == 2:
            before_second()
        with original(self, workspace, timeout, cancelled) as value:
            reads.append(value[0])
            if number == 2:
                second_ready(value[0])
            yield value

    monkeypatch.setattr(LibraryApp, '_search_read', observe)
    return reads


def test_actual_page37_keeps_two_fresh_reads_and_hidden_attestation_with_one_candidate_index(tmp_path, monkeypatch):
    app, workspace, imported, text, _ = _app(tmp_path)
    owners, indexes = _track_work(monkeypatch)
    reads = _observe_reads(monkeypatch)
    mapped = Counter()
    original_mapping = literal._mapped
    attestations = []
    original_attestor = application.strict_text_attestor

    def mapping(raw, check=lambda: None):
        mapped[raw] += 1
        return original_mapping(raw, check)

    def attestor(db, *args, **kwargs):
        actual = original_attestor(db, *args, **kwargs)

        def checked(identity, locator, digest):
            result = actual(identity, locator, digest)
            attestations.append((db, locator.index, result))
            return result
        return checked

    monkeypatch.setattr(literal, '_mapped', mapping)
    monkeypatch.setattr(application, 'strict_text_attestor', attestor)
    first = app.search_page(workspace, 'Needle', mode='terms', limit=37)
    assert len(first.results) == 37 and first.has_more_results
    assert first.source_pages == 100 and all(row.verified for row in first.results)
    assert len(reads) == 2 and reads[0] is not reads[1]
    assert [ordinal for db, ordinal, accepted in attestations if db is reads[0] and accepted] == list(range(1, 38))
    assert [(ordinal, accepted) for db, ordinal, accepted in attestations if db is reads[1]] == [(38, True)]
    assert len(app._selections) == 37
    assert len(indexes) == 1 and len(mapped) == 38 and set(mapped.values()) == {1}
    _closed(owners, indexes)
    for db in reads:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            db.execute('SELECT 1')

    second = app.search_page(workspace, 'Needle', mode='terms', limit=37,
                             cursor=first.cursor, offset=37)
    assert len(second.results) == 37 and second.has_more_results
    assert first.window_identity == second.window_identity
    assert len(owners) == len(indexes) == 2
    assert mapped[text.split('\f')[0]] == 2  # no computation survives the public call
    _closed(owners, indexes)
    last = second.results[-1]
    assert app.copy(workspace, last.ref, last.revision, 'quote') == 'Needle'
    assert app.assets.resolve(imported['asset']).read_bytes() == text.encode('utf-8')


@pytest.mark.parametrize('mutation', ['same_size', 'append', 'missing'])
def test_actual_lookahead_still_reauthenticates_retained_bytes_before_handle_publication(tmp_path, monkeypatch, mutation):
    app, workspace, imported, text, _ = _app(tmp_path)
    app.search(workspace, 'Needle', mode='terms', limit=1)
    before = dict(app._selections)
    original = app.assets.resolve(imported['asset'])
    data = original.read_bytes()
    owners, indexes = _track_work(monkeypatch)

    def change():
        if mutation == 'same_size':
            altered = data.replace(b'Needle', b'Broken', 1)
            assert len(altered) == len(data) and altered != data
            original.write_bytes(altered)
        elif mutation == 'append':
            original.write_bytes(data + b'\nchanged source\n')
        else:
            original.unlink()

    reads = _observe_reads(monkeypatch, before_second=change)
    with pytest.raises(ValueError, match='retained|original|source'):
        app.search_page(workspace, 'Needle', mode='terms', limit=37)
    assert len(reads) == 2 and app._selections == before
    _closed(owners, indexes)
    if mutation == 'missing':
        assert not original.exists()
    else:
        assert original.read_bytes() != data
    assert data == text.encode('utf-8')


@pytest.mark.parametrize('mutation', ['schema', 'workspace'])
def test_fresh_lookahead_does_not_reuse_schema_or_workspace_authority(tmp_path, monkeypatch, mutation):
    app, workspace, _, _, _ = _app(tmp_path)
    app.search(workspace, 'Needle', limit=1)
    before = dict(app._selections)
    owners, indexes = _track_work(monkeypatch)

    def change():
        with closing(sqlite3.connect(app.database)) as db:
            if mutation == 'schema':
                db.execute('CREATE TABLE unexpected_source_authority(value TEXT)')
            else:
                db.execute('UPDATE workspace_meta SET archived=1 WHERE workspace=?', (workspace,))
            db.commit()

    _observe_reads(monkeypatch, before_second=change)
    with pytest.raises(ValueError, match='schema|archived'):
        app.search_page(workspace, 'Needle', mode='terms', limit=37)
    assert app._selections == before
    _closed(owners, indexes)


def test_current_collection_change_still_rejects_the_changed_window(tmp_path, monkeypatch):
    app, workspace, imported, _, collection = _app(tmp_path, collection=True)
    app.search(workspace, 'Needle', limit=1, collection=collection)
    before = dict(app._selections)
    owners, indexes = _track_work(monkeypatch)

    def remove():
        with app.catalog_transaction(workspace) as catalog:
            assert catalog.remove(collection, imported['edition'])

    _observe_reads(monkeypatch, before_second=remove)
    with pytest.raises(ValueError, match='source window changed'):
        app.search_page(workspace, 'Needle', mode='terms', limit=37, collection=collection)
    assert app._selections == before
    _closed(owners, indexes)


def test_real_sql_cancellation_in_the_second_snapshot_closes_work_and_preserves_previous_handles(tmp_path, monkeypatch):
    app, workspace, _, _, _ = _app(tmp_path)
    app.search(workspace, 'Needle', limit=1)
    before = dict(app._selections)
    owners, indexes = _track_work(monkeypatch)
    cancelled = Event()
    statements = []

    def ready(db):
        def trace(sql):
            statements.append(sql)
            if 'FROM active_page' in sql:
                cancelled.set()
        db.set_trace_callback(trace)

    reads = _observe_reads(monkeypatch, second_ready=ready)
    with pytest.raises(TimeoutError, match='cancelled|timed out'):
        app.search_page(workspace, 'Needle', mode='terms', limit=37, cancelled=cancelled.is_set)
    assert len(reads) == 2 and cancelled.is_set()
    assert any('FROM active_page' in statement for statement in statements)
    assert app._selections == before
    _closed(owners, indexes)


@pytest.mark.parametrize('outcome', ['cancel', 'cleanup_error'])
def test_candidate_cleanup_finishes_before_any_new_public_handles(tmp_path, monkeypatch, outcome):
    app, workspace, _, _, _ = _app(tmp_path)
    app.search(workspace, 'Needle', limit=1)
    before = dict(app._selections)
    stopped = Event()
    owners, indexes = _track_work(monkeypatch)
    actual_close = literal._CandidateWork.close

    def close(self):
        actual_close(self)
        if outcome == 'cancel':
            stopped.set()
        else:
            raise RuntimeError('controlled candidate cleanup completion error')

    monkeypatch.setattr(literal._CandidateWork, 'close', close)
    expected = TimeoutError if outcome == 'cancel' else RuntimeError
    with pytest.raises(expected, match='cancelled|timed out|controlled candidate cleanup'):
        app.search_page(workspace, 'Needle', mode='terms', limit=37, cancelled=stopped.is_set)
    assert app._selections == before
    _closed(owners, indexes)


@contextmanager
def _source_rows(raws):
    with closing(sqlite3.connect(':memory:')) as db:
        db.execute('CREATE TABLE active_page(id INTEGER PRIMARY KEY, workspace TEXT, edition TEXT, '
                   'revision TEXT, asset TEXT, ordinal INTEGER, physical INTEGER, printed TEXT, '
                   'verified INTEGER, raw TEXT, digest TEXT)')
        for number, raw in enumerate(raws, 1):
            db.execute('INSERT INTO active_page VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (number, 'workspace', 'edition', 'revision', 'a' * 64,
                        number, number, str(number), 1, raw, sha256(raw.encode('utf-8')).hexdigest()))
        yield db


@pytest.mark.parametrize(('mode', 'query'), [
    ('word', 'STRASSE'),
    ('phrase', 'STRASSE Україна'),
    ('terms', 'STRASSE Україна'),
    ('advanced', '"STRASSE" AND "Україна"'),
])
@pytest.mark.parametrize('offset', [0, 2])
def test_shared_and_independent_actual_sqlite_search_have_identical_unicode_raw_spans(mode, query, offset):
    raws = [f'Історія {number}: Straße\nУкраїна Ω. Needle remains exact.'
            for number in range(5)]
    with _source_rows(raws) as db, literal._CandidateWork() as work:
        options = dict(mode=mode, edition_ids=('edition',), page_ids=tuple(range(1, 6)),
                       limit=2, offset=offset)
        baseline = literal.search(db, 'workspace', query, **options)
        first = literal.search(db, 'workspace', query, _candidate_work=work, **options)
        second = literal.search(db, 'workspace', query, _candidate_work=work, **options)
        assert first == second == baseline and first
        for hit in first:
            assert raws[hit['page_id'] - 1][hit['start']:hit['end']] == hit['raw_quote']
        index = work._index
    _closed([work], [index])


@pytest.mark.parametrize('mutation', ['new_match', 'removed_match', 'locator', 'revision', 'membership'])
def test_complete_fresh_sql_row_changes_rebuild_candidate_work_without_stale_results(mutation):
    second = 'Absent before actual mutation.' if mutation == 'new_match' else 'Needle second source.'
    with _source_rows(['Needle first source.', second]) as db, literal._CandidateWork() as work:
        options = dict(mode='terms', edition_ids=('edition',), page_ids=(1, 2), limit=100)
        first = literal.search(db, 'workspace', 'Needle', _candidate_work=work, **options)
        assert first
        previous_index = work._index
        if mutation in ('new_match', 'removed_match'):
            raw = 'Needle newly admitted exact source.' if mutation == 'new_match' else 'Removed old match.'
            number = 2 if mutation == 'new_match' else 1
            db.execute('UPDATE active_page SET raw=?,digest=? WHERE id=?',
                       (raw, sha256(raw.encode('utf-8')).hexdigest(), number))
        elif mutation == 'locator':
            db.execute("UPDATE active_page SET printed='changed exact label' WHERE id=1")
        elif mutation == 'revision':
            db.execute("UPDATE active_page SET revision='replacement-revision' WHERE id=1")
        else:
            db.execute("UPDATE active_page SET edition='excluded' WHERE id=1")
        baseline = literal.search(db, 'workspace', 'Needle', **options)
        current = literal.search(db, 'workspace', 'Needle', _candidate_work=work, **options)
        assert current == baseline and current != first
        assert work._index is not previous_index
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            previous_index.execute('SELECT 1')
        current_index = work._index
    _closed([work], [current_index])


def test_damaged_unmatched_row_cannot_hide_behind_a_matching_retained_index():
    with _source_rows(['Needle valid first source.', 'Unmatched exact second source.']) as db:
        work = literal._CandidateWork()
        with pytest.raises(ValueError, match='raw page digest mismatch'):
            with work:
                options = dict(mode='terms', edition_ids=('edition',), page_ids=(1, 2), limit=1)
                assert literal.search(db, 'workspace', 'Needle', _candidate_work=work, **options)
                index = work._index
                db.execute("UPDATE active_page SET raw='Damaged unmatched bytes.' WHERE id=2")
                literal.search(db, 'workspace', 'Needle', _candidate_work=work, **options)
        _closed([work], [index])


def test_reused_index_drops_old_deadline_callback_and_obeys_the_new_one():
    with _source_rows(['Needle exact source.']) as db, literal._CandidateWork() as work:
        stale = Event()

        def old_cancelled():
            if stale.is_set():
                raise AssertionError('An old read callback survived its search.')
            return False

        options = dict(mode='terms', edition_ids=('edition',), page_ids=(1,), limit=1)
        assert literal.search(db, 'workspace', 'Needle', _candidate_work=work,
                              cancelled=old_cancelled, **options)
        index = work._index
        stale.set()
        # More than 1000 real SQLite VM operations after the first search must
        # not invoke the detached snapshot's callback.
        assert index.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL '
                             'SELECT x+1 FROM n WHERE x<5000) SELECT sum(x) FROM n').fetchone()[0] == 12502500
        assert literal.search(db, 'workspace', 'Needle', _candidate_work=work,
                              cancelled=lambda: False, **options)
        with pytest.raises(TimeoutError, match='cancelled|timed out'):
            literal.search(db, 'workspace', 'Needle', _candidate_work=work,
                           cancelled=lambda: True, **options)
    _closed([work], [index])


def test_normalization_retention_is_bounded_and_large_pages_are_not_cached():
    raws = [f'Needle {number:03d} Україна. ' + ('exact source ' * 90) for number in range(99)]
    raws.append('Needle final large source. ' + 'unchanged ' * 1000)
    with _source_rows(raws) as db, literal._CandidateWork() as work:
        options = dict(mode='terms', edition_ids=('edition',),
                       page_ids=tuple(range(1, 101)), limit=100)
        baseline = literal.search(db, 'workspace', 'Needle', **options)
        first = literal.search(db, 'workspace', 'Needle', _candidate_work=work, **options)
        second = literal.search(db, 'workspace', 'Needle', _candidate_work=work, **options)
        assert len(first) == 100 and first == second == baseline
        assert 0 < work._map_characters <= 32 * 1024
        assert len(work._maps) <= 100 and 100 not in work._maps
        index = work._index
    _closed([work], [index])


@pytest.mark.parametrize('internal', ['_source_page', '_page_info', '_candidate_work'])
def test_public_source_page_cannot_accept_a_caller_supplied_internal_receiver(tmp_path, internal):
    app, workspace, _, _, _ = _app(tmp_path, count=1)
    app.search(workspace, 'Needle', limit=1)
    before = dict(app._selections)
    with pytest.raises(ValueError, match='Internal source-page'):
        app.search_page(workspace, 'Needle', **{internal: object()})
    assert app._selections == before


def test_closed_candidate_work_cannot_be_reopened_as_a_later_search():
    with _source_rows(['Needle retained source.']) as db:
        work = literal._CandidateWork()
        with work:
            assert literal.search(db, 'workspace', 'Needle', mode='terms',
                                  page_ids=(1,), _candidate_work=work)
            index = work._index
        _closed([work], [index])
        with pytest.raises(ValueError, match='bounded source window|closed'):
            literal.search(db, 'workspace', 'Needle', mode='terms',
                           page_ids=(1,), _candidate_work=work)
