"""Cancellation remains a stop signal through real source/citation boundaries."""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

from carr_library.application import LibraryApp
from carr_library.catalog import intake
from carr_library.citations.anchors import Identity, Locator, digest
from carr_library.citations.historical import historical_loader
from carr_library.citations.pdf_review_attestation import (
    ReviewedPage, pdf_review_attestor, render_fingerprint)
from carr_library.citations.printed_label_attestation import printed_label_guard
from carr_library.citations.text_attestation import CONFIG, strict_text_attestor
from carr_library.storage.revisions import Page, Storage


class PhaseDeadline(TimeoutError):
    """Same existing exception hierarchy as the real corpus phase deadline."""


ERRORS = (InterruptedError, TimeoutError, PhaseDeadline)
RAW = 'Synthetic source needle.\r\n'
DATA = (RAW + '\fSecond retained segment.').encode('utf-8')


@pytest.fixture
def source(tmp_path):
    store = Storage(tmp_path / 'source.db')
    identity = Identity('workspace', 'edition', sha256(DATA).hexdigest(), 'revision')
    store.publish_revision(identity.workspace, identity.edition_id, identity.revision,
                           identity.asset_sha256, CONFIG, [Page(RAW), Page('Second retained segment.')])
    try:
        yield store, identity, Locator('logical_segment', 1)
    finally:
        store.close()


@pytest.mark.parametrize('error_type', ERRORS)
def test_historical_text_asset_deadline_is_preserved_through_both_attestors(source, error_type):
    store, identity, locator = source
    error = error_type('Stopped actual source resolution.')
    calls = []
    def resolve(selected):
        calls.append(selected)
        raise error
    with store.transaction():
        text = strict_text_attestor(store.db, identity.workspace, resolve)
        load = historical_loader(store.db, identity.workspace,
            printed_label_guard(identity.workspace, text, lambda *_: None))
        with pytest.raises(error_type) as caught:
            load(identity, locator)
        assert caught.value is error and calls == [identity]
        # The read remains valid; cancellation grants no replacement authority.
        assert store.db.execute('SELECT count(*) FROM page').fetchone()[0] == 2


@pytest.mark.parametrize('error_type', ERRORS)
def test_real_file_snapshot_segmentation_does_not_turn_cancelled_read_into_unverified_text(source, tmp_path, monkeypatch, error_type):
    store, identity, locator = source
    path = tmp_path / 'original.txt'
    path.write_bytes(DATA)
    snapshot = intake.file_snapshot(tmp_path, path.name)
    original_open = intake.FileSnapshot.open
    consumed, opened = [], []
    error = error_type('Stopped after a real retained byte read.')
    def stopped():
        if consumed:
            raise error
        return False
    @contextmanager
    def observed_open(asset, **kwargs):
        with original_open(asset, **kwargs) as stream:
            opened.append(stream)
            class Reader:
                def read(self, count=-1):
                    value = stream.read(count)
                    if value:
                        consumed.append(len(value))
                    return value
            yield Reader()
    monkeypatch.setattr(intake.FileSnapshot, 'open', observed_open)
    with store.transaction():
        text = strict_text_attestor(store.db, identity.workspace, lambda _: snapshot, cancelled=stopped)
        load = historical_loader(store.db, identity.workspace,
            printed_label_guard(identity.workspace, text, lambda *_: None))
        with pytest.raises(error_type) as caught:
            load(identity, locator)
        assert caught.value is error and sum(consumed) == len(DATA)
    assert len(opened) == 1 and opened[0].closed and path.read_bytes() == DATA


def synthetic_pdf():
    stream = b'BT /F1 10 Tf 10 40 Td (Synthetic source) Tj ET\n'
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 180 100] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
        b'<< /Length ' + str(len(stream)).encode('ascii') + b' >>\nstream\n' + stream + b'endstream',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    output, offsets = bytearray(b'%PDF-1.4\n'), [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(str(number).encode('ascii') + b' 0 obj\n' + value + b'\nendobj\n')
    crossref = len(output)
    output.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        output.extend(f'{offset:010d} 00000 n \n'.encode('ascii'))
    output.extend(b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n' +
                  str(crossref).encode('ascii') + b'\n%%EOF\n')
    return bytes(output)


@pytest.mark.parametrize('error_type', ERRORS)
def test_actual_pdf_render_deadline_propagates_without_replacing_the_original_failure(tmp_path, error_type):
    data = synthetic_pdf()
    renderer, pixels = render_fingerprint(data, 1)
    identity = Identity('workspace', 'pdf-edition', sha256(data).hexdigest(), 'pdf-revision')
    raw = 'Synthetic source'
    review = ReviewedPage(identity, 1, digest(raw), pixels, renderer, 'Synthetic explicit reviewer')
    error = error_type('Stopped after the actual PDFium rendering.')
    rendered = []
    def render_then_stop(value, physical):
        result = render_fingerprint(value, physical)
        rendered.append(result)
        assert result == (renderer, pixels)
        raise error
    store = Storage(tmp_path / 'pdf.db')
    try:
        store.publish_revision(identity.workspace, identity.edition_id, identity.revision,
            identity.asset_sha256, 'independent-reviewed-pdf', [Page(raw, physical=1)])
        with store.transaction():
            attest = pdf_review_attestor(identity.workspace, lambda _: data, lambda *_: review,
                                         renderer=render_then_stop)
            load = historical_loader(store.db, identity.workspace,
                printed_label_guard(identity.workspace, attest, lambda *_: None))
            with pytest.raises(error_type) as caught:
                load(identity, Locator('physical_page', 1))
            assert caught.value is error and rendered == [(renderer, pixels)]
    finally:
        store.close()


@pytest.mark.parametrize('error_type', ERRORS)
def test_printed_label_lookup_cancellation_never_falls_through_to_text_approval(source, error_type):
    _, identity, _ = source
    error = error_type('Stopped current printed-label review.')
    def lookup(*_):
        raise error
    def forbidden(*_):
        pytest.fail('Cancelled label authorization must not try a text fallback.')
    attest = printed_label_guard(identity.workspace, forbidden, lookup)
    with pytest.raises(error_type) as caught:
        attest(identity, Locator('physical_page', 1, 'iv', True), digest(RAW))
    assert caught.value is error


@pytest.mark.parametrize('error_type', ERRORS)
def test_actual_current_copy_cancellation_preserves_error_closes_snapshot_and_keeps_old_selection(tmp_path, monkeypatch, error_type):
    source = tmp_path / 'retained.txt'
    source.write_bytes(DATA)
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Synthetic cancellation')
    imported = app.import_file(workspace, source, title='Synthetic retained source')
    assert imported['state'] == 'complete'
    hit, = app.search(workspace, 'needle')
    assert hit.verified is True
    old_handles = dict(app._selections)
    original_asset, original_read = app._asset_bytes, app.read
    reads, assets = [], []
    error = error_type('Stopped actual retained-source copy verification.')
    @contextmanager
    def monitored_read(*args, **kwargs):
        with original_read(*args, **kwargs) as db:
            reads.append(db)
            yield db
    def asset_then_stop(*args, **kwargs):
        value = original_asset(*args, **kwargs)
        assert type(value) is bytes and value == DATA
        assets.append(value)
        raise error
    monkeypatch.setattr(app, 'read', monitored_read)
    monkeypatch.setattr(app, '_asset_bytes', asset_then_stop)
    with pytest.raises(error_type) as caught:
        app.copy(workspace, hit.ref, hit.revision, 'quote')
    assert caught.value is error and assets == [DATA]
    assert app._selections == old_handles and source.read_bytes() == DATA
    assert len(reads) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        reads[0].execute('SELECT 1')
    monkeypatch.setattr(app, '_asset_bytes', original_asset)
    assert app.copy(workspace, hit.ref, hit.revision, 'quote') == 'needle'
