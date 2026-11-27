"""Actual optional OCR qualification for the typed pending-import producer."""
import shutil

import pytest

from test_pending_backup import app, _snapshot, _restore


@pytest.mark.skipif(shutil.which('tesseract') is None, reason='Actual local OCR engine required')
def test_actual_reviewed_ocr_checkpoint_retains_attestation_and_resumes(app, tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas
    original = tmp_path / 'scanned.pdf'
    image = Image.new('RGB', (1200, 300), 'white')
    ImageDraw.Draw(image).text((30, 70), 'Railway backup source',
                               font=ImageFont.load_default(size=72), fill='black')
    canvas = Canvas(str(original))
    canvas.drawImage(ImageReader(image), 50, 500, 500, 125)
    canvas.save()
    workspace = app.create_workspace('Reviewed pending original')
    pending = app.import_file(workspace, original)
    assert pending['state'] == 'quarantined'
    request_id = pending['request_id']
    review = app.pending_review(workspace, request_id)
    assert 'Railway' in review['raw_text']
    app.confirm_import_review(workspace, request_id, 'Synthetic test reviewer', confirmed=True,
                              expected_checksum=review['checksum'])
    before = (app.root / 'import-requests' / 'reviews' / (request_id + '.json')).read_bytes()
    backup, metadata = _snapshot(app, tmp_path)
    original.unlink()
    restored, _ = _restore(backup, metadata, tmp_path)
    assert (restored.root / 'import-requests' / 'reviews' / (request_id + '.json')).read_bytes() == before
    assert restored.pending_review(workspace, request_id)['checksum'] == review['checksum']
    final = restored.resume_import(workspace, request_id)
    assert final['state'] == 'complete' and final['id'] == pending['id']
    hit, = restored.search(workspace, 'Railway')
    assert hit.verified
