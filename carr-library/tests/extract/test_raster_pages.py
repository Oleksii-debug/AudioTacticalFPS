"""Actual Pillow containers and exact pixel provenance; OCR accuracy is separate."""
from hashlib import sha256
from io import BytesIO
import json
import struct
import threading

from PIL import Image, ImageOps
import pytest

from carr_library.catalog.intake import classify, snapshot
from carr_library.extract.document_formats import (file_dialog_wildcard, MIME_SUFFIXES,
    PHYSICAL_MIMES, RASTER_MIMES)
from carr_library.extract.page_ocr import Config
from carr_library.extract import page_ocr, raster_pages


def image_bytes(format='PNG', *, size=(24, 12), color='red', orientation=None):
    with Image.new('RGB', size, color) as image:
        options = {}
        if orientation is not None:
            exif = Image.Exif()
            exif[274] = orientation
            options['exif'] = exif
        output = BytesIO()
        image.save(output, format=format, **options)
        return output.getvalue()


def tiff_bytes(colors=('red', 'blue'), *, size=(24, 12)):
    images = [Image.new('RGB', size, color) for color in colors]
    try:
        output = BytesIO()
        images[0].save(output, format='TIFF', save_all=True,
                       append_images=images[1:], compression='raw')
        return output.getvalue()
    finally:
        for image in images:
            image.close()


@pytest.mark.parametrize('format,suffix,mime', [
    ('JPEG', '.jpg', 'image/jpeg'), ('JPEG', '.jpeg', 'image/jpeg'),
    ('PNG', '.png', 'image/png'), ('TIFF', '.tif', 'image/tiff'),
    ('TIFF', '.tiff', 'image/tiff')])
def test_real_headers_registry_and_retained_identity(tmp_path, format, suffix, mime):
    data = image_bytes(format)
    path = tmp_path / ('Оригінал' + suffix)
    path.write_bytes(data)
    original = snapshot(tmp_path, path.name)
    assert original.data == data
    assert original.manifest() == {'sha256': sha256(data).hexdigest(),
                                   'mime': mime, 'size': len(data)}
    assert classify(data, suffix.upper()) == mime
    assert mime in RASTER_MIMES and mime in PHYSICAL_MIMES
    assert '*' + suffix in file_dialog_wildcard()
    assert MIME_SUFFIXES[mime] in ('.jpg', '.png', '.tif')
    frames = raster_pages.frame_inventory(data)
    assert len(frames) == 1 and frames[0]['index'] == 1
    assert (frames[0]['width'], frames[0]['height']) == (24, 12)


@pytest.mark.parametrize('format,suffix', [('PNG', '.png'), ('JPEG', '.jpg'), ('TIFF', '.tif')])
def test_malformed_container_is_not_admitted_as_a_source_page(format, suffix):
    data = image_bytes(format)[:12]
    # Header screening deliberately does not make parser or quotation claims.
    assert classify(data, suffix).startswith('image/')
    with pytest.raises((ValueError, EOFError)):
        raster_pages.frame_inventory(data)


@pytest.mark.parametrize('suffix', ['.txt', '.pdf', '.jpeg', '.tiff'])
def test_png_cannot_be_relabelled_as_another_format(suffix):
    with pytest.raises(ValueError, match='mismatched'):
        classify(image_bytes(), suffix)


def test_real_tiff_frame_order_and_independent_pixel_identity(tmp_path, monkeypatch):
    data = tiff_bytes(('red', 'blue', 'green'))
    path = tmp_path / 'ordered.tif'
    path.write_bytes(data)
    frames = raster_pages.frame_inventory(data)
    assert [frame['index'] for frame in frames] == [1, 2, 3]
    observed = []

    def recognize(result, image, config, deadline, **kwargs):
        observed.append(image.getpixel((0, 0)))
        result['raw_text'] = 'Synthetic OCR candidate\n'
        result['status'] = 'review_required'
        result['provenance']['method'] = 'ocr_candidate'
        return result

    monkeypatch.setattr(page_ocr, '_recognize_image', recognize)
    for index in (1, 2, 3):
        result = raster_pages.extract_raster_page(path, index)
        provenance = result['provenance']
        renderer, pixels = raster_pages.render_fingerprint(data, index)
        assert result['physical_index'] == index
        assert result['printed_label'] is None and result['printed_label_verified'] is False
        assert result['status'] == 'review_required'
        assert result['raw_text'] == 'Synthetic OCR candidate\n'
        assert provenance['asset_sha256'] == sha256(data).hexdigest()
        assert provenance['source_unit'] == 'TIFF frame'
        assert provenance['frame'] == index and provenance['frame_count'] == 3
        assert provenance['frame_inventory_sha256'] == raster_pages.inventory_sha256(frames)
        assert provenance['renderer'] == renderer
        assert provenance['decoded_pixels_sha256'] == pixels
    assert observed == [(255, 0, 0), (0, 0, 255), (0, 128, 0)]
    assert len({raster_pages.render_fingerprint(data, n)[1] for n in (1, 2, 3)}) == 3
    assert path.read_bytes() == data


@pytest.mark.parametrize('format', ['JPEG', 'TIFF'])
def test_actual_exif_orientation_is_bound_to_normalized_pixels(format):
    data = image_bytes(format, size=(30, 10), orientation=6)
    frame, = raster_pages.frame_inventory(data)
    assert frame['orientation'] == 6
    assert (frame['width'], frame['height']) == (10, 30)
    with Image.open(BytesIO(data)) as original:
        with ImageOps.exif_transpose(original) as oriented:
            with oriented.convert('RGB') as expected:
                digest = sha256(struct.pack('>II', *expected.size) + expected.tobytes()).hexdigest()
    renderer, actual = raster_pages.render_fingerprint(data, 1)
    assert actual == digest and format in renderer
    assert 'pillow-exif-rgb-white-v1' in renderer


def test_transparent_pixels_are_composited_over_explicit_white():
    with Image.new('RGBA', (12, 6), (0, 0, 0, 0)) as image:
        output = BytesIO()
        image.save(output, format='PNG')
    with Image.new('RGB', (12, 6), 'white') as expected:
        digest = sha256(struct.pack('>II', *expected.size) + expected.tobytes()).hexdigest()
    assert raster_pages.render_fingerprint(output.getvalue(), 1)[1] == digest


def test_selected_pixel_bound_refuses_before_decoding(tmp_path, monkeypatch):
    path = tmp_path / 'bounded.png'
    path.write_bytes(image_bytes(size=(80, 40)))
    monkeypatch.setattr(raster_pages, '_rgb',
        lambda *args, **kwargs: pytest.fail('Pixel bound must precede image decoding.'))
    with pytest.raises(ValueError, match='pixel limit'):
        raster_pages.extract_raster_page(path, 1, Config(max_pixels=100))


def test_frame_and_byte_bounds_reject_real_containers(monkeypatch):
    data = tiff_bytes(('red', 'blue', 'green'))
    monkeypatch.setattr(raster_pages, 'MAX_FRAMES', 2)
    with pytest.raises(ValueError, match='frame limit'):
        raster_pages.frame_inventory(data)
    monkeypatch.setattr(raster_pages, 'MAX_BYTES', len(data) - 1)
    with pytest.raises(ValueError, match='byte limit'):
        raster_pages.frame_inventory(data)


def test_animated_png_is_refused_without_silently_importing_first_frame():
    with Image.new('RGB', (24, 12), 'red') as first:
        with Image.new('RGB', (24, 12), 'blue') as second:
            stream = BytesIO()
            first.save(stream, format='PNG', save_all=True, append_images=[second],
                       duration=100, loop=0)
    with pytest.raises(ValueError, match='Animated'):
        raster_pages.frame_inventory(stream.getvalue())


def test_cancel_during_real_decode_keeps_original_and_never_calls_ocr(tmp_path, monkeypatch):
    data = image_bytes()
    path = tmp_path / 'cancel.png'
    path.write_bytes(data)
    stopped = threading.Event()
    transpose = ImageOps.exif_transpose

    def cancel_after_decode(image, **kwargs):
        result = transpose(image, **kwargs)
        stopped.set()
        return result

    monkeypatch.setattr(ImageOps, 'exif_transpose', cancel_after_decode)
    monkeypatch.setattr(page_ocr, '_recognize_image',
        lambda *args, **kwargs: pytest.fail('Cancelled image must not reach OCR.'))
    with pytest.raises(InterruptedError, match='cancelled'):
        raster_pages.extract_raster_page(path, 1, cancelled=stopped.is_set)
    assert stopped.is_set() and path.read_bytes() == data


def test_cancelled_or_expired_inventory_never_opens_image(monkeypatch):
    monkeypatch.setattr(Image, 'open',
        lambda *args, **kwargs: pytest.fail('Admission must precede any image parser.'))
    with pytest.raises(InterruptedError):
        raster_pages.frame_inventory(b'not opened', cancelled=lambda: True)
    with pytest.raises(TimeoutError):
        raster_pages.frame_inventory(b'not opened', deadline=-1)


def test_mid_inventory_cancel_is_not_converted_to_malformed_image():
    calls = 0

    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 4

    with pytest.raises(InterruptedError, match='cancelled'):
        raster_pages.frame_inventory(tiff_bytes(('red', 'blue', 'green')), cancelled=cancelled)
    assert calls == 4


def test_stored_raw_text_and_layout_keep_shared_ocr_review_policy(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    path = tmp_path / 'raw.png'
    path.write_bytes(image_bytes(size=(240, 120)))

    class Runtime:
        executable = 'synthetic-tesseract'
        portable = managed = False

        def assert_unchanged(self, remaining):
            remaining()

    monkeypatch.setattr(page_ocr, 'discover_ocr_runtime', lambda *a, **k: Runtime())
    raw = 'Exact “quote” — Міст\n'

    def run(runtime, command, *, remaining):
        remaining()
        if '--version' in command:
            return SimpleNamespace(stdout=b'tesseract 5.5.0\n')
        output = Path(command[2])
        output.with_suffix('.txt').write_text(raw, encoding='utf-8', newline='')
        output.with_suffix('.tsv').write_text(
            '\t'.join(page_ocr._TSV_FIELDS) + '\n'
            '5\t1\t1\t1\t1\t1\t10\t10\t80\t30\t95\tExact\n',
            encoding='utf-8', newline='')
        return SimpleNamespace(stdout=b'')

    monkeypatch.setattr(page_ocr, 'run_ocr', run)
    result = raster_pages.extract_raster_page(path, 1)
    assert result['raw_text'] == result['provenance']['ocr_text'] == raw
    assert result['status'] == 'review_required'
    assert 'ocr_not_verified' in result['warnings']
    assert result['blocks'][0]['bbox'] == (10, 10, 80, 30)
    assert result['provenance']['ocr_coordinate_frame'] == 'source_render_pixels_top_left'
    assert result['printed_label'] is None and result['printed_label_verified'] is False
