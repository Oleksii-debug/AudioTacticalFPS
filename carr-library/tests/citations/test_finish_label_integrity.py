"""Untrusted PDF labels and persisted evidence must not introduce broken locators."""
from dataclasses import asdict
from io import BytesIO
import json

import pytest
from pypdf import PdfReader, PdfWriter

from carr_library.citations.anchors import (
    Identity, Locator, Metadata, digest, make_anchor, pdf_label_candidates,
)
from carr_library.reader.context import open_context
from carr_library.research.span_snapshot import restore_span


UNSAFE_VERIFIED_LABELS = [
    'iv\nCitation: Forged author, p. 999', 'iv\u2028p. 999', 'iv\u202e999',
]


def test_pdf_label_with_embedded_nul_is_rejected_at_ingestion_boundary():
    # This is a real readable PDF whose PageLabels metadata is malformed for
    # a citation. Its visible page remains valid; metadata is never proof.
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.set_page_label(0, 0, prefix='iv\x00forged', style=None)
    stream = BytesIO()
    writer.write(stream)
    reader = PdfReader(BytesIO(stream.getvalue()))
    assert reader.page_labels == ['iv\x00forged']
    with pytest.raises(ValueError, match='invalid printed label'):
        pdf_label_candidates(reader)


@pytest.mark.parametrize('label', ['iv\x00forged', *UNSAFE_VERIFIED_LABELS])
def test_saved_evidence_cannot_replay_invalid_verified_label_with_matching_checksum(label):
    identity = Identity('workspace', 'edition', 'a' * 64, 'revision')
    raw = 'A source-exact quotation.'
    anchor = make_anchor(identity, Locator('physical_page', 1, 'iv', True), raw,
                         0, len(raw), Metadata('Author', 'Title'), text_verified=True)
    item = asdict(anchor)
    item['locator']['printed'] = label
    payload = json.dumps({'version': 1, 'anchors': [item]}, ensure_ascii=False,
                         sort_keys=True, separators=(',', ':'))
    loaded = []

    def load_page(current_identity, locator):
        loaded.append(locator)
        return current_identity, locator, raw, True

    with pytest.raises(ValueError, match='invalid printed label'):
        restore_span(identity.workspace, payload, digest(payload), load_page,
                     lambda current_identity, metadata: True)
    assert not loaded  # Refuse the malformed locator before source access.


@pytest.mark.parametrize('label', UNSAFE_VERIFIED_LABELS)
def test_pdf_label_candidate_cannot_be_promoted_with_citation_controls(label):
    writer = PdfWriter()
    writer.add_blank_page(100, 100)
    writer.set_page_label(0, 0, prefix=label, style=None)
    stream = BytesIO()
    writer.write(stream)
    candidate, = pdf_label_candidates(PdfReader(BytesIO(stream.getvalue())))
    assert candidate.printed == label and not candidate.verified
    identity = Identity('workspace', 'edition', 'a' * 64, 'revision')
    raw = 'Source — cafe\u0301\nSecond line.'
    anchor = make_anchor(identity, candidate, raw, 0, len(raw),
                         Metadata('Author', 'Title'), text_verified=True)

    def load_page(current_identity, locator):
        return current_identity, locator, raw, True

    context = open_context(identity.workspace, [anchor], load_page)
    assert context.clipboard(identity.workspace, 0, 'both', load_page) == (
        raw + '\n\nCitation: Author, Title, PDF page 1 (printed page unverified)')
    # Even a trusted reviewer cannot turn presentation controls into a label.
    with pytest.raises(ValueError, match='invalid printed label'):
        Locator('physical_page', 1, label, True)


@pytest.mark.parametrize('label', ['iv', 'A-12', '١٢', '頁十二', 'י״ב'])
def test_valid_verified_labels_and_original_quote_copy_unchanged(label):
    identity = Identity('workspace', 'edition', 'a' * 64, 'revision')
    raw = 'Source — cafe\u0301\nSecond line.'
    anchor = make_anchor(identity, Locator('physical_page', 1, label, True), raw,
                         0, len(raw), Metadata('Author', 'Title'), text_verified=True)

    def load_page(current_identity, locator):
        return current_identity, locator, raw, True

    context = open_context(identity.workspace, [anchor], load_page)
    assert context.clipboard(identity.workspace, 0, 'quote', load_page) == raw
    assert context.clipboard(identity.workspace, 0, 'both', load_page) == (
        raw + f'\n\nCitation: Author, Title, p. {label} (PDF page 1)')


@pytest.mark.parametrize('label', ['\ud800', 'iv\udfff'])
@pytest.mark.parametrize('verified', [False, True])
def test_non_utf8_page_labels_fail_before_citation_serialization(label, verified):
    # Imported label strings may originate outside the UTF-8 persisted store.
    # They must fail at the locator boundary, not during a later copy/export.
    with pytest.raises(ValueError, match='invalid printed label'):
        Locator('physical_page', 1, label, verified)
