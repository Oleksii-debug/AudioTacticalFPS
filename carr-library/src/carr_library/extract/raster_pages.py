"""Bounded retained JPEG/PNG/TIFF frames for the existing isolated OCR runner.

Pillow is already pinned by the product. No image/model download, new OCR
engine, printed-page inference or independent text approval is performed here.
"""
from contextlib import contextmanager
from hashlib import sha256
from io import BytesIO
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import struct
import time
import warnings

from .document_formats import raster_format
from .page_ocr import Config
from ..catalog.intake import MAX_BYTES, snapshot

MAX_FRAMES = 1000
MAX_PIXELS = 20_000_000
NORMALIZATION = 'pillow-exif-rgb-white-v1'


def _check(deadline, cancelled):
    if cancelled():
        raise InterruptedError('Raster source processing cancelled.')
    if time.monotonic() >= deadline:
        raise TimeoutError('Raster source processing reached its time limit.')


def _source(data):
    if type(data) is not bytes or not 0 < len(data) <= MAX_BYTES:
        raise ValueError('Raster source exceeds its retained byte limit.')
    selected = raster_format(data)
    if selected is None:
        raise ValueError('Unsupported raster image header.')
    return selected


@contextmanager
def _open(data):
    from PIL import Image
    selected = _source(data)
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        try:
            with Image.open(BytesIO(data), formats=[selected.name]) as image:
                if image.format != selected.name:
                    raise ValueError('Image container changed format; multi-picture JPEG is unsupported.')
                yield image, selected
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ValueError('Raster dimensions exceed the decoder limit.') from exc
        except (InterruptedError, TimeoutError):
            raise
        except (OSError, SyntaxError, KeyError, TypeError) as exc:
            raise ValueError('Malformed or unsupported raster image.') from exc


def _frame(image, index, limit):
    width, height = image.size
    if (type(width) is not int or type(height) is not int or width < 1 or height < 1
            or width * height > limit):
        raise ValueError('Raster frame exceeds the selected pixel limit.')
    orientation = image.getexif().get(274, 1)
    if type(orientation) is not int or not 1 <= orientation <= 8:
        raise ValueError('Unsupported image orientation metadata.')
    # Pillow's TIFF loader already exposes its oriented display dimensions.
    # JPEG/PNG are transposed explicitly by the same ImageOps call at decode.
    size = (height, width) if image.format != 'TIFF' and orientation in (5, 6, 7, 8) else (width, height)
    return dict(index=index, width=size[0], height=size[1], mode=image.mode,
                orientation=orientation)


def frame_inventory(data, config=Config(), *, cancelled=lambda: False, deadline=None):
    """Visit at most 1001 frame headers; never ask Pillow for unbounded n_frames."""
    if not callable(cancelled):
        raise ValueError('A raster cancellation callback is required.')
    deadline = time.monotonic() + config.timeout if deadline is None else deadline
    _check(deadline, cancelled)
    frames = []
    with _open(data) as (image, selected):
        for index in range(1, MAX_FRAMES + 2):
            _check(deadline, cancelled)
            try:
                image.seek(index - 1)
            except EOFError:
                break
            if index > MAX_FRAMES:
                raise ValueError('Raster source exceeds the physical frame limit.')
            if selected.name != 'TIFF' and index > 1:
                raise ValueError('Animated or multi-picture images are unsupported; no frames were omitted.')
            frames.append(_frame(image, index, config.max_pixels))
        _check(deadline, cancelled)
    if not frames:
        raise ValueError('Raster source contains no image frames.')
    return tuple(frames)


def inventory_sha256(frames):
    return sha256(json.dumps(frames, sort_keys=True, ensure_ascii=False,
                            separators=(',', ':')).encode('utf-8')).hexdigest()



def validate_checkpoint(value, settings, ordinal, count):
    """Passive typed validation shared by import resume and complete backup."""
    provenance = value.get('provenance')
    if not isinstance(provenance, dict):
        raise ValueError('Raster checkpoint has no physical-frame provenance.')
    size = provenance.get('render_size')
    digest = provenance.get('decoded_pixels_sha256')
    raw = value.get('raw_text')
    if (provenance.get('source_format') != settings['format']
            or provenance.get('source_unit') != ('TIFF frame' if settings['format'] == 'TIFF' else 'single image')
            or type(provenance.get('frame')) is not int or provenance['frame'] != ordinal
            or type(provenance.get('frame_count')) is not int or provenance['frame_count'] != count
            or provenance.get('frame_inventory_sha256') != settings['frame_inventory_sha256']
            or provenance.get('config') != settings['config']
            or provenance.get('normalization') != settings['normalization']
            or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
            or type(provenance.get('source_orientation')) is not int
            or not 1 <= provenance['source_orientation'] <= 8
            or not isinstance(size, list) or len(size) != 2
            or any(type(n) is not int or n < 1 for n in size)
            or size[0] * size[1] > settings['config'].get('max_pixels', MAX_PIXELS)):
        raise ValueError('Raster checkpoint differs from its physical-frame identity.')
    if (value.get('status') not in ('blank', 'review_required')
            or not isinstance(raw, str)
            or (value['status'] == 'blank' and (raw != '' or provenance.get('method') != 'render_blank'))
            or (raw and (provenance.get('method') != 'ocr_candidate'
                         or provenance.get('ocr_text') != raw))):
        raise ValueError('Raster checkpoint cannot authorize native or edited text.')


def _rgb(image, expected, deadline, cancelled):
    from PIL import Image, ImageOps
    _check(deadline, cancelled)
    # Load one bounded frame. TIFF applies its stored orientation during load;
    # exif_transpose removes only the remaining JPEG/PNG orientation transform.
    with ImageOps.exif_transpose(image) as oriented:
        _check(deadline, cancelled)
        if oriented.size != (expected['width'], expected['height']):
            raise ValueError('Decoded image dimensions differ from its reviewed header.')
        with oriented.convert('RGBA') as rgba:
            with Image.new('RGBA', rgba.size, (255, 255, 255, 255)) as background:
                with Image.alpha_composite(background, rgba) as displayed:
                    result = displayed.convert('RGB')
    try:
        _check(deadline, cancelled)
        return result
    except BaseException:
        result.close()
        raise


def _pixel_hash(image):
    return sha256(struct.pack('>II', *image.size) + image.tobytes()).hexdigest()


def _renderer(selected):
    return 'Pillow/' + metadata.version('Pillow') + '/' + selected.name + '/' + NORMALIZATION


def extract_raster_page(path, physical_index, config=Config(), *, cancelled=lambda: False):
    """Extract one exact physical frame through page_ocr's existing recognizer."""
    from . import page_ocr
    deadline = time.monotonic() + config.timeout
    path = Path(path)
    original = snapshot(path.parent, path.name, cancelled=cancelled)
    selected = _source(original.data)
    frames = frame_inventory(original.data, config, cancelled=cancelled, deadline=deadline)
    if type(physical_index) is not int or not 1 <= physical_index <= len(frames):
        raise ValueError('Invalid physical image frame.')
    expected = frames[physical_index - 1]
    with _open(original.data) as (container, actual):
        _check(deadline, cancelled)
        container.seek(physical_index - 1)
        if actual != selected or _frame(container, physical_index, config.max_pixels) != expected:
            raise ValueError('Raster frame inventory changed.')
        with _rgb(container, expected, deadline, cancelled) as image:
            _check(deadline, cancelled)
            provenance = dict(asset_sha256=original.digest, native_text='',
                renderer=_renderer(selected), render_size=image.size,
                config=vars(config).copy(), source_format=selected.name,
                source_unit='TIFF frame' if selected.name == 'TIFF' else 'single image',
                frame=physical_index, frame_count=len(frames),
                frame_inventory_sha256=inventory_sha256(frames),
                source_orientation=expected['orientation'],
                normalization=NORMALIZATION, decoded_pixels_sha256=_pixel_hash(image))
            result = dict(physical_index=physical_index, printed_label=None,
                printed_label_verified=False, raw_text='', blocks=[], warnings=[],
                provenance=provenance)
            _check(deadline, cancelled)
            if all(lo == 255 for lo, hi in image.getextrema()):
                result['status'] = 'blank'
                provenance['method'] = 'render_blank'
            else:
                result['warnings'].append('ocr_required')
                result = page_ocr._recognize_image(result, image, config, deadline,
                                                   cancelled=cancelled)
            _check(deadline, cancelled)
            return result


def render_fingerprint(data, physical, *, cancelled=lambda: False):
    """Independent review of the same normalized pixels; never recognizes text."""
    config = Config()
    deadline = time.monotonic() + config.timeout
    frames = frame_inventory(data, config, cancelled=cancelled, deadline=deadline)
    if type(physical) is not int or not 1 <= physical <= len(frames):
        raise ValueError('Invalid physical image frame.')
    expected = frames[physical - 1]
    with _open(data) as (container, selected):
        _check(deadline, cancelled)
        container.seek(physical - 1)
        if _frame(container, physical, MAX_PIXELS) != expected:
            raise ValueError('Raster review frame inventory changed.')
        with _rgb(container, expected, deadline, cancelled) as image:
            result = _renderer(selected), _pixel_hash(image)
            _check(deadline, cancelled)
            return result
