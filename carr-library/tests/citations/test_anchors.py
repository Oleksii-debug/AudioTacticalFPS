from dataclasses import replace
from io import BytesIO
import pytest
from pypdf import PdfReader, PdfWriter
from carr_library.citations.anchors import (
    Anchor, Identity, Locator, Metadata, make_anchor, resolve,
    format_citation, pdf_label_candidates, validate_span,
)

I = Identity('workspace-a', 'edition-a', 'a' * 64, 'r1')
M = Metadata('Synthetic author', 'Same title', '2', '3')
P = Locator('physical_page', 4, 'iv', True)
RAW = 'Міст — cafe\u0301; 🙂'


def anchor(raw=RAW, locator=P, identity=I, start=0, end=None):
    return make_anchor(identity, locator, raw, start, len(raw) if end is None else end, M, text_verified=True)


def test_exact_unicode_and_metadata_snapshot():
    a = anchor(start=5, end=11)
    assert a.quote == RAW[5:11]
    assert resolve(a, I, P, RAW) == RAW
    assert format_citation(a) == 'Synthetic author, Same title, vol. 2, edition 3, p. iv (PDF page 4)'
    corrected = replace(M, author='Other author')
    assert corrected != a.metadata and a.metadata == M


@pytest.mark.parametrize('identity', [replace(I, workspace='b'), replace(I, edition_id='b'),
                                     replace(I, revision='r2'), replace(I, asset_sha256='b'*64)])
def test_reject_same_text_wrong_source(identity):
    with pytest.raises(ValueError):
        resolve(anchor(), identity, P, RAW)


def test_reject_tampering_and_unverified_text():
    a = anchor()
    for bad in (replace(a, quote='forged'), replace(a, start=-1), replace(a, end=100), replace(a, raw_sha256='0'*64)):
        with pytest.raises(ValueError):
            resolve(bad, I, P, RAW)
    with pytest.raises(ValueError):
        resolve(a, I, P, RAW + '!')
    with pytest.raises(ValueError):
        make_anchor(I, P, RAW, 0, 1, M, text_verified=False)


def test_logical_and_unknown_and_repeat_labels():
    assert 'Segment 1 (unpaginated)' in format_citation(anchor(locator=Locator('logical_segment', 1)))
    assert 'PDF page' not in format_citation(anchor(locator=Locator('logical_segment', 1)))
    assert 'printed page unverified' in format_citation(anchor(locator=Locator('physical_page', 1, '1')))
    with pytest.raises(ValueError):
        Locator('logical_segment', 1, '1', True)
    with pytest.raises(ValueError):
        resolve(anchor(), I, Locator('physical_page', 5, 'iv', True), RAW)


def test_cross_page_span():
    a, b = anchor(), anchor('next', Locator('physical_page', 5, 'iv', True))
    assert validate_span([a, b]) == (a, b)
    with pytest.raises(ValueError):
        validate_span([anchor(end=2), b])
    for bad in (replace(b, locator=Locator('physical_page', 6)), replace(b, identity=replace(I, edition_id='b')), replace(b, start=1)):
        with pytest.raises(ValueError):
            validate_span([a, bad])


def pdf(labels):
    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(100, 100)
    if labels:
        writer.set_page_label(0, 1, style='/r', start=1)
        writer.set_page_label(2, 3, style='/D', start=8)
    stream = BytesIO()
    writer.write(stream)
    stream.seek(0)
    return PdfReader(stream)


def test_pypdf_default_labels_are_not_printed_evidence():
    reader = pdf(False)
    assert reader.page_labels == ['1', '2', '3', '4']
    assert all(x.printed is None and not x.verified for x in pdf_label_candidates(reader))


def test_pypdf_roman_and_arabic_are_only_candidates():
    result = pdf_label_candidates(pdf(True))
    assert [x.printed for x in result] == ['i', 'ii', '8', '9']
    assert all(not x.verified for x in result)
    assert [x.index for x in result] == [1, 2, 3, 4]
