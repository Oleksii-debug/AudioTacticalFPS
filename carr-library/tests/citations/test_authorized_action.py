"""Disk SQLite integration; review/authorization rows are synthetic fixtures."""
from contextlib import closing, contextmanager
from dataclasses import replace
import sqlite3

import pytest

from carr_library.citations.anchors import Identity, LabelCorrection, Locator, Metadata, digest, make_anchor
from carr_library.citations.historical import historical_loader
from carr_library.citations.printed_label_attestation import printed_label_guard
from carr_library.citations.authorized_action import AuthorizedSelection, copy_authorized_span
from carr_library.research.span_snapshot import seal_span


@pytest.fixture
def library(tmp_path):
    path = tmp_path / 'fixture.db'
    raw = ('Évidence — один\n', 'Другий уривок\n')
    ident = Identity('w', 'edition', 'a'*64, 'rev')
    meta = Metadata('Author A', 'Title A', '1', 'first')
    locs = [Locator('physical_page', 1, 'iv', True), Locator('physical_page', 2)]
    anchors = [make_anchor(ident, loc, text, 0, len(text), meta, text_verified=True)
               for loc, text in zip(locs, raw)]
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript('''CREATE TABLE revision(workspace,edition,asset,id);
        CREATE TABLE sealed(revision);
        CREATE TABLE page(revision,ordinal,physical,printed,verified,raw,digest);
        CREATE TABLE approval(physical,raw_hash,active);
        CREATE TABLE meta_approval(active);
        CREATE TABLE label_approval(physical,printed,active);
        CREATE TABLE saved(workspace,topic,evidence,payload,checksum,allowed,note);
        ''')
        db.execute('INSERT INTO revision VALUES(?,?,?,?)', ('w','edition','a'*64,'rev'))
        db.execute('INSERT INTO sealed VALUES(?)', ('rev',))
        db.executemany('INSERT INTO page VALUES(?,?,?,?,?,?,?)', [
            ('rev',i,i,loc.printed,int(loc.verified),text,digest(text))
            for i,(loc,text) in enumerate(zip(locs,raw),1)])
        db.executemany('INSERT INTO approval VALUES(?,?,1)', [(i,digest(t)) for i,t in enumerate(raw,1)])
        db.execute('INSERT INTO meta_approval VALUES(1)')
        db.execute('INSERT INTO label_approval VALUES(1,?,1)', ('iv',))

    def readers(db, workspace):
        def text_ok(i, loc, h):
            return i == ident and db.execute('SELECT 1 FROM approval WHERE physical=? AND raw_hash=? AND active=1', (loc.index,h)).fetchone() == (1,)
        def meta_ok(i,m):
            return i == ident and m == meta and db.execute('SELECT active FROM meta_approval').fetchone() == (1,)
        def label_ok(i, physical):
            rows = db.execute('SELECT printed FROM label_approval WHERE physical=? AND active=1', (physical,)).fetchmany(2)
            if i != ident or len(rows) != 1:
                return None
            return LabelCorrection(i, Locator('physical_page', physical),
                                   Locator('physical_page', physical, rows[0][0], True),
                                   'synthetic label review', 'fixture reviewer', 'b'*64)
        return historical_loader(db, workspace, printed_label_guard(workspace, text_ok, label_ok)), meta_ok

    with closing(sqlite3.connect(path)) as db, db:
        db.execute('BEGIN')
        payload, checksum = seal_span('w', anchors, *readers(db, 'w'))
        db.execute('INSERT INTO saved VALUES(?,?,?,?,?,1,?)', ('w','topic','e',payload,checksum,'note'))

    @contextmanager
    def open_read(workspace, topic, evidence):
        with closing(sqlite3.connect(path)) as db:
            db.execute('BEGIN')
            rows = db.execute('SELECT workspace,topic,evidence,payload,checksum FROM saved WHERE workspace=? AND topic=? AND evidence=? AND allowed=1', (workspace,topic,evidence)).fetchmany(2)
            yield AuthorizedSelection(*rows[0], *readers(db, workspace)) if len(rows)==1 else None

    def change(sql, args=()):
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(sql,args)

    return open_read, checksum, change, raw


def copy(lib, position=0, mode='both', workspace='w', topic='topic', evidence='e', reader=None):
    open_read, checksum, _, _ = lib
    return copy_authorized_span(workspace, topic, evidence, checksum, position, mode, reader or open_read)


def test_exact_and_unknown_page(library):
    assert copy(library,0,'quote') == library[3][0]
    assert copy(library,0,'citation') == 'Author A, Title A, vol. 1, edition first, p. iv (physical page 1)'
    assert copy(library,1) == library[3][1] + '\n\nCitation: Author A, Title A, vol. 1, edition first, Physical page 2 (printed page unverified)'


@pytest.mark.parametrize('sql', ['UPDATE saved SET allowed=0', 'DELETE FROM saved',
    'UPDATE saved SET topic="other"', 'UPDATE meta_approval SET active=0',
    'UPDATE approval SET active=0 WHERE physical=2',
    'UPDATE label_approval SET active=0', 'UPDATE label_approval SET printed="999"'])
def test_fresh_action_denies_revocation(library, sql):
    assert copy(library)
    library[2](sql)
    with pytest.raises(ValueError):
        copy(library)


def test_changed_selection_and_note(library):
    library[2]('UPDATE saved SET note="new"')
    assert copy(library,0,'quote') == library[3][0]
    library[2]('UPDATE saved SET checksum=?', ('b'*64,))
    with pytest.raises(ValueError, match='selection changed'):
        copy(library)


@pytest.mark.parametrize('kwargs', [{'workspace':'foreign'}, {'topic':'other'}, {'evidence':'other'}])
def test_scope(library, kwargs):
    with pytest.raises(ValueError, match='not authorized'):
        copy(library, **kwargs)


def test_reader_identity_and_exit_failure(library):
    @contextmanager
    def wrong(*args):
        with library[0](*args) as selection:
            yield replace(selection, evidence='other')
    with pytest.raises(ValueError, match='not authorized'):
        copy(library,reader=wrong)

    @contextmanager
    def failed(*args):
        with library[0](*args) as selection:
            yield selection
            raise RuntimeError('transaction failed')
    with pytest.raises(RuntimeError, match='transaction failed'):
        copy(library,reader=failed)


def test_invalid_action(library):
    with pytest.raises(ValueError):
        copy(library,position=True)
    with pytest.raises(IndexError):
        copy(library,position=2)
    with pytest.raises(ValueError):
        copy(library,mode='all')
