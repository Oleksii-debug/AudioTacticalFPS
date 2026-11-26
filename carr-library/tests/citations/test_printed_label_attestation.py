"""Real pypdf metadata, PDFium render, and historical SQLite reader."""
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import sqlite3

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.constants import PageLabelStyle
from reportlab.pdfgen.canvas import Canvas

from carr_library.citations.anchors import (Identity, LabelCorrection, Locator,
    Metadata, digest, format_span_citation, make_anchor, pdf_label_candidates)
from carr_library.citations.historical import historical_loader
from carr_library.citations.pdf_review_attestation import (ReviewedPage,
    pdf_review_attestor, render_fingerprint)
from carr_library.citations.printed_label_attestation import printed_label_guard


@pytest.fixture
def source():
    stream = BytesIO()
    c = Canvas(stream, pagesize=(300, 300), invariant=1)
    c.drawString(20, 240, 'Synthetic text')
    c.drawString(140, 20, 'iv')
    c.save()
    writer = PdfWriter(clone_from=BytesIO(stream.getvalue()))
    writer.set_page_label(0, 0, style=PageLabelStyle.DECIMAL, start=999)
    output = BytesIO()
    writer.write(output)
    data = output.getvalue()
    identity = Identity('workspace-a', 'edition-a', sha256(data).hexdigest(), 'rev-a')
    candidate, = pdf_label_candidates(PdfReader(BytesIO(data)))
    raw = 'Synthetic text\niv\n'
    renderer, pixels = render_fingerprint(data, 1)
    review = ReviewedPage(identity, 1, digest(raw), pixels, renderer, 'synthetic-reviewer')
    correct = Locator('physical_page', 1, 'iv', True)
    approval = LabelCorrection(identity, candidate, correct, 'synthetic known drawing', 'fixture-reviewer', pixels)
    text = pdf_review_attestor(identity.workspace, lambda _: data, lambda *_: review)
    return identity, candidate, raw, approval, text


def read_citation(tmp_path, identity, locator, raw, attest):
    path = tmp_path / 'library.db'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE revision(workspace,edition,asset,id);
        CREATE TABLE sealed(revision); CREATE TABLE page(revision,ordinal,physical,printed,verified,raw,digest);''')
        db.execute('INSERT INTO revision VALUES(?,?,?,?)', (identity.workspace,identity.edition_id,identity.asset_sha256,identity.revision))
        db.execute('INSERT INTO sealed VALUES(?)', (identity.revision,))
        db.execute('INSERT INTO page VALUES(?,?,?,?,?,?,?)', (identity.revision,1,1,locator.printed,int(locator.verified),raw,digest(raw)))
    db = sqlite3.connect(path)
    try:
        db.execute('BEGIN')
        load = historical_loader(db, identity.workspace, attest)
        anchor = make_anchor(identity, locator, raw, 0, 14, Metadata('Synthetic author','Synthetic title'), text_verified=load(identity, locator)[3])
        return format_span_citation((anchor,), load)
    finally:
        db.close()


def test_reproduce_existing_false_verified_label(source, tmp_path):
    identity, candidate, raw, _, text = source
    assert candidate.printed == '999' and not candidate.verified
    forged = replace(candidate, verified=True)
    assert 'p. 999' in read_citation(tmp_path, identity, forged, raw, text)


def test_guard_blocks_forged_label(source, tmp_path):
    identity, candidate, raw, approval, text = source
    guard = printed_label_guard(identity.workspace, text, lambda *_: approval)
    with pytest.raises(ValueError, match='unverified extraction'):
        read_citation(tmp_path, identity, replace(candidate, verified=True), raw, guard)


def test_verified_roman_label(source, tmp_path):
    identity, _, raw, approval, text = source
    guard = printed_label_guard(identity.workspace, text, lambda *_: approval)
    assert 'p. iv (PDF page 1)' in read_citation(tmp_path, identity, approval.replacement, raw, guard)


def test_unverified_metadata_fallback(source, tmp_path):
    identity, candidate, raw, _, text = source
    def should_not_lookup(*_):
        pytest.fail('unverified metadata is not a printed-page claim')
    guard = printed_label_guard(identity.workspace, text, should_not_lookup)
    citation = read_citation(tmp_path, identity, candidate, raw, guard)
    assert 'PDF page 1 (printed page unverified)' in citation and '999' not in citation


@pytest.mark.parametrize('kind', ['missing', 'workspace', 'edition', 'revision', 'asset', 'page', 'label', 'unverified'])
def test_wrong_approval(source, kind):
    identity, _, raw, approval, text = source
    altered = {'workspace': {'workspace':'workspace-b'}, 'edition': {'edition_id':'edition-b'},
               'revision': {'revision':'rev-b'}, 'asset': {'asset_sha256':'0'*64}}
    if kind in altered:
        other = replace(approval, identity=replace(identity, **altered[kind]))
    elif kind == 'missing':
        other = None
    elif kind == 'page':
        other = replace(approval, previous=replace(approval.previous,index=2), replacement=replace(approval.replacement,index=2))
    elif kind == 'label':
        other = replace(approval, replacement=replace(approval.replacement,printed='v'))
    else:
        other = replace(approval, replacement=replace(approval.replacement,verified=False))
    guard = printed_label_guard(identity.workspace, text, lambda *_: other)
    assert not guard(identity, approval.replacement, digest(raw))


def test_revocation_checked_every_time(source):
    identity, _, raw, approval, text = source
    active = [approval]
    guard = printed_label_guard(identity.workspace, text, lambda *_: active[0])
    assert guard(identity, approval.replacement, digest(raw))
    active[0] = None
    assert not guard(identity, approval.replacement, digest(raw))


def test_label_approval_not_text_approval(source):
    identity, _, raw, approval, text = source
    guard = printed_label_guard(identity.workspace, text, lambda *_: approval)
    assert not guard(identity, approval.replacement, digest(raw+'changed'))


def test_logical_segment_and_scope(source):
    identity, _, raw, _, _ = source
    guard = printed_label_guard(identity.workspace, lambda *_: True, lambda *_: pytest.fail('not paginated'))
    assert guard(identity, Locator('logical_segment',1), digest(raw))
    assert not guard(replace(identity,workspace='workspace-b'), Locator('logical_segment',1), digest(raw))
