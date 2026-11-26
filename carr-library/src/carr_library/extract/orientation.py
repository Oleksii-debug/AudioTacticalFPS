"""Optional local orientation evidence; never changes source or verifies a quote."""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class Orientation:
    status: str
    clockwise: int | None = None
    confidence: float | None = None
    raw_osd: str = ''
    engine: str = 'Tesseract OSD via pytesseract'


def detect_orientation(image, *, timeout=15, min_confidence=15.0, max_pixels=20_000_000):
    """Use upstream OSD on a bounded, already-rendered PIL image, not a path.

    A candidate is not a verified orientation. Short pages often lack enough
    characters; preserve the unrotated source and do not guess in that case.
    """
    from PIL import Image
    if not isinstance(image, Image.Image):
        raise TypeError('expected a rendered PIL image')
    if not (isfinite(timeout) and 0 < timeout <= 120 and
            isfinite(min_confidence) and min_confidence >= 0 and
            type(max_pixels) is int and 0 < max_pixels <= 20_000_000):
        raise ValueError('invalid orientation limits')
    if image.width <= 0 or image.height <= 0 or image.width * image.height > max_pixels:
        return Orientation('render_limit')
    try:
        import pytesseract
    except ImportError:
        return Orientation('wrapper_missing')
    try:
        raw = pytesseract.image_to_osd(image, lang='osd', config='--dpi 300', timeout=timeout)
    except pytesseract.TesseractNotFoundError:
        return Orientation('engine_missing')
    except (OSError, RuntimeError):
        return Orientation('osd_failed_or_timed_out')
    # Only the two required fixed English fields are interpreted; retain raw OSD.
    fields = {}
    for line in raw.splitlines():
        key, sep, value = line.partition(':')
        if sep and key in ('Rotate', 'Orientation confidence'):
            if key in fields:
                return Orientation('invalid_osd', raw_osd=raw)
            fields[key] = value.strip()
    try:
        angle = int(fields['Rotate'])
        confidence = float(fields['Orientation confidence'])
    except (KeyError, ValueError):
        return Orientation('invalid_osd', raw_osd=raw)
    if angle not in (0, 90, 180, 270) or not isfinite(confidence) or confidence < 0:
        return Orientation('invalid_osd', raw_osd=raw)
    if confidence < min_confidence:
        return Orientation('uncertain', confidence=confidence, raw_osd=raw)
    return Orientation('candidate', angle, confidence, raw)


def candidate_image(image, evidence):
    """Explicit opt-in transform; OCR boxes refer to returned image pixels.

    Persist clockwise angle, original/rendered dimensions and raw OSD with OCR
    output. Do not reuse rotated bounding boxes as unrotated source coordinates.
    """
    from PIL import Image
    if not isinstance(evidence, Orientation) or evidence.status != 'candidate':
        raise ValueError('orientation is not a candidate')
    transforms = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
                  270: Image.Transpose.ROTATE_90}
    if evidence.clockwise == 0:
        return image.copy()
    if evidence.clockwise not in transforms:
        raise ValueError('invalid right-angle rotation')
    return image.transpose(transforms[evidence.clockwise])
