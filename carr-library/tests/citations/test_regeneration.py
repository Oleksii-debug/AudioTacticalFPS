"""Actual CSL and DOCX regeneration with source and publication regressions."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
from zipfile import ZipFile, ZIP_DEFLATED

from lxml import etree
import pytest

from carr_library.citations import regeneration
from carr_library.citations.csl import load_custom_style
from carr_library.citations.documents import CitationParagraph, citation_document, read_citation_docx
from carr_library.citations.regeneration import regenerate_citation_docx

RECORDS = [
    {'id': 'source-a', 'type': 'book', 'title': 'Наука Štúdia',
     'author': [{'family': 'Doe', 'given': 'Jane'}], 'issued': {'date-parts': [[2024]]}},
    {'id': 'source-b', 'type': 'book', 'title': 'Another source',
     'author': [{'family': 'Doe', 'given': 'Jane'}], 'issued': {'date-parts': [[2024]]}},
]


def make_document(tmp_path, *, style='harvard', locator=None):
    item = {'id': 'source-a'}
    if locator is not None:
        item.update(locator=locator, label='page')
    clusters = [dict(id='stable-first', items=[item], note_index=1 if style == 'chicago-note' else None),
                dict(id='stable-second', items=[{'id': 'source-b'}], note_index=2 if style == 'chicago-note' else None)]
    body = [CitationParagraph('  First paragraph.\nUnicode: Україна.  ', ('stable-first',)),
            CitationParagraph('Intervening uncited paragraph.'),
            CitationParagraph('Second paragraph.', ('stable-second',))]
    path = tmp_path / 'source.docx'
    path.write_bytes(citation_document(RECORDS, style=style, citations=clusters,
                                      paragraphs=body, title='Дослідження Štúdia'))
    return path, clusters


def regenerate(source, target, **options):
    return regenerate_citation_docx(source, target, private_root=target.parent / 'private-library', **options)


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


@pytest.mark.parametrize('style', ['harvard', 'chicago-note'])
def test_real_roundtrip_preserves_snapshot_ids_order_prose_and_native_endnotes(tmp_path, style):
    source, _ = make_document(tmp_path, style=style)
    original = source.read_bytes()
    target = regenerate(source, tmp_path / 'regenerated.docx')
    assert read_citation_docx(target) == read_citation_docx(source)
    assert source.read_bytes() == original
    with ZipFile(source) as old, ZipFile(target) as new:
        assert new.read('word/document.xml') == old.read('word/document.xml')
        if style == 'chicago-note':
            assert new.read('word/endnotes.xml') == old.read('word/endnotes.xml')


def test_explicit_insert_and_reorder_recompute_numeric_context(tmp_path):
    source, clusters = make_document(tmp_path, style='ieee')
    selected = [clusters[1], dict(id='inserted', items=[{'id': 'source-a'}], note_index=None), clusters[0]]
    body = [CitationParagraph('Moved second.', ('stable-second',)),
            CitationParagraph('New claim.', ('inserted',)),
            CitationParagraph('Moved first.', ('stable-first',))]
    target = regenerate(source, tmp_path / 'edited.docx', citations=selected, paragraphs=body)
    saved = read_citation_docx(target)
    assert saved['citation_ids'] == ['stable-second', 'inserted', 'stable-first']
    assert saved['rendered']['citations'] == ['[1]', '[2]', '[2]']
    assert saved['rendered']['entry_ids'][0] == ['source-b']
    assert saved['document']['paragraphs'] == [
        {'text': p.text, 'citation_ids': list(p.citation_ids)} for p in body]


def test_changed_source_with_locator_requires_explicit_new_request_without_verification_claim(tmp_path):
    source, clusters = make_document(tmp_path, locator='12')
    changed = deepcopy(RECORDS)
    changed[0]['title'] = 'Changed edition'
    with pytest.raises(ValueError, match='source changed'):
        regenerate(source, tmp_path / 'stale.docx', records=changed)
    with pytest.raises(ValueError, match='source changed'):
        regenerate(source, tmp_path / 'stale-text.docx', changed_reference_ids=['source-a'])
    clusters[0]['items'][0]['locator'] = '19'
    target = regenerate(source, tmp_path / 'reviewed.docx', records=changed,
                        citations=clusters, changed_reference_ids=['source-a'])
    saved = read_citation_docx(target)
    assert saved['records'][0]['title'] == 'Changed edition'
    assert saved['clusters'][0]['items'][0]['locator'] == '19'
    assert saved['quotation_verified'] is False
    assert any('manually' in warning for warning in saved['warnings'])


def test_metadata_change_never_reuses_publication_extent_as_locator(tmp_path):
    source, _ = make_document(tmp_path)
    changed = deepcopy(RECORDS)
    changed[0].update(title='Reviewed metadata', page='100-125')
    target = regenerate(source, tmp_path / 'metadata.docx', records=changed)
    saved = read_citation_docx(target)
    assert saved['records'][0]['page'] == '100-125'
    assert 'locator' not in saved['clusters'][0]['items'][0]


@pytest.mark.parametrize('change', [
    lambda s: s['clusters'][0]['items'][0].update(unexpected='do not discard'),
    lambda s: s['clusters'][0]['items'][0].update(id={'invalid': 'identifier'}),
    lambda s: s['clusters'][1].update(id='stable-first'),
    lambda s: s['clusters'][0].update(note_index=True),
    lambda s: s['document']['paragraphs'][0].update(citation_ids=['unknown']),
    lambda s: s['document'].update(unexpected=True),
    lambda s: s['processor'].update(package='stale'),
    lambda s: s['style'].update(sha256='0' * 64),
])
def test_rehashed_invalid_snapshot_is_rejected_before_processor(tmp_path, monkeypatch, change):
    source, _ = make_document(tmp_path)
    rewrite_snapshot(source, change)
    monkeypatch.setattr(regeneration, 'citation_document',
                        lambda *a, **k: pytest.fail('Malformed snapshot reached the processor'))
    with pytest.raises(ValueError):
        regenerate(source, tmp_path / 'invalid.docx')
    assert not (tmp_path / 'invalid.docx').exists()


def test_checksum_corruption_is_rejected(tmp_path):
    source, _ = make_document(tmp_path)
    rewrite_snapshot(source, lambda s: s['records'][0].update(title='Altered'), checksum=False)
    with pytest.raises(ValueError, match='integrity'):
        regenerate(source, tmp_path / 'corrupt.docx')


def test_native_note_gaps_are_not_silently_renumbered(tmp_path):
    source, _ = make_document(tmp_path, style='chicago-note')
    rewrite_snapshot(source, lambda s: s['clusters'][1].update(note_index=8))
    with pytest.raises(ValueError, match='consecutive'):
        regenerate(source, tmp_path / 'wrong-notes.docx')
    assert not (tmp_path / 'wrong-notes.docx').exists()


@pytest.mark.parametrize('change', ['source', 'destination'])
def test_actual_render_cannot_publish_after_source_or_destination_change(tmp_path, monkeypatch, change):
    source, _ = make_document(tmp_path)
    target = tmp_path / 'new.docx'
    actual = regeneration.citation_document
    def changing(*args, **kwargs):
        content = actual(*args, **kwargs)
        (source if change == 'source' else target).write_bytes(b'Independent user change')
        return content
    monkeypatch.setattr(regeneration, 'citation_document', changing)
    with pytest.raises(ValueError, match='changed'):
        regenerate(source, target)
    if change == 'source':
        assert not target.exists()
    else:
        assert target.read_bytes() == b'Independent user change'


def test_cancellation_new_destination_and_explicit_order_guards(tmp_path):
    source, _ = make_document(tmp_path)
    original = source.read_bytes()
    with pytest.raises(TimeoutError, match='cancelled'):
        regenerate(source, tmp_path / 'cancelled.docx', cancelled=lambda: True)
    with pytest.raises(FileExistsError):
        regenerate(source, source)
    with pytest.raises(ValueError, match='order'):
        regenerate(source, tmp_path / 'wrong-order.docx', paragraphs=['Citation omitted'])
    with pytest.raises(ValueError, match='outside'):
        regenerate_citation_docx(source, tmp_path / 'private.docx', private_root=tmp_path)
    assert source.read_bytes() == original
    assert list(tmp_path.glob('*.docx')) == [source]


def test_custom_style_needs_explicit_identical_selected_bytes(tmp_path):
    style = load_custom_style(b'''<style xmlns="http://purl.org/net/xbiblio/csl" version="1.0" class="in-text">
    <info><title>Selected local style</title><id>urn:synthetic:local</id></info>
    <citation><layout prefix="(" suffix=")"><text variable="title"/></layout></citation>
    <bibliography><layout><text variable="title"/></layout></bibliography></style>''')
    source, _ = make_document(tmp_path, style=style)
    with pytest.raises(ValueError, match='Explicitly select'):
        regenerate(source, tmp_path / 'missing.docx')
    target = regenerate(source, tmp_path / 'same.docx', custom_style=style)
    assert read_citation_docx(target)['style']['sha256'] == style.sha256
    other = load_custom_style(style.source + b'\n')
    with pytest.raises(ValueError, match='identity changed'):
        regenerate(source, tmp_path / 'different.docx', custom_style=other)


def test_visible_word_edits_are_kept_only_in_source_file(tmp_path):
    source, _ = make_document(tmp_path)
    output = BytesIO()
    with ZipFile(source) as original, ZipFile(output, 'w', ZIP_DEFLATED) as replacement:
        for item in original.infolist():
            raw = original.read(item)
            if item.filename == 'word/document.xml':
                raw = raw.replace(b'Intervening uncited paragraph.', b'Visible external edit.')
            replacement.writestr(item, raw)
    source.write_bytes(output.getvalue())
    edited = source.read_bytes()
    target = regenerate(source, tmp_path / 'snapshot.docx')
    with ZipFile(target) as regenerated:
        raw = regenerated.read('word/document.xml')
        assert b'Visible external edit.' not in raw
        assert b'Intervening uncited paragraph.' in raw
    assert source.read_bytes() == edited


def test_queued_destination_parent_replacement_is_rejected_before_render(tmp_path, monkeypatch):
    from carr_library.research.export_file import capture_export_destination
    source, _ = make_document(tmp_path)
    parent = tmp_path / 'chosen-folder'
    parent.mkdir()
    target = parent / 'output.docx'
    private = tmp_path / 'private-library'
    approval = capture_export_destination(target, private_root=private)
    parent.rename(tmp_path / 'original-folder')
    parent.mkdir()
    monkeypatch.setattr(regeneration, 'citation_document',
                        lambda *a, **k: pytest.fail('Changed parent reached the processor'))
    with pytest.raises(ValueError, match='destination changed'):
        regenerate_citation_docx(source, target, private_root=private, approval=approval)
    assert not target.exists()
