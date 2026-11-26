"""Cross-candidate regression with real PDF rendering and reopened SQLite.

Approval rows below are synthetic trusted fixtures, not a production trust store.
"""
from contextlib import contextmanager, closing
from hashlib import sha256
from io import BytesIO
import json
import sqlite3

import pytest
from pypdf import PdfReader
from reportlab.pdfgen.canvas import Canvas

from carr_library.citations.anchors import (Identity, LabelCorrection, Locator,
    Metadata, digest, make_anchor)
from carr_library.citations.historical import historical_loader
from carr_library.citations.pdf_review_attestation import (ReviewedPage,
    pdf_review_attestor, render_fingerprint)
from carr_library.citations.printed_label_attestation import printed_label_guard
from carr_library.research.span_snapshot import seal_span, restore_span


@pytest.fixture
def library(tmp_path):
    out = BytesIO()
    canvas = Canvas(out, pagesize=(300, 300), invariant=1)
    for text, label in [('First synthetic page', 'iv'), ('Second synthetic page', 'v')]:
        canvas.drawString(20, 230, text)
        canvas.drawString(140, 20, label)
        canvas.showPage()
    canvas.save()
    data = out.getvalue()
    original = tmp_path / 'original.pdf'
    original.write_bytes(data)
    identity = Identity('a', 'edition', sha256(data).hexdigest(), 'historical')
    metadata = Metadata('Fixture author', 'Fixture title', '1', 'first')
    path = tmp_path / 'library.db'
    anchors = []
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript('''CREATE TABLE revision(workspace,edition,asset,id);
        CREATE TABLE sealed(revision);
        CREATE TABLE page(revision,ordinal,physical,printed,verified,raw,digest);
        CREATE TABLE trusted_review(physical PRIMARY KEY,raw_hash,pixels,renderer,label,text_ok,label_ok);
        CREATE TABLE trusted_metadata(author,title,volume,edition,active);
        CREATE TABLE snapshot(workspace,topic,payload,checksum);
        ''')
        db.execute('INSERT INTO revision VALUES(?,?,?,?)', (identity.workspace,identity.edition_id,identity.asset_sha256,identity.revision))
        db.execute('INSERT INTO sealed VALUES(?)', (identity.revision,))
        db.execute('INSERT INTO trusted_metadata VALUES(?,?,?,?,1)', (metadata.author,metadata.title,metadata.volume,metadata.edition))
        for n, (page, label) in enumerate(zip(PdfReader(BytesIO(data)).pages, ('iv','v')), 1):
            raw = page.extract_text()
            assert raw == ('First synthetic page\niv\n' if n == 1 else 'Second synthetic page\nv\n')
            loc = Locator('physical_page', n, label, True)
            renderer, pixels = render_fingerprint(data, n)
            db.execute('INSERT INTO page VALUES(?,?,?,?,?,?,?)', (identity.revision,n,n,label,1,raw,digest(raw)))
            db.execute('INSERT INTO trusted_review VALUES(?,?,?,?,?,1,1)', (n,digest(raw),pixels,renderer,label))
            anchors.append(make_anchor(identity,loc,raw,6 if n == 1 else 0,len(raw) if n == 1 else 6,metadata,text_verified=True))

    @contextmanager
    def reader():
        with closing(sqlite3.connect(path)) as db:
            db.execute('BEGIN')

            def review(i, n):
                row = db.execute('SELECT raw_hash,pixels,renderer FROM trusted_review WHERE physical=? AND text_ok=1', (n,)).fetchone()
                return ReviewedPage(i,n,*row,'fixture') if i == identity and row else None

            def label(i, n):
                row = db.execute('SELECT label,pixels FROM trusted_review WHERE physical=? AND label_ok=1', (n,)).fetchone()
                return LabelCorrection(i,Locator('physical_page',n),Locator('physical_page',n,row[0],True),'fixture','fixture',row[1]) if i == identity and row else None

            def meta(i, m):
                return i == identity and db.execute('SELECT author,title,volume,edition FROM trusted_metadata WHERE active=1').fetchone() == (m.author,m.title,m.volume,m.edition)

            text = pdf_review_attestor('a', lambda i: original.read_bytes() if i == identity else None, review)
            load = historical_loader(db, 'a', printed_label_guard('a', text, label))
            yield db, load, meta

    with reader() as (db, load, meta):
        payload, checksum = seal_span('a', anchors, load, meta)
        db.execute('INSERT INTO snapshot VALUES(?,?,?,?)', ('a','topic',payload,checksum))
        db.commit()
    return path, reader


def stored(db, load, meta, workspace='a'):
    row = db.execute('SELECT payload,checksum FROM snapshot WHERE workspace=? AND topic=?', (workspace,'topic')).fetchone()
    if row is None:
        raise ValueError('missing authorized snapshot')
    return restore_span(workspace, *row, load, meta)


def change(path, sql):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(sql)


def test_reopened_exact_span(library):
    _, reader = library
    with reader() as (db, load, meta):
        ctx = stored(db, load, meta)
        assert [p.quote for p in ctx.pages] == ['synthetic page\niv\n','Second']
        assert ctx.clipboard('a',1,'both',load) == 'Second\n\nCitation: Fixture author, Fixture title, vol. 1, edition first, p. v (PDF page 2)'


@pytest.mark.parametrize('sql', [
    'UPDATE trusted_review SET text_ok=0 WHERE physical=2',
    'UPDATE trusted_review SET label_ok=0 WHERE physical=2',
    'UPDATE trusted_metadata SET active=0',
    'UPDATE page SET printed="999" WHERE physical=2',
])
def test_restore_denies_revoked_or_changed_source(library, sql):
    path, reader = library
    change(path, sql)
    with reader() as (db, load, meta), pytest.raises(ValueError):
        stored(db, load, meta)


def test_workspace_isolation(library):
    _, reader = library
    with reader() as (db, load, meta), pytest.raises(ValueError):
        stored(db, load, meta, 'b')


def test_rehashed_metadata_forgery(library):
    path, reader = library
    with closing(sqlite3.connect(path)) as db, db:
        payload, = db.execute('SELECT payload FROM snapshot').fetchone()
        data = json.loads(payload)
        for anchor in data['anchors']:
            anchor['metadata']['author'] = 'False author'
        forged = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        db.execute('UPDATE snapshot SET payload=?,checksum=?', (forged,digest(forged)))
    with reader() as (db, load, meta), pytest.raises(ValueError, match='unattested metadata'):
        stored(db, load, meta)


def test_existing_clipboard_metadata_revocation_gap(library):
    path, reader = library
    with reader() as (db, load, meta):
        stale = stored(db, load, meta)
    change(path, 'UPDATE trusted_metadata SET active=0')
    with reader() as (db, load, meta):
        # Reproduce API gap, not an assertion of a released UI defect.
        assert 'Fixture author' in stale.clipboard('a',0,'citation',load)
        # Existing restore API is the required pre-copy reauthorization boundary.
        with pytest.raises(ValueError, match='unattested metadata'):
            stored(db, load, meta)
