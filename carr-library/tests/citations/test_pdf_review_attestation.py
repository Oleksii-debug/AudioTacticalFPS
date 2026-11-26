"""Synthetic review records, real PDFium rendering and historical SQLite loader."""
from dataclasses import replace
from hashlib import sha256
import sqlite3

import pytest

from carr_library.citations.anchors import Identity, Locator, Metadata, digest, make_anchor, resolve_span
from carr_library.citations.historical import historical_loader
from carr_library.citations.pdf_review_attestation import ReviewedPage, pdf_review_attestor, render_fingerprint
from test_pdf_mapping_contradiction import fixture, inspect


@pytest.fixture
def source():
    data = fixture(True)
    identity = Identity('workspace-a', 'edition-a', sha256(data).hexdigest(), 'rev-a')
    renderer, pixels = render_fingerprint(data, 1)
    # Synthetic golden transcription from the known PDF drawing instruction;
    # this is NOT a real authenticated review service or automatic approval.
    review = ReviewedPage(identity, 1, digest('AAAA'), pixels, renderer, 'synthetic-reviewer')
    return data, identity, review


def test_review_bound_to_actual_render(source):
    data, identity, review = source
    attest = pdf_review_attestor('workspace-a', lambda _: data, lambda *_: review)
    assert attest(identity, Locator('physical_page', 1), digest('AAAA'))
    assert inspect(data)[0] == 'ZZZZ\n'
    assert not attest(identity, Locator('physical_page', 1), digest('ZZZZ\n'))


@pytest.mark.parametrize('change', ['raw_sha256', 'rendered_sha256', 'renderer', 'reviewer', 'physical', 'identity'])
def test_review_mismatch(source, change):
    data, identity, review = source
    values = dict(raw_sha256='0'*64, rendered_sha256='0'*64, renderer='other', reviewer=' ',
                  physical=2, identity=replace(identity, revision='other'))
    attest = pdf_review_attestor('workspace-a', lambda _: data, lambda *_: replace(review, **{change:values[change]}))
    assert not attest(identity, Locator('physical_page', 1), digest('AAAA'))


def test_equal_pixels_do_not_authorize_other_asset(source):
    data, identity, review = source
    other = fixture(False)
    assert render_fingerprint(data, 1) == render_fingerprint(other, 1)
    attest = pdf_review_attestor('workspace-a', lambda _: other, lambda *_: review)
    assert not attest(identity, Locator('physical_page', 1), digest('AAAA'))


def test_missing_review_and_wrong_scope(source):
    data, identity, review = source
    attest = pdf_review_attestor('workspace-a', lambda _: data, lambda *_: None)
    assert not attest(identity, Locator('physical_page', 1), digest('AAAA'))
    attest = pdf_review_attestor('workspace-b', lambda _: data, lambda *_: review)
    assert not attest(identity, Locator('physical_page', 1), digest('AAAA'))


def test_wrong_page_and_logical_locator(source):
    data, identity, review = source
    attest = pdf_review_attestor('workspace-a', lambda _: data, lambda *_: replace(review, physical=2))
    assert not attest(identity, Locator('physical_page', 2), digest('AAAA'))
    assert not attest(identity, Locator('logical_segment', 1), digest('AAAA'))


def test_corrupt_pdf_rejected():
    data = b'not a pdf'
    identity = Identity('workspace-a','edition-a',sha256(data).hexdigest(),'rev-a')
    review = ReviewedPage(identity,1,digest('AAAA'),'0'*64,'renderer','reviewer')
    attest = pdf_review_attestor('workspace-a',lambda _: data,lambda *_: review)
    assert not attest(identity,Locator('physical_page',1),digest('AAAA'))


@pytest.mark.parametrize('raw,approved', [('AAAA', True), ('ZZZZ\n', False)])
def test_historical_loader(source, tmp_path, raw, approved):
    data, identity, review = source
    # Minimal real SQLite fixture matching #40 loader columns; not the full #21 store.
    with sqlite3.connect(tmp_path/'fixture.db') as db:
        db.executescript('''CREATE TABLE revision(workspace,edition,asset,id);
        CREATE TABLE sealed(revision); CREATE TABLE page(revision,ordinal,physical,printed,verified,raw,digest);''')
        db.execute('INSERT INTO revision VALUES(?,?,?,?)', (identity.workspace,identity.edition_id,identity.asset_sha256,identity.revision))
        db.execute('INSERT INTO sealed VALUES(?)',(identity.revision,))
        db.execute('INSERT INTO page VALUES(?,?,?,?,?,?,?)',(identity.revision,1,1,None,0,raw,digest(raw)))
    db = sqlite3.connect(tmp_path/'fixture.db')
    try:
        db.execute('BEGIN')
        locator = Locator('physical_page',1)
        load = historical_loader(db,identity.workspace,pdf_review_attestor(identity.workspace,lambda _: data,lambda *_: review))
        if approved:
            anchor = make_anchor(identity,locator,raw,0,len(raw),Metadata('Synthetic author','Synthetic title'),text_verified=load(identity,locator)[3])
            assert resolve_span((anchor,),load)[0][1] == 'AAAA'
            assert 'printed page unverified' in anchor.locator.display()
        else:
            with pytest.raises(ValueError, match='unverified extraction'):
                load(identity,locator)
    finally:
        db.close()
