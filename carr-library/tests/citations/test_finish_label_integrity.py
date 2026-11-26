"""Untrusted PDF labels and persisted evidence must not introduce broken locators."""
from dataclasses import asdict
from io import BytesIO
import json

import pytest
from pypdf import PdfReader, PdfWriter

from carr_library.citations.anchors import (
    Identity, Locator, Metadata, digest, make_anchor, pdf_label_candidates,
)
from carr_library.research.span_snapshot import restore_span


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


def test_saved_evidence_cannot_replay_a_nul_label_even_with_matching_checksum():
    identity = Identity('workspace', 'edition', 'a' * 64, 'revision')
    raw = 'A source-exact quotation.'
    anchor = make_anchor(identity, Locator('physical_page', 1, 'iv', True), raw,
                         0, len(raw), Metadata('Author', 'Title'), text_verified=True)
    item = asdict(anchor)
    item['locator']['printed'] = 'iv\x00forged'
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


@pytest.mark.parametrize('label', ['\ud800', 'iv\udfff'])
@pytest.mark.parametrize('verified', [False, True])
def test_non_utf8_page_labels_fail_before_citation_serialization(label, verified):
    # Imported label strings may originate outside the UTF-8 persisted store.
    # They must fail at the locator boundary, not during a later copy/export.
    with pytest.raises(ValueError, match='invalid printed label'):
        Locator('physical_page', 1, label, verified)
