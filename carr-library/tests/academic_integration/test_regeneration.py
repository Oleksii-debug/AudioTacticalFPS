"""Actual canonical citeproc and OOXML regeneration, using synthetic sources."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from zipfile import ZipFile, ZIP_DEFLATED

from lxml import etree
import pytest

from carr_library.academic_integration import (
    CitationCancelled, CitationCluster, CitationError, CitationParagraph, CitationRequest,
    export_citation_docx, read_citation_docx, regenerate_citation_docx, render_citations,
)
from carr_library.academic_integration import regeneration


REFERENCES = [
    {'id': 'source-a', 'type': 'book', 'title': 'Наука Štúdia',
     'author': [{'family': 'Doe', 'given': 'Jane'}], 'issued': {'date-parts': [[2024]]}},
    {'id': 'source-b', 'type': 'book', 'title': 'Another source',
     'author': [{'family': 'Doe', 'given': 'Jane'}], 'issued': {'date-parts': [[2024]]}},
]


def document(tmp_path, *, style='harvard', locator=None, style_path=None):
    citations = [CitationCluster((CitationRequest('source-a', locator,
        locator_verified=locator is not None),), 'stable-first', 1),
        CitationCluster((CitationRequest('source-b'),), 'stable-second', 2)]
    result = render_citations(REFERENCES, style=style, citations=citations, style_path=style_path)
    path = export_citation_docx(result, tmp_path / 'source.docx', title='Дослідження Štúdia', paragraphs=[
        CitationParagraph('  First paragraph.\nUnicode: Україна.  ', ('stable-first',)),
        CitationParagraph('Intervening uncited paragraph.'),
        CitationParagraph('Second paragraph.', ('stable-second',)),
    ])
    return path, citations


def rewrite_snapshot(path, change, *, checksum=True):
    output = BytesIO()
    with ZipFile(path) as original, ZipFile(output, 'w', ZIP_DEFLATED) as replacement:
        for item in original.infolist():
            raw = original.read(item)
            if item.filename == 'customXml/carr-citations.xml':
                node = etree.fromstring(raw)
                snapshot = json.loads(node.text)
                change(snapshot)
                raw_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode('utf-8')
                node.text = raw_json.decode('utf-8')
                if checksum:
                    node.set('sha256', sha256(raw_json).hexdigest())
                raw = etree.tostring(node)
            replacement.writestr(item, raw)
    path.write_bytes(output.getvalue())


@pytest.mark.parametrize('style', ['harvard', 'chicago-notes'])
def test_real_roundtrip_preserves_snapshot_paragraphs_ids_pins_and_endnotes(tmp_path, style):
    source, _ = document(tmp_path, style=style)
    original = source.read_bytes()
    target = regenerate_citation_docx(source, tmp_path / 'regenerated.docx')
    before, after = read_citation_docx(source), read_citation_docx(target)
    assert after == before
    assert source.read_bytes() == original
    with ZipFile(source) as old, ZipFile(target) as new:
        assert new.read('word/document.xml') == old.read('word/document.xml')
        if style == 'chicago-notes':
            assert new.read('word/endnotes.xml') == old.read('word/endnotes.xml')


def test_explicit_insert_and_reorder_keep_ids_and_recompute_numeric_citations(tmp_path):
    source, previous = document(tmp_path, style='ieee')
    citations = [previous[1], CitationCluster((CitationRequest('source-a'),), 'inserted', 2), previous[0]]
    paragraphs = [CitationParagraph('Moved second.', ('stable-second',)),
                  CitationParagraph('New claim.', ('inserted',)),
                  CitationParagraph('Moved first.', ('stable-first',))]
    target = regenerate_citation_docx(source, tmp_path / 'edited.docx', citations=citations,
                                      paragraphs=paragraphs)
    saved = read_citation_docx(target)
    assert [x['id'] for x in saved['clusters']] == ['stable-second', 'inserted', 'stable-first']
    assert [x['text'] for x in saved['citations']] == ['[1]', '[2]', '[2]']
    assert saved['bibliography'][0]['reference_ids'] == ['source-b']
    assert saved['document']['paragraphs'] == [
        {'text': p.text, 'citation_ids': list(p.citation_ids)} for p in paragraphs]


def test_changed_reference_locator_requires_explicit_new_source_check(tmp_path):
    source, citations = document(tmp_path, locator='12')
    changed = deepcopy(REFERENCES)
    changed[0]['title'] = 'Changed edition'
    with pytest.raises(CitationError, match='source changed'):
        regenerate_citation_docx(source, tmp_path / 'stale.docx', references=changed)
    with pytest.raises(CitationError, match='source changed'):
        regenerate_citation_docx(source, tmp_path / 'stale-revision.docx', changed_reference_ids=['source-a'])
    citations[0] = CitationCluster((CitationRequest('source-a', '19', locator_verified=True),), 'stable-first', 1)
    target = regenerate_citation_docx(source, tmp_path / 'reviewed.docx', references=changed,
                                      citations=citations, changed_reference_ids=['source-a'])
    saved = read_citation_docx(target)
    assert saved['references'][0]['title'] == 'Changed edition'
    assert saved['clusters'][0]['items'][0]['locator'] == '19'
    assert saved['clusters'][0]['locator_evidence'][0]['locator_verified'] is True


def test_changed_metadata_without_locator_regenerates_without_inventing_one(tmp_path):
    source, _ = document(tmp_path)
    changed = deepcopy(REFERENCES)
    changed[0]['title'], changed[0]['page'] = 'Reviewed metadata', '100-125'
    target = regenerate_citation_docx(source, tmp_path / 'metadata.docx', references=changed)
    saved = read_citation_docx(target)
    assert saved['references'][0]['page'] == '100-125'
    assert 'locator' not in saved['clusters'][0]['items'][0]
    assert saved['clusters'][0]['locator_evidence'][0]['locator_verified'] is None


@pytest.mark.parametrize('change', [
    lambda s: s['clusters'][0]['locator_evidence'][0].update(locator_verified=False),
    lambda s: s['clusters'][0]['locator_evidence'][0].update(reference_id='source-b'),
    lambda s: s['clusters'][0]['items'][0].update(unexpected='do not discard'),
    lambda s: s['clusters'][0]['items'][0].update(id={'invalid': 'identifier'}),
    lambda s: s['clusters'][1].update(id='stable-first'),
    lambda s: s['clusters'][0].update(note_index=True),
    lambda s: s.update(clusters=s['clusters'] * 2501),
    lambda s: s['document']['paragraphs'][0].update(citation_ids=['unknown']),
])
def test_rehashed_inconsistent_metadata_is_rejected_before_worker(tmp_path, monkeypatch, change):
    source, _ = document(tmp_path, locator='12')
    rewrite_snapshot(source, change)
    def forbidden(*args, **kwargs):
        pytest.fail('Malformed snapshot reached the processor')
    monkeypatch.setattr(regeneration, 'render_citations', forbidden)
    with pytest.raises(CitationError):
        regenerate_citation_docx(source, tmp_path / 'invalid.docx')
    assert not (tmp_path / 'invalid.docx').exists()


@pytest.mark.parametrize('field', ['processor', 'style'])
def test_stale_pins_are_never_relabelled_as_current(tmp_path, field):
    source, _ = document(tmp_path)
    rewrite_snapshot(source, lambda s: s[field].update(sha256='0' * 64))
    with pytest.raises(CitationError, match='identity changed'):
        regenerate_citation_docx(source, tmp_path / 'stale.docx')
    assert not (tmp_path / 'stale.docx').exists()


def test_metadata_checksum_corruption_is_rejected(tmp_path):
    source, _ = document(tmp_path)
    rewrite_snapshot(source, lambda s: s['references'][0].update(title='Altered'), checksum=False)
    with pytest.raises(CitationError, match='integrity'):
        regenerate_citation_docx(source, tmp_path / 'corrupt.docx')


def test_native_note_gaps_are_rejected_without_silent_renumbering(tmp_path):
    source, _ = document(tmp_path, style='chicago-notes')
    rewrite_snapshot(source, lambda s: s['clusters'][1].update(note_index=8))
    with pytest.raises(CitationError, match='consecutive'):
        regenerate_citation_docx(source, tmp_path / 'wrong-notes.docx')
    assert not (tmp_path / 'wrong-notes.docx').exists()


def test_source_change_after_actual_render_stops_publication(tmp_path, monkeypatch):
    source, _ = document(tmp_path)
    actual = regeneration.render_citations
    def changing(*args, **kwargs):
        result = actual(*args, **kwargs)
        source.write_bytes(b'An independently changed source')
        return result
    monkeypatch.setattr(regeneration, 'render_citations', changing)
    with pytest.raises(CitationError, match='changed during regeneration'):
        regenerate_citation_docx(source, tmp_path / 'changed.docx')
    assert not (tmp_path / 'changed.docx').exists()


def test_cancellation_no_overwrite_and_explicit_order_guards(tmp_path):
    source, _ = document(tmp_path)
    original = source.read_bytes()
    with pytest.raises(CitationCancelled):
        regenerate_citation_docx(source, tmp_path / 'cancelled.docx', cancelled=lambda: True)
    with pytest.raises(FileExistsError):
        regenerate_citation_docx(source, source)
    with pytest.raises(CitationError, match='order'):
        regenerate_citation_docx(source, tmp_path / 'wrong-order.docx', paragraphs=['Citation omitted'])
    assert source.read_bytes() == original
    assert set(p.name for p in tmp_path.glob('*.docx')) == {'source.docx'}


def test_custom_style_requires_explicit_file_and_exact_saved_bytes(tmp_path):
    from carr_library.academic_integration import formatting
    style = tmp_path / 'selected.csl'
    style.write_bytes((formatting._ROOT / 'styles/harvard-cite-them-right.csl').read_bytes())
    source, _ = document(tmp_path, style_path=style)
    with pytest.raises(CitationError, match='not installed'):
        regenerate_citation_docx(source, tmp_path / 'missing.docx')
    target = regenerate_citation_docx(source, tmp_path / 'same-style.docx', style_path=style)
    assert read_citation_docx(target)['style']['sha256'] == sha256(style.read_bytes()).hexdigest()
    style.write_bytes(style.read_bytes() + b'\n')
    with pytest.raises(CitationError, match='identity changed'):
        regenerate_citation_docx(source, tmp_path / 'different-style.docx', style_path=style)
