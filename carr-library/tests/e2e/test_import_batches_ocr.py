"""Actual scanned batch acceptance with the prepared local OCR runtime."""
import pytest

from carr_library.application import LibraryApp
from carr_library.extract.ocr_runtime import discover_ocr_runtime
from carr_library.ingest import worker


@pytest.mark.skipif(discover_ocr_runtime() is None, reason='local OCR engine unavailable')
def test_quarantined_batch_waits_for_explicit_review_and_refreshes_external_completion(
        tmp_path, monkeypatch):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas
    app = LibraryApp(tmp_path / 'private-library')
    original = tmp_path / 'scanned.pdf'
    image = Image.new('RGB', (1200, 300), 'white')
    ImageDraw.Draw(image).text((30, 70), 'Railway batch source',
                               font=ImageFont.load_default(size=72), fill='black')
    canvas = Canvas(str(original))
    canvas.drawImage(ImageReader(image), 50, 500, 500, 125)
    canvas.save()
    workspace = app.create_workspace('Reviewed batch')
    planned = app.plan_import_batch(workspace, [original])
    pending_batch = app.run_import_batch(workspace, planned['batch_id'])
    assert pending_batch['counts']['quarantined'] == 1
    assert app.browse(workspace) == ()
    def no_implicit_retry(*args, **kwargs):
        pytest.fail('Quarantined requests require an explicit retry or review action.')
    with monkeypatch.context() as patch:
        patch.setattr(worker, '_invoke', no_implicit_retry)
        assert app.run_import_batch(workspace, planned['batch_id'])['counts']['quarantined'] == 1
    request_id = planned['rows'][0]['request_id']
    pending = app.pending_review(workspace, request_id)
    assert pending['raw_text'].strip() == 'Railway batch source'
    app.confirm_import_review(workspace, request_id, 'Synthetic reviewer', confirmed=True,
                              expected_checksum=pending['checksum'])
    assert app.resume_import(workspace, request_id)['state'] == 'complete'
    refreshed = LibraryApp(app.root).import_batch(workspace, planned['batch_id'])
    assert refreshed['counts']['complete'] == 1
    for key in ('row_id', 'work_id', 'edition_id', 'request_id'):
        assert refreshed['rows'][0][key] == planned['rows'][0][key]
    hit, = app.search(workspace, 'Railway')
    assert hit.verified and app.copy(workspace, hit.ref, hit.revision, 'quote') == 'Railway'
