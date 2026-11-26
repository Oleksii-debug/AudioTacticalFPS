"""Service-level assembly: actual lane code, synthetic DB/host policy, no wx."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

import pytest
from carr_library.catalog.membership import Catalog
from carr_library.citations.anchors import Identity, Locator, Metadata, digest, make_anchor, format_citation
from carr_library.reader.context import open_context
from carr_library.research.topic_tree import TopicTree
from carr_library.search.literal import search as literal_search
from carr_library.ui.scope_bindings import bind_scope_services
from carr_library.ui.search_model import Hit, SearchModel


class FixtureHost:
    """Synthetic attestation and ACL only; not an application migration or provider."""
    def __init__(self, path):
        self.path = path
        self.allowed = {('a', 't'), ('b', 't'), ('a', None)}
        with self.connect() as db:
            db.execute('CREATE TABLE workspace(id TEXT PRIMARY KEY)')
            root = Path(__file__).resolve().parents[1]
            for name in ('catalog/catalog_membership_candidate.sql',
                         'reader/topic_tree_candidate.sql', 'reader/saved_evidence_candidate.sql'):
                db.executescript((root / name).read_text())
            db.execute('CREATE TABLE active_page(id,workspace,edition,revision,asset,ordinal,physical,printed,verified,raw,digest)')
            db.executemany('INSERT INTO workspace VALUES(?)', [('a',), ('b',)])
            for w in ('a', 'b'):
                c = Catalog(db, w)
                c.add_work('work', 'Same title', [w])
                c.add_edition('edition', 'work', dict(sha256=w*64, mime='text/plain', size=10))
                c.create_collection('group', w)
                c.assign('group', 'edition')
                TopicTree(db, w).create('t', w)
                raw = 'Café and історія. ' + w
                db.execute('INSERT INTO active_page VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                           ('page', w, 'edition', 'revision', w*64, 1, None, None, 0, raw, digest(raw)))

    def connect(self):
        db = sqlite3.connect(self.path)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA recursive_triggers=ON')
        return db

    @contextmanager
    def transaction(self, workspace, topic):
        db = self.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            if (workspace, topic) not in self.allowed:
                raise PermissionError('revoked')
            yield db
            if (workspace, topic) not in self.allowed:
                raise PermissionError('revoked')
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def metadata(self, db, identity, value):
        return value == Metadata(identity.workspace, 'Same title')

    def page(self, db, identity, locator):
        raw, = db.execute('SELECT raw FROM active_page WHERE workspace=? AND edition=? AND revision=? AND asset=?',
                          (identity.workspace, identity.edition_id, identity.revision, identity.asset_sha256)).fetchone()
        return identity, locator, raw, True

    def anchor(self, db, workspace, ref, revision):
        page, start, end = ref.split(':')
        edition, asset, raw = db.execute('SELECT edition,asset,raw FROM active_page WHERE workspace=? AND id=? AND revision=?',
                                         (workspace, page, revision)).fetchone()
        return make_anchor(Identity(workspace, edition, asset, revision), Locator('logical_segment', 1),
                           raw, int(start), int(end), Metadata(workspace, 'Same title'), text_verified=True)

    def search(self, db, workspace, query, mode, result, limit, *, collection='group'):
        assert (mode, result) == ('literal', 'short')
        ids = [r[0] for r in Catalog(db, workspace).editions(collection)]
        for row in literal_search(db, workspace, query, edition_ids=ids, limit=limit):
            ref = f"{row['page_id']}:{row['start']}:{row['end']}"
            anchor = self.anchor(db, workspace, ref, row['revision_id'])
            assert anchor.quote == row['raw_quote']
            yield Hit(workspace, ref, anchor.identity.revision, anchor.metadata.title, workspace,
                      'unknown', anchor.locator.display(), anchor.quote, format_citation(anchor))

    def context(self, db, workspace, ref, revision):
        anchor = self.anchor(db, workspace, ref, revision)
        return open_context(workspace, [anchor], lambda i, loc: self.page(db, i, loc))

    def copy(self, db, workspace, ref, revision, field):
        return self.context(db, workspace, ref, revision).clipboard(
            workspace, 0, field, lambda i, loc: self.page(db, i, loc))

    def bind(self, workspace='a', topic='t', **changes):
        callbacks = dict(authorized_transaction=self.transaction, search=self.search,
                         load_context=self.context, verified_copy=self.copy,
                         load_page=self.page, attest_metadata=self.metadata)
        callbacks.update(changes)
        return bind_scope_services(workspace, topic, **callbacks)


@pytest.fixture
def host(tmp_path):
    return FixtureHost(tmp_path / 'library.sqlite')


def test_real_service_flow(host):
    s = host.bind()
    with s.catalog_tx() as c:
        assert s.collections(c) == (('group', 'a'),)
        assert c.editions('group')[0][2] == 'a'*64
    with s.topic_tx() as t:
        t.create('child', 'Child', parent='t')
    model = SearchModel()
    model.set_workspace('a')
    model.replace('a', s.search('a', 'CAFÉ', 'literal', 'short', 10))
    assert model.row_count() == 1
    hit = model.hit(0)
    assert hit.quote == 'Café'
    page = s.load_context('a', hit.ref, hit.revision).page(0)
    assert page.raw_text.endswith('a')
    assert s.verified_copy('a', hit.ref, hit.revision, 'citation') == hit.citation
    assert s.verified_copy('a', hit.ref, hit.revision, 'quote') == hit.quote
    with s.evidence_tx() as store:
        store.save('t', 'e', page.anchor, 'note')
    with s.evidence_tx() as store:
        store.edit_note('t', 'e', 'updated')
    with host.bind().evidence_tx() as store:
        saved, = store.list_saved('t')
        assert saved.anchor == page.anchor and saved.note == 'updated'
    with host.bind('b').evidence_tx() as store:
        assert store.list_saved('t') == ()


def test_membership_search_snapshot(host):
    s = host.bind()
    with s.catalog_tx() as c:
        assert c.remove('group', 'edition')
    assert s.search('a', 'історія', 'literal', 'short', 10) == ()
    assert host.bind('b').search('b', 'історія', 'literal', 'short', 10)[0].workspace == 'b'


def test_revocation_all_callbacks(host):
    s = host.bind()
    hit, = s.search('a', 'Café', 'literal', 'short', 10)
    host.allowed.remove(('a', 't'))
    for factory in (s.catalog_tx, s.topic_tx, s.evidence_tx):
        with pytest.raises(PermissionError), factory():
            pytest.fail('revoked service yielded')
    for fn, args in [(s.search, ('a', 'Café', 'literal', 'short', 10)),
                     (s.load_context, ('a', hit.ref, hit.revision)),
                     (s.verified_copy, ('a', hit.ref, hit.revision, 'quote'))]:
        with pytest.raises(PermissionError):
            fn(*args)


def test_foreign_request_and_result(host):
    s = host.bind()
    with pytest.raises(ValueError):
        s.search('b', 'Café', 'literal', 'short', 10)
    foreign = host.bind('b').search('b', 'Café', 'literal', 'short', 10)
    with pytest.raises(ValueError):
        host.bind(search=lambda *args: iter(foreign)).search('a', 'Café', 'literal', 'short', 10)
    with pytest.raises(ValueError):
        s.verified_copy('a', 'ref', 'revision', 'both')


def test_rollback_on_revocation_during_operation(host):
    s = host.bind()
    with pytest.raises(PermissionError), s.topic_tx() as t:
        t.create('child', 'Child', parent='t')
        host.allowed.remove(('a', 't'))
    host.allowed.add(('a', 't'))
    with s.topic_tx() as t:
        assert [row.id for row in t.list_topics()] == ['t']


def test_bad_transaction_and_missing_scope(host):
    @contextmanager
    def no_transaction(*args):
        db = host.connect()
        try:
            yield db
        finally:
            db.close()
    with pytest.raises(ValueError):
        host.bind(authorized_transaction=no_transaction)
    host.allowed.add(('a', 'missing'))
    with pytest.raises(ValueError):
        host.bind(topic='missing')
    assert host.bind(topic=None).evidence_tx is None


def test_source_drift_blocks_saved_evidence(host):
    s = host.bind()
    hit, = s.search('a', 'Café', 'literal', 'short', 10)
    anchor = s.load_context('a', hit.ref, hit.revision).page(0).anchor
    with s.evidence_tx() as store:
        store.save('t', 'e', anchor)
    with host.connect() as db:
        db.execute("UPDATE active_page SET raw='Changed' WHERE workspace='a'")
    with pytest.raises(ValueError), s.evidence_tx() as store:
        store.list_saved('t')


def test_displayed_hit_saved_in_one_authorized_transaction(host):
    opened = []

    @contextmanager
    def counted(workspace, topic):
        with host.transaction(workspace, topic) as db:
            opened.append(db)
            yield db

    s = host.bind(authorized_transaction=counted)
    hit, = s.search('a', 'Café', 'literal', 'short', 10)
    opened.clear()
    key = s.save_hit('a', hit.ref, hit.revision, hit.quote, hit.citation)
    assert len(opened) == 1
    with host.bind().evidence_tx() as store:
        saved, = store.list_saved('t')
        assert saved.evidence_id == key
        assert saved.anchor.quote == hit.quote and saved.note == ''
    with host.bind('b').evidence_tx() as store:
        assert store.list_saved('t') == ()


@pytest.mark.parametrize('fault', ['quote', 'citation', 'revision', 'scope', 'empty', 'ambiguous'])
def test_changed_or_invalid_display_never_saves(host, fault):
    s = host.bind()
    hit, = s.search('a', 'Café', 'literal', 'short', 10)
    args = ['a', hit.ref, hit.revision, hit.quote, hit.citation]
    if fault == 'quote':
        args[3] += ' altered'
    elif fault == 'citation':
        args[4] += ' altered'
    elif fault == 'revision':
        args[2] = 'other-revision'
    elif fault == 'scope':
        args[0] = 'b'
    elif fault == 'empty':
        args[1] = ''
    else:
        from carr_library.reader.context import Context

        def duplicated(db, *identity):
            page = host.context(db, *identity).page(0)
            return Context((page, page))

        s = host.bind(load_context=duplicated)
    with pytest.raises((ValueError, TypeError)):
        s.save_hit(*args)
    with host.bind().evidence_tx() as store:
        assert store.list_saved('t') == ()


def test_save_rolls_back_if_permission_revoked_during_attestation(host):
    s = host.bind()
    hit, = s.search('a', 'Café', 'literal', 'short', 10)

    def revoke(db, identity, metadata):
        valid = host.metadata(db, identity, metadata)
        host.allowed.remove(('a', 't'))
        return valid

    s = host.bind(attest_metadata=revoke)
    with pytest.raises(PermissionError):
        s.save_hit('a', hit.ref, hit.revision, hit.quote, hit.citation)
    host.allowed.add(('a', 't'))
    with host.bind().evidence_tx() as store:
        assert store.list_saved('t') == ()


def test_collection_selection_and_search_recheck_live_membership(host):
    s = host.bind(collection_search=host.search)
    s.select_collection('a', 'group')
    assert len(s.search('a', 'Café', 'literal', 'short', 10)) == 1
    with s.catalog_tx() as catalog:
        catalog.remove('group', 'edition')
    assert s.search('a', 'Café', 'literal', 'short', 10) == ()
    s.select_collection('a', None)
    assert len(s.search('a', 'Café', 'literal', 'short', 10)) == 1
    s.select_collection('a', 'group')
    with s.catalog_tx() as catalog:
        catalog.archive('group')
    with pytest.raises(ValueError):
        s.search('a', 'Café', 'literal', 'short', 10)
    with pytest.raises(ValueError):
        s.select_collection('a', 'group')
    with pytest.raises(ValueError):
        s.select_collection('b', None)
    host.allowed.remove(('a', 't'))
    with pytest.raises(PermissionError):
        s.select_collection('a', None)


def test_library_only_exposes_no_save_action(host):
    assert host.bind(topic=None).save_hit is None


def test_preview_callback_shares_live_authorized_collection_snapshot(host):
    visits = []

    def preview(db, workspace, *args, collection):
        visits.append((db.in_transaction, workspace, collection))
        return host.search(db, workspace, *args, collection=collection)

    s = host.bind(collection_search=host.search, preview_search=preview)
    assert host.bind().preview_search is None
    s.select_collection('a', 'group')
    assert len(s.preview_search('a', 'Café', 'literal', 'short', 10)) == 1
    assert visits == [(True, 'a', 'group')]
    with s.catalog_tx() as catalog:
        catalog.remove('group', 'edition')
    assert s.preview_search('a', 'Café', 'literal', 'short', 10) == ()
    with pytest.raises(ValueError, match='scope mismatch'):
        s.preview_search('b', 'Café', 'literal', 'short', 10)
    host.allowed.remove(('a', 't'))
    with pytest.raises(PermissionError):
        s.preview_search('a', 'Café', 'literal', 'short', 10)
