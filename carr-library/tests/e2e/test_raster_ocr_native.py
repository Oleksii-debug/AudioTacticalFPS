"""Actual isolated JPEG/PNG/TIFF -> installed Tesseract -> reviewed-source flow.

Select this qualification suite after the existing OCR runtime setup. A missing
runtime is NOT_RUN, not an OCR or native-platform success.
"""
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from PIL import Image, ImageDraw, ImageFont
import pytest

from carr_library.application import LibraryApp
from carr_library.extract.ocr_runtime import discover_ocr_runtime
from carr_library.ingest.worker import load_request
from carr_library.library_backup import create_backup, restore_backup


@pytest.fixture
def ocr_runtime():
    runtime = discover_ocr_runtime()
    assert runtime is not None, (
        'Actual installed local OCR runtime is required for this qualification suite; '
        'run the existing OCR setup before selecting it.')
    return runtime


def original(path, format):
    texts = ['Railway raster one', 'Railway raster two'] if format == 'TIFF' else ['Railway raster one']
    pages = []
    try:
        for text in texts:
            image = Image.new('RGB', (1600, 300), 'white')
            ImageDraw.Draw(image).text((30, 70), text,
                font=ImageFont.load_default(size=72), fill='black')
            pages.append(image)
        options = (dict(save_all=True, append_images=pages[1:], compression='tiff_deflate')
                   if format == 'TIFF' else dict(quality=95) if format == 'JPEG' else {})
        pages[0].save(path, format=format, **options)
    finally:
        for image in pages:
            image.close()
    return texts


@pytest.mark.parametrize('format,suffix', [('JPEG', '.jpg'), ('PNG', '.png'), ('TIFF', '.tiff')])
def test_actual_isolated_ocr_review_and_restored_source(tmp_path, ocr_runtime, format, suffix):
    source = tmp_path / ('Actual scan' + suffix)
    expected = original(source, format)
    raw = source.read_bytes()
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Real raster OCR')
    result = app.import_file(workspace, source, title='Real retained raster')
    request_id = result['request_id']
    for physical, text in enumerate(expected, 1):
        assert result['state'] == 'quarantined' and result['isolated'] is True
        assert app.browse(workspace) == ()
        pending = app.pending_review(workspace, request_id)
        assert pending['physical'] == physical
        assert pending['raw_text'].strip() == text
        assert pending['renderer'].startswith('Pillow/')
        app.confirm_import_review(workspace, request_id, 'Synthetic original comparison',
                                  confirmed=True, expected_checksum=pending['checksum'])
        # Remove the external original before resuming to prove retained-byte use.
        source.unlink(missing_ok=True)
        result = app.resume_import(workspace, request_id)
    assert result['state'] == 'complete' and result['completed'] == len(expected)
    assert app.assets.resolve(sha256(raw).hexdigest()).read_bytes() == raw
    with sqlite3.connect(app.root / 'imports.sqlite') as db:
        settings = json.loads(db.execute('SELECT config FROM job WHERE id=?',
                                        (result['id'],)).fetchone()[0])
        records = [json.loads(row[0]) for row in db.execute(
            'SELECT payload FROM checkpoint WHERE job=? ORDER BY ordinal', (result['id'],))]
    assert settings['adapter'] == 'raster_ocr-v1'
    assert settings['runtime_identity']['packages']['Pillow']
    assert 'raster_pages.py' in settings['runtime_identity']['source_sha256']
    assert [item['raw_text'].strip() for item in records] == expected
    assert all(item['provenance']['ocr_tsv'] for item in records)
    assert all(item['printed_label'] is None and not item['printed_label_verified'] for item in records)
    digest = create_backup(app, tmp_path / 'backup')
    restored = LibraryApp(restore_backup(tmp_path / 'backup', tmp_path / 'restored', digest))
    hits = restored.search(workspace, 'Railway')
    assert len(hits) == len(expected) and all(hit.verified for hit in hits)
    assert all('Physical page' in hit.page and 'printed page unverified' in hit.page for hit in hits)
    assert load_request(restored.root, request_id)['job'] == result['id']
    assert restored.resume_import(workspace, request_id)['state'] == 'complete'
