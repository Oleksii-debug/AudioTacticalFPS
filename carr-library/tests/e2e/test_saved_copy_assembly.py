"""CARR-15: exact candidate payload compatibility, real disk SQLite, synthetic authority."""
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3
import pytest
from carr_library.citations.anchors import Identity, Locator, Metadata, digest, make_anchor
from carr_library.citations.historical import historical_loader
from carr_library.citations.authorized_action import AuthorizedSelection, copy_authorized_span
from carr_library.research.topic_tree import TopicTree
from carr_library.research.saved_evidence import SavedEvidenceStore, _encode
from carr_library.research.span_snapshot import seal_span
from carr_library.research.anchored_export import export_saved


@pytest.fixture
def lib(tmp_path):
    path = tmp_path / 'library.sqlite'
    raw = ('Дослівний уривок\n', 'Druhý úryvok\n')
    identity = Identity('a', 'edition', 'a'*64, 'rev')
    metadata = Metadata('Author A', 'Same title', '1', 'first')
    anchors = tuple(make_anchor(identity, Locator('physical_page', i), text, 0,
                                len(text), metadata, text_verified=True)
                    for i, text in enumerate(raw, 1))

    @contextmanager
    def tx():
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN')
            yield db

    def readers(db, ws):
        def approve(i, loc, h):
            return i == identity and db.execute(
                'SELECT 1 FROM approval WHERE physical=? AND hash=? AND active=1',
                (loc.index, h)).fetchone() == (1,)
        def meta(i, m):
            return i == identity and m == metadata
        return historical_loader(db, ws, approve), meta

    with closing(sqlite3.connect(path)) as db, db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''CREATE TABLE workspace(id TEXT PRIMARY KEY);
        INSERT INTO workspace VALUES('a'),('b');
        CREATE TABLE revision(workspace,edition,asset,id);
        CREATE TABLE sealed(revision);
        CREATE TABLE page(revision,ordinal,physical,printed,verified,raw,digest);
        CREATE TABLE approval(physical,hash,active);''')
        for f in ('topic_tree_candidate.sql', 'saved_evidence_candidate.sql'):
            db.executescript((Path(__file__).parents[1] / 'reader' / f).read_text())
        db.execute('INSERT INTO revision VALUES(?,?,?,?)', ('a','edition','a'*64,'rev'))
        db.execute('INSERT INTO sealed VALUES(?)', ('rev',))
        for i, text in enumerate(raw, 1):
            db.execute('INSERT INTO page VALUES(?,?,?,?,?,?,?)', ('rev',i,i,None,0,text,digest(text)))
            db.execute('INSERT INTO approval VALUES(?,?,1)', (i,digest(text)))
        for ws in ('a','b'):
            TopicTree(db, ws).create('topic', 'Research')
        payload = _encode(anchors[0])
        db.execute('INSERT INTO research_evidence VALUES(?,?,?,?,?,?)',
                   ('a','topic','single',payload,digest(payload),'note'))

    def add_span():
        with tx() as db:
            payload, checksum = seal_span('a', anchors, *readers(db,'a'))
            db.execute('INSERT INTO research_evidence VALUES(?,?,?,?,?,?)',
                       ('a','topic','span',payload,checksum,'span note'))
            return checksum

    @contextmanager
    def open_read(ws, topic, evidence):
        with tx() as db:
            TopicTree(db, ws)._visible(topic)
            row = db.execute('SELECT workspace,topic,id,payload,checksum FROM research_evidence '
                             'WHERE workspace=? AND topic=? AND id=?', (ws,topic,evidence)).fetchone()
            yield AuthorizedSelection(*row, *readers(db, ws)) if row else None

    def copy(evidence, pos=0, ws='a'):
        with tx() as db:
            checksum = db.execute('SELECT checksum FROM research_evidence WHERE workspace=? AND topic=? AND id=?',
                                  ('a','topic',evidence)).fetchone()[0]
        return copy_authorized_span(ws, 'topic', evidence, checksum, pos, 'quote', open_read)

    return tx, readers, raw, add_span, copy


def test_single_store_export_roundtrip(lib):
    tx, readers, raw, _, _ = lib
    with tx() as db:
        store = SavedEvidenceStore(db, 'a', *readers(db,'a'))
        store.edit_note('topic','single','edited note')
    with tx() as db:
        rows = SavedEvidenceStore(db, 'a', *readers(db,'a')).list_saved('topic')
        assert rows[0].anchor.quote == raw[0]
        assert rows[0].note == 'edited note'
        exported = export_saved('a','topic',rows,readers(db,'a')[0])
        assert 'Дослівний уривок' in exported and 'edited note' in exported
        assert SavedEvidenceStore(db,'b',*readers(db,'b')).list_saved('topic') == ()


def test_existing_saved_quote_copy_acceptance(lib):
    # Desired integration contract: previously saved exact evidence is copyable.
    assert lib[4]('single') == lib[2][0]


def test_new_span_copy_and_scope_revocation(lib):
    tx, _, raw, add_span, copy = lib
    add_span()
    assert copy('span',1) == raw[1]
    with pytest.raises(ValueError, match='not authorized'):
        copy('span',ws='b')
    with tx() as db:
        db.execute('UPDATE approval SET active=0 WHERE physical=2')
    with pytest.raises(ValueError, match='unverified extraction'):
        copy('span',0)


def test_new_span_and_existing_rows_browse_acceptance(lib):
    # Desired integrated catalog/evidence view: mixed valid saved rows are visible.
    tx, readers, _, add_span, _ = lib
    add_span()
    with tx() as db:
        rows = SavedEvidenceStore(db,'a',*readers(db,'a')).list_saved('topic')
        assert {r.evidence_id for r in rows} == {'single','span'}


def test_existing_immutable_payload_cannot_be_silently_rewritten(lib):
    tx, _, _, add_span, _ = lib
    add_span()
    with tx() as db:
        payload, checksum = db.execute("SELECT payload,checksum FROM research_evidence WHERE id='span'").fetchone()
        with pytest.raises(sqlite3.IntegrityError, match='immutable evidence'):
            db.execute("UPDATE research_evidence SET payload=?,checksum=? WHERE id='single'",(payload,checksum))


def test_archived_topic_denies_fresh_copy(lib):
    tx, _, _, add_span, copy = lib
    add_span()
    with tx() as db:
        TopicTree(db,'a').archive('topic')
    with pytest.raises(ValueError, match='archived topic'):
        copy('span')


def test_new_writer_and_legacy_bytes_stay_distinct(lib):
    import json
    tx, readers, raw, add_span, copy = lib
    with tx() as db:
        before = db.execute("SELECT payload,checksum FROM research_evidence WHERE id='single'").fetchone()
        store = SavedEvidenceStore(db, 'a', *readers(db,'a'))
        single, = store.list_saved('topic')
        assert single.checksum == before[1]
        store.save('topic', 'new-single', single.anchor, 'new note')
        data = json.loads(db.execute("SELECT payload FROM research_evidence WHERE id='new-single'").fetchone()[0])
        assert data['version'] == 1 and len(data['anchors']) == 1
        store.list_saved('topic')
        assert db.execute("SELECT payload,checksum FROM research_evidence WHERE id='single'").fetchone() == before
    assert copy('new-single') == raw[0]


def test_span_browse_export_search_all_pages_without_joining(lib):
    from carr_library.research.topic_search import search_topic
    tx, readers, raw, add_span, copy = lib
    add_span()
    with tx() as db:
        store = SavedEvidenceStore(db, 'a', *readers(db,'a'))
        rows = store.list_saved('topic')
        span = next(r for r in rows if r.evidence_id == 'span')
        assert tuple(a.quote for a in span.anchors) == raw
        assert [h.evidence.evidence_id for h in search_topic(store,'a','topic','Druhý')] == ['span']
        assert search_topic(store,'a','topic','уривок Druhý') == ()
        out = export_saved('a','topic',rows,readers(db,'a')[0])
        assert 'source part 1 of 2' in out and 'source part 2 of 2' in out
        assert 'Druhý úryvok' in out
        # Each quotation has its own field; no joined page text is fabricated.
        assert raw[0] + raw[1] not in out
        store.save_span('topic', 'saved-span', span.anchors, 'new span')
    assert copy('saved-span',1) == raw[1]


def test_both_formats_revalidate_revocation_and_original_checksum(lib):
    from carr_library.research.span_snapshot import restore_span
    tx, readers, raw, add_span, copy = lib
    add_span()
    with tx() as db:
        rows = db.execute('SELECT id,payload,checksum FROM research_evidence').fetchall()
        for _, payload, checksum in rows:
            with pytest.raises(ValueError, match='drift'):
                restore_span('a',payload,'b'*64,*readers(db,'a'))
        db.execute('UPDATE approval SET active=0 WHERE physical=1')
    for eid, _, _ in rows:
        with pytest.raises(ValueError, match='unverified extraction'):
            copy(eid)
