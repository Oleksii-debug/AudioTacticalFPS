"""Bibliographic exports preserve pinpoint locators without inventing extent."""
from dataclasses import replace
from hashlib import sha256
import re

import pytest

from carr_library.catalog._rispy_parser import loads as load_ris
from carr_library.research.evidence_export import Evidence, export_topic


def source(**changes):
    raw = 'Private exact quotation.'
    row = Evidence('workspace', 'topic', 'evidence', 'edition', 'revision',
                   'a' * 64, sha256(raw.encode()).hexdigest(), 'physical_page',
                   4, None, '137', True, raw, 0, len(raw), raw,
                   'Synthetic title', 'Synthetic author', '2', True,
                   'Private research note.')
    return replace(row, **changes)


def locator_note(format, output):
    if format == 'ris':
        reference, = load_ris(output)
        assert 'start_page' not in reference and 'end_page' not in reference
        note, = reference['notes']
        return note
    assert not re.search(r'^\s*pages\s*=', output, flags=re.MULTILINE)
    note, = re.findall(r'^  note = \{(.*)\},$', output, flags=re.MULTILINE)
    return note


@pytest.mark.parametrize('format', ['bibtex', 'ris'])
def test_quote_page_label_is_a_locator_and_never_publication_extent(format):
    row = source()
    output = export_topic('workspace', 'topic', [row], format=format)
    assert locator_note(format, output) == 'Source locator: PDF page 4; printed page 137'
    assert row.quote not in output and row.note not in output


@pytest.mark.parametrize('format', ['bibtex', 'ris'])
def test_multipage_locator_retains_every_slice_in_order_and_unknown_labels(format):
    first = source(span_count=3, printed_label='ix')
    second = replace(first, span_index=2, physical_index=5,
                     printed_label='unverified-candidate', printed_label_verified=False)
    third = replace(first, span_index=3, physical_index=6, printed_label='S-9')
    output = export_topic('workspace', 'topic', [first, second, third], format=format)
    assert locator_note(format, output) == (
        'Source locator: PDF page 4; printed page ix; '
        'PDF page 5; printed page unknown; PDF page 6; printed page S-9')
    assert 'unverified-candidate' not in output
    assert first.quote not in output and first.note not in output


@pytest.mark.parametrize('format', ['bibtex', 'ris'])
def test_unpaginated_segment_remains_a_segment_in_bibliographic_notes(format):
    row = source(kind='logical_segment', physical_index=None, segment_index=8,
                 printed_label=None, printed_label_verified=False)
    output = export_topic('workspace', 'topic', [row], format=format)
    assert locator_note(format, output) == 'Source locator: Segment 8 (unpaginated)'
    assert 'PDF page' not in output and 'printed page' not in output
