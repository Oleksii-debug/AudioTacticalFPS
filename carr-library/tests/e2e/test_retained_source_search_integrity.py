"""Retained source corruption is an integrity error, never a review downgrade.

These tests use the canonical desktop service, real SQLite, retained originals,
and synthetic native PDF/TXT imports. They do not run a model or approve PDF
text against its rendered page.
"""
from threading import Event

import pytest
from reportlab.pdfgen.canvas import Canvas

import carr_library.application as application
from carr_library.application import LibraryApp


@pytest.fixture
def mixed(tmp_path):
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Integrity fixture')
    other = app.create_workspace('Other library')
    collection = app.create_collection(workspace, 'Selected sources')
    text = app.add_text(workspace, 'TXT source', 'Railway exact retained text.',
                        collections=(collection,))
    pdf = tmp_path / 'synthetic.pdf'
    canvas = Canvas(str(pdf), invariant=1, pageCompression=0)
    for page in range(2):
        canvas.drawString(40, 700, f'Railway PDFonly page {page + 1}: synthetic native text.')
        canvas.showPage()
    canvas.save()
    imported = app.import_file(workspace, pdf, title='PDF source',
                               collections=(collection,))
    assert imported['state'] == 'complete' and imported['verified'] is False
    return app, workspace, other, collection, text, imported


def test_intact_mixed_sources_remain_searchable_without_pdf_approval(mixed):
    app, workspace, _, _, _, _ = mixed
    hits = app.search(workspace, 'Railway')
    assert len(hits) == 3
    assert next(hit for hit in hits if hit.title == 'TXT source').verified is True
    pdfs = [hit for hit in hits if hit.title == 'PDF source']
    assert len(pdfs) == 2 and all(hit.verified is False for hit in pdfs)
    assert all('independent review' in hit.citation for hit in pdfs)
    assert 'PDFonly page 1' in app.preview(workspace, pdfs[0].ref, pdfs[0].revision)
    with pytest.raises(ValueError, match='unverified extraction'):
        app.copy(workspace, pdfs[0].ref, pdfs[0].revision, 'quote')


@pytest.mark.parametrize('kind', ['TXT source', 'PDF source'])
@pytest.mark.parametrize('mutation', ['same_size', 'append', 'missing'])
def test_source_drift_blocks_search_and_cached_preview_without_downgrade(mixed, kind, mutation):
    app, workspace, _, _, text, pdf = mixed
    selected = next(hit for hit in app.search(workspace, 'Railway') if hit.title == kind)
    original = app.assets.resolve((text if kind == 'TXT source' else pdf)['asset'])
    data = original.read_bytes()
    if mutation == 'same_size':
        changed = data.replace(b'Railway', b'Highway', 1)
        assert changed != data and len(changed) == len(data)
        original.write_bytes(changed)
    elif mutation == 'append':
        original.write_bytes(data + b'\n% source identity changed\n')
    else:
        original.unlink()
    previous_selections = dict(app._selections)
    with pytest.raises(ValueError, match='retained|original|source'):
        app.search(workspace, 'Railway')
    assert app._selections == previous_selections
    with pytest.raises(ValueError, match='retained|original|source'):
        app.preview(workspace, selected.ref, selected.revision)
    with pytest.raises(ValueError):
        app.copy(workspace, selected.ref, selected.revision, 'quote')


def test_validation_cache_is_per_search_and_does_not_repeat_each_pdf_page(mixed, monkeypatch):
    app, workspace, _, _, _, pdf = mixed
    original = app.assets.resolve
    calls = []

    def resolve(digest, **options):
        calls.append(digest)
        return original(digest, **options)

    monkeypatch.setattr(app.assets, 'resolve', resolve)
    assert len(app.search(workspace, 'PDFonly')) == 2
    assert calls == [pdf['asset']]
    assert len(app.search(workspace, 'PDFonly')) == 2
    assert calls == [pdf['asset'], pdf['asset']]


def test_cancel_during_original_validation_keeps_previous_complete_selection(mixed, monkeypatch):
    app, workspace, _, _, _, _ = mixed
    previous = app.search(workspace, 'Railway')
    previous_selections = dict(app._selections)
    original = app.assets.resolve
    stopped = Event()

    def resolve(digest, **options):
        path = original(digest, **options)
        stopped.set()
        return path

    monkeypatch.setattr(app.assets, 'resolve', resolve)
    with pytest.raises(TimeoutError, match='cancelled|timed out'):
        app.search(workspace, 'PDFonly', cancelled=stopped.is_set)
    assert previous and app._selections == previous_selections


def test_original_validation_consumes_existing_search_deadline(mixed, monkeypatch):
    app, workspace, _, _, _, _ = mixed
    current = [100.0]
    original = app.assets.resolve

    def resolve(digest, **options):
        path = original(digest, **options)
        current[0] += 2.0
        return path

    monkeypatch.setattr(application, 'monotonic', lambda: current[0])
    monkeypatch.setattr(app.assets, 'resolve', resolve)
    with pytest.raises(TimeoutError, match='cancelled|timed out'):
        app.search(workspace, 'PDFonly', timeout=1.0)
    assert app._selections == {}


def test_unauthorized_preview_does_not_read_an_original(mixed, monkeypatch):
    app, workspace, other, collection, _, pdf = mixed
    hit = app.search(workspace, 'PDFonly', collection=collection)[0]
    with app.catalog_transaction(workspace) as catalog:
        assert catalog.remove(collection, pdf['edition'])

    def unexpected(*args):
        pytest.fail('Source authorization must precede access to retained bytes.')

    monkeypatch.setattr(app.assets, 'resolve', unexpected)
    with pytest.raises(ValueError, match='active library'):
        app.preview(other, hit.ref, hit.revision)
    with pytest.raises(ValueError, match='left the selected collection'):
        app.preview(workspace, hit.ref, hit.revision)
