"""Bounded notebook search using the canonical schema and independent test readers.

These focused tests use explicit synthetic source/metadata attestors. Real
retained originals and app authorization are covered by the notebook E2E suite.
"""
from dataclasses import FrozenInstanceError, replace

import pytest

from carr_library.citations.anchors import Identity, Locator, Metadata, make_anchor
from carr_library.research import notebook_search as service
from carr_library.research.saved_evidence import SavedEvidenceStore
from carr_library.research.topic_tree import TopicTree
from carr_library.storage.revisions import Storage


class Notebook:
    def __init__(self, path):
        self.storage = Storage(path, schema_version=3)
        self.db = self.storage.db
        self.db.execute('BEGIN IMMEDIATE')
        self.pages, self.metadata = {}, {}
        self.loads, self.attestations = [], []
        for workspace in ('a', 'b'):
            self.db.execute('INSERT INTO workspace VALUES(?)', (workspace,))
            self.db.execute('INSERT INTO workspace_meta(workspace,name,description) VALUES(?,?,?)',
                            (workspace, 'Library ' + workspace, ''))
            tree = TopicTree(self.db, workspace)
            tree.create('root', 'Research')
            tree.create('child', 'Українські джерела', parent='root')
            tree.create('empty', 'No saved evidence')

    def store(self, workspace='a'):
        return SavedEvidenceStore(self.db, workspace, self.load, self.attest)

    def load(self, identity, locator):
        self.loads.append((identity, locator))
        return identity, locator, self.pages[identity, locator], True

    def attest(self, identity, metadata):
        self.attestations.append((identity, metadata))
        return self.metadata.get(identity) == metadata

    def add(self, evidence, *texts, topic='root', workspace='a', note=''):
        identity = Identity(workspace, topic + '-' + evidence, 'a' * 64, 'historical-revision')
        metadata = Metadata('Independent author', 'Historical source')
        self.metadata[identity] = metadata
        anchors = []
        for position, raw in enumerate(texts, 1):
            locator = Locator('logical_segment', position)
            self.pages[identity, locator] = raw
            anchors.append(make_anchor(identity, locator, raw, 0, len(raw), metadata, text_verified=True))
        self.store(workspace).save(topic, evidence, tuple(anchors), note)
        return tuple(anchors)

    def tag(self, topic, evidence, name, workspace='a'):
        self.db.execute('INSERT OR IGNORE INTO research_tag(workspace,id,name) VALUES(?,?,?)',
                        (workspace, name, name))
        self.db.execute('INSERT INTO research_evidence_tag VALUES(?,?,?,?)',
                        (workspace, topic, evidence, name))

    def search(self, query, **options):
        return service.search_notebook(self.store(), 'a', query, **options)

    def close(self):
        self.db.rollback()
        self.storage.close()


@pytest.fixture
def notebook(tmp_path):
    notebook = Notebook(tmp_path / 'library.db')
    try:
        yield notebook
    finally:
        notebook.close()


def keys(result):
    return tuple((hit.evidence.topic_id, hit.evidence.evidence_id) for hit in result.hits)


def test_nfc_case_phrase_matches_preserve_exact_quote_and_separate_note(notebook):
    n = notebook
    raw = 'Railway history — І\u0308жак, cita\u0301cia.\r\nExact original.'
    anchors = n.add('unicode', raw, note='Research: railway HISTORY and їжак.')
    n.add('foreign', raw, workspace='b')
    result = n.search('RAILWAY history')
    hit, = result.hits
    assert hit.matched_fields == ('quote', 'note')
    assert hit.evidence.anchors == anchors and hit.evidence.anchor.quote == raw
    assert hit.evidence.note == 'Research: railway HISTORY and їжак.'
    assert hit.topic_path == ('Research',)
    assert (result.total_matches, result.evidence_scanned, result.topics_scanned) == (1, 1, 1)
    assert not result.truncated
    for query in ('ЇЖАК', 'і\u0308жак', 'citácia', 'cita\u0301cia'):
        assert n.search(query, field='quote').hits[0].evidence.anchors == anchors
    assert n.search('Research', field='quote').hits == ()
    assert n.search('Research', field='note').hits[0].matched_fields == ('note',)
    with pytest.raises(FrozenInstanceError):
        hit.archived = True
    with pytest.raises(FrozenInstanceError):
        result.total_matches = 0


def test_phrase_never_crosses_anchor_or_quote_note_boundaries(notebook):
    n = notebook
    anchors = n.add('span', 'The first railway', 'archives remain exact.', note='Separate research')
    n.add('other', 'Last quote token', note='note first token')
    assert n.search('railway archives').hits == ()
    assert n.search('token note').hits == ()
    assert n.search('exact Separate').hits == ()
    assert n.search('first railway', field='quote').hits[0].evidence.anchors == anchors
    assert n.search('archives remain', field='quote').hits[0].evidence.anchors == anchors


@pytest.mark.parametrize('query', ['"railway" OR secret', 'rail*', 'note:word', '"', '---'])
def test_fts_operators_are_literal_phrase_text(notebook, query):
    notebook.add('text', 'Railway archive and secret', note='Note only word')
    assert notebook.search(query).hits == ()


def test_exact_tag_filters_before_verification_and_empty_tag_scope_is_honest(notebook):
    n = notebook
    good, = n.add('good', 'Railway source', note='Matching note')
    bad, = n.add('bad', 'Unrelated source', topic='child')
    n.tag('root', 'good', 'rail')
    n.tag('root', 'good', 'Україна')
    n.tag('child', 'bad', 'railway')
    n.pages[bad.identity, bad.locator] += ' tampered'
    n.loads.clear()
    result = n.search('', tag='rail')
    hit, = result.hits
    assert hit.evidence.anchor == good and hit.matched_fields == ()
    assert hit.tags == ('rail', 'Україна')
    assert (result.evidence_scanned, result.topics_scanned) == (1, 1)
    assert n.loads == [(good.identity, good.locator)]
    for tag in ('Rail', 'rai', 'missing'):
        result = n.search('', tag=tag)
        assert (result.hits, result.total_matches, result.evidence_scanned, result.topics_scanned) == ((), 0, 0, 0)
    with pytest.raises(ValueError):
        n.search('Matching note', limit=1)


def test_exact_totals_aggregate_pages_and_fields_and_limit_is_deterministic(notebook):
    n = notebook
    n.add('same', 'Archive record one', 'Archive record two', note='Archive record note')
    n.add('same', 'Archive record three', topic='child', note='Archive record note')
    n.add('nonmatch', 'Different text', topic='child')
    expected = {('root', 'same'), ('child', 'same')}
    complete = n.search('Archive record')
    assert set(keys(complete)) == expected
    assert all(h.matched_fields == ('quote', 'note') for h in complete.hits)
    assert (complete.total_matches, complete.evidence_scanned, complete.topics_scanned) == (2, 3, 2)
    limited = n.search('Archive record', limit=1)
    assert limited.hits == complete.hits[:1]
    assert limited.total_matches == 2 and limited.truncated
    assert n.search('Archive record', limit=1) == limited
    assert not complete.truncated


def test_eligible_nonmatching_corruption_fails_before_partial_results(notebook):
    n = notebook
    n.add('a-match', 'Found phrase')
    other, = n.add('z-nonmatch', 'Unrelated passage')
    n.metadata[other.identity] = Metadata('Changed author', 'Wrong source')
    with pytest.raises(ValueError, match='metadata'):
        n.search('Found phrase', limit=1)


def test_archived_ancestor_visibility_and_fresh_getter_authorization(notebook):
    n = notebook
    n.add('record', 'Historical archive', topic='child')
    TopicTree(n.db, 'a').archive('root')
    assert n.search('Historical').hits == ()
    n.loads.clear()
    with pytest.raises(ValueError, match='archived'):
        service.get_notebook_hit(n.store(), 'a', 'child', 'record')
    assert not n.loads
    hit, = n.search('Historical', include_archived=True).hits
    assert hit.archived and hit.topic_path == ('Research', 'Українські джерела')
    fresh = service.get_notebook_hit(n.store(), 'a', 'child', 'record', include_archived=True)
    assert fresh.evidence == hit.evidence and fresh.matched_fields == () and fresh.archived


def test_global_preflight_bounds_include_all_topics_before_any_source_read(notebook, monkeypatch):
    n = notebook
    n.add('one', 'First matching record')
    n.add('two', 'Second matching record', topic='child')
    n.tag('root', 'one', 'Only one')
    n.loads.clear()
    monkeypatch.setattr(service, 'MAX_ROWS', 1)
    with pytest.raises(ValueError, match='search budget'):
        n.search('matching', limit=1)
    assert n.loads == []
    assert n.search('matching', tag='Only one').evidence_scanned == 1
    n.loads.clear()
    monkeypatch.setattr(service, 'MAX_ROWS', 5000)
    monkeypatch.setattr(service, 'MAX_BYTES', 1)
    with pytest.raises(ValueError, match='search budget'):
        n.search('matching')
    assert n.loads == []


@pytest.mark.parametrize('options', [
    {'field': 'author'}, {'limit': 0}, {'limit': 101}, {'limit': True}, {'limit': 2.0},
    {'include_archived': 1}, {'tag': ''}, {'tag': ' '}, {'tag': 'x\x00y'},
    {'timeout': 0}, {'timeout': -1}, {'timeout': 61}, {'timeout': True},
    {'timeout': float('nan')}, {'timeout': float('inf')}, {'cancelled': None},
])
def test_invalid_options_are_rejected(notebook, options):
    with pytest.raises(ValueError):
        notebook.search('query', **options)


@pytest.mark.parametrize('query', ['', ' \t\n', 'x\x00y', '\ud800', 'q' * 501, None])
def test_invalid_or_unfiltered_empty_queries_are_rejected(notebook, query):
    with pytest.raises(ValueError):
        notebook.search(query)


def test_cancellation_during_a_span_is_latched_and_never_returns_partial(notebook):
    n = notebook
    n.add('span', 'First archive page', 'Second archive page')
    store = n.store()
    n.loads.clear()
    cancelled_once = False

    def cancel_after_first_source():
        nonlocal cancelled_once
        if n.loads and not cancelled_once:
            cancelled_once = True
            return True
        return False

    with pytest.raises(TimeoutError):
        service.search_notebook(store, 'a', 'archive', cancelled=cancel_after_first_source)
    assert len(n.loads) == 1 and cancelled_once
    n.loads.clear()
    with pytest.raises(TimeoutError):
        n.search('archive', cancelled=lambda: True)
    assert n.loads == []
    assert n.search('archive').total_matches == 1


def test_deadline_includes_metadata_and_final_result_materialization(notebook, monkeypatch):
    n = notebook
    n.add('record', 'Exact source')
    now = [61.001]
    monkeypatch.setattr(service, 'monotonic', lambda: now[0])
    store = n.store()
    original = store.attest_metadata

    def delayed_metadata(*args):
        accepted = original(*args)
        now[0] += 2
        return accepted

    store.attest_metadata = delayed_metadata
    n.loads.clear()
    with pytest.raises(TimeoutError):
        service.search_notebook(store, 'a', 'Exact', timeout=1)
    assert n.loads == []
    now[0] = 61.001
    original_tags = service._tags

    def delayed_tags(*args):
        tags = original_tags(*args)
        now[0] += 2
        return tags

    monkeypatch.setattr(service, '_tags', delayed_tags)
    with pytest.raises(TimeoutError):
        n.search('Exact', timeout=1)


def test_fresh_getter_rechecks_exact_row_and_never_loads_a_whole_topic(notebook):
    n = notebook
    good, = n.add('good', 'Original quote', note='Original note')
    bad, = n.add('bad', 'Separate quote')
    n.pages[bad.identity, bad.locator] += ' changed'
    n.loads.clear()
    store = n.store()
    first = service.get_notebook_hit(store, 'a', 'root', 'good')
    assert n.loads == [(good.identity, good.locator)]
    store.edit_note('root', 'good', 'Updated note')
    n.tag('root', 'good', 'New tag')
    TopicTree(n.db, 'a').rename('root', 'New name')
    current = service.get_notebook_hit(store, 'a', 'root', 'good')
    assert current.evidence.anchor == good and current.evidence.note == 'Updated note'
    assert current.topic_path == ('New name',) and current.tags == ('New tag',)
    assert first.evidence.note == 'Original note'
    n.pages[good.identity, good.locator] += ' changed'
    with pytest.raises(ValueError):
        service.get_notebook_hit(store, 'a', 'root', 'good')


def test_many_records_from_one_source_are_verified_once_each(notebook):
    n = notebook
    anchor, = n.add('000', 'Shared historical source', note='Repeated research note')
    store = n.store()
    for position in range(1, 64):
        store.save('root', f'{position:03}', anchor, 'Repeated research note')
    n.loads.clear()
    n.attestations.clear()
    result = n.search('Repeated research', field='note', limit=5)
    assert result.total_matches == result.evidence_scanned == 64
    assert result.topics_scanned == 1 and result.truncated and len(result.hits) == 5
    assert len(n.loads) == len(n.attestations) == 64
    assert all(hit.evidence.anchor == anchor for hit in result.hits)


def test_fresh_getter_bounds_untrusted_note_before_source_load(notebook):
    n = notebook
    n.add('record', 'Exact source')
    # SQLite length(text) stops at NUL, so byte preflight must cover its suffix.
    n.db.execute('UPDATE research_evidence SET note=? WHERE workspace=? AND topic=? AND id=?',
                 ('\x00' + 'x' * 400000, 'a', 'root', 'record'))
    n.loads.clear()
    with pytest.raises(ValueError, match='snapshot size'):
        service.get_notebook_hit(n.store(), 'a', 'root', 'record')
    assert n.loads == []


def test_scope_and_consistent_transaction_are_required(notebook, monkeypatch):
    n = notebook
    n.add('record', 'Exact source')
    with pytest.raises(ValueError, match='workspace'):
        service.search_notebook(n.store(), 'b', 'Exact')
    with pytest.raises(ValueError):
        service.get_notebook_hit(n.store(), 'a', 'child', 'record')
    store = n.store()
    original = store.get_saved
    monkeypatch.setattr(store, 'get_saved', lambda *args, **kwargs:
                        replace(original(*args, **kwargs), topic_id='child'))
    with pytest.raises(ValueError, match='scope'):
        service.search_notebook(store, 'a', 'Exact')
    n.db.commit()
    with pytest.raises(ValueError, match='transaction'):
        n.search('Exact')
