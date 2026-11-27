"""Page-preserving adapter over pypdf, PDFium and Tesseract; trusted inputs only."""
from dataclasses import dataclass
from contextlib import closing
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path
import csv
import importlib.metadata as metadata
import math
import subprocess
import tempfile
import time

from .pinned_tessdata import MODELS, POLICY, stage_pinned_models
from .ocr_runtime import OcrRuntimeError, discover_ocr_runtime
from .ocr_languages import model_ids


_MAX_OCR_OUTPUT_BYTES = 16 * 1024 * 1024
_TSV_FIELDS = ('level', 'page_num', 'block_num', 'par_num', 'line_num',
               'word_num', 'left', 'top', 'width', 'height', 'conf', 'text')


class _OcrOutputLimit(ValueError):
    pass


def _read_ocr_output(path):
    # Match the ingestion checkpoint ceiling before reading an unbounded file.
    # The journal still checks the size of the complete serialized checkpoint.
    with path.open('rb') as stream:
        data = stream.read(_MAX_OCR_OUTPUT_BYTES + 1)
    if len(data) > _MAX_OCR_OUTPUT_BYTES:
        raise _OcrOutputLimit('OCR output exceeds checkpoint limit')
    return data.decode('utf-8')


def _ocr_blocks(tsv, render_size):
    """Decode Tesseract's unquoted TSV without changing recognized punctuation."""
    rows = csv.DictReader(StringIO(tsv, newline=''), delimiter='\t',
                          quoting=csv.QUOTE_NONE)
    if tuple(rows.fieldnames or ()) != _TSV_FIELDS:
        raise ValueError('unexpected OCR layout header')
    width, height = render_size
    blocks = []
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise ValueError('incomplete OCR layout row')
        level = int(row['level'])
        if not 1 <= level <= 5 or int(row['page_num']) != 1:
            raise ValueError('invalid OCR layout page')
        if level != 5 or not row['text']:
            continue
        confidence = float(row['conf'])
        left, top, box_width, box_height = (int(row[k]) for k in ('left', 'top', 'width', 'height'))
        line = tuple(int(row[k]) for k in ('block_num', 'par_num', 'line_num'))
        if (not math.isfinite(confidence) or not -1 <= confidence <= 100
                or not (0 <= left < width and 0 <= top < height)
                or not (0 < box_width <= width - left and 0 < box_height <= height - top)
                or any(part < 1 for part in line) or int(row['word_num']) < 1
                or '\x00' in row['text']):
            raise ValueError('invalid OCR layout word')
        blocks.append(dict(text=row['text'], confidence=confidence,
                           bbox=(left, top, box_width, box_height), line=line))
    return blocks


@dataclass(frozen=True)
class Config:
    languages: str = 'eng'
    dpi: int = 300
    timeout: int = 30
    max_pixels: int = 20_000_000
    orientation: bool = False
    model_policy: str = 'default'
    model_profile: str = 'installed'

    def __post_init__(self):
        if type(self.orientation) is not bool:
            raise ValueError('orientation must be boolean')
        selected = model_ids(self.languages)
        if len(self.languages) > 2048 or len(set(selected)) != len(selected):
            raise ValueError('duplicate or oversized language selection')
        if self.model_policy not in ('default', POLICY):
            raise ValueError('unknown OCR model policy')
        if self.model_profile not in ('installed', 'fast', 'best'):
            raise ValueError('unsupported OCR model profile')
        if self.model_policy == POLICY and self.model_profile == 'best':
            raise ValueError('the pinned Fast policy cannot use the Best model profile')
        if self.model_policy == POLICY:
            if len(set(selected)) != len(selected) or any(lang not in MODELS for lang in selected):
                raise ValueError('unsupported pinned OCR language')
        if not 72 <= self.dpi <= 400 or not 1 <= self.timeout <= 120 or not 1 <= self.max_pixels <= 20_000_000:
            raise ValueError('invalid resource limit')


def extract_page(asset, physical_index, config=Config()):
    """Return a candidate, never a verified printed label or corrected quote.

    Baseline OCR TSV coordinates are pixels in the original rendered image.
    Optional orientation evidence has its own pixel coordinate frame.
    Parser isolation and durable job checkpoints belong to the ingest runner.
    """
    deadline = time.monotonic() + config.timeout
    from pypdf import PdfReader
    import pypdfium2 as pdfium
    with Path(asset).open('rb') as f:
        data = f.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError('source too large')
    reader = PdfReader(BytesIO(data))
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 1000:
        raise ValueError('encrypted or oversized PDF')
    if type(physical_index) is not int or not 1 <= physical_index <= len(reader.pages):
        raise ValueError('invalid physical index')
    source = reader.pages[physical_index - 1]
    native = source.extract_text() or ''
    result = dict(physical_index=physical_index, printed_label=None,
                  printed_label_verified=False, raw_text=native, blocks=[], warnings=[],
                  provenance=dict(asset_sha256=sha256(data).hexdigest(), native_text=native,
                                  native_engine='pypdf', native_version=metadata.version('pypdf'),
                                  rotation=source.rotation, config=vars(config).copy()))
    provenance = result['provenance']
    with pdfium.PdfDocument(data) as doc:
        if len(doc) != len(reader.pages):
            raise ValueError('parser page count disagreement')
        with closing(doc[physical_index - 1]) as page:
            has_images = any(page.get_objects(filter=[pdfium.raw.FPDF_PAGEOBJ_IMAGE]))
            visible = [c for c in native if not c.isspace()]
            suspect = bool(visible) and (any(c == '\ufffd' or ord(c) < 32 for c in visible)
                                         or sum(c.isalnum() for c in visible) / len(visible) < .4)
            if native.strip() and not suspect and not has_images:
                # Native extraction can include text never painted on this page.
                # Reuse PDFium visibility and bounds APIs, retaining raw evidence.
                reason = _native_visibility_warning(page, pdfium, config.max_pixels)
                if reason:
                    provenance['method'] = 'native_visibility_review'
                    result['status'] = 'review_required'
                    result['warnings'].append(reason)
                    return result
                provenance['method'] = 'native'
                result['status'] = 'candidate'
                return result
            result['warnings'].append('ocr_required')
            if has_images and native.strip():
                result['warnings'].append('mixed_native_and_image')
            provenance['renderer'] = 'pypdfium2 ' + metadata.version('pypdfium2')
            width, height = page.get_size()
            scale = config.dpi / 72
            if not all(math.isfinite(v) and v > 0 for v in (width, height)) or math.ceil(width * scale) * math.ceil(height * scale) > config.max_pixels:
                result['status'] = 'review_required'
                result['warnings'].append('render_limit')
                return result
            with closing(page.render(scale=scale)) as bitmap:
                image = bitmap.to_pil().convert('RGB')
                provenance['render_size'] = image.size
                # Near-white marks can still contain source text. Only an
                # entirely white raster is eligible for automatic blank status.
                if all(lo == 255 for lo, hi in image.getextrema()) and not native.strip():
                    result['status'] = 'blank'
                    provenance['method'] = 'render_blank'
                    return result
                try:
                    def remaining():
                        seconds = deadline - time.monotonic()
                        if seconds <= 0:
                            raise subprocess.TimeoutExpired('tesseract', config.timeout)
                        return seconds

                    profile_options = ({'model_profile': config.model_profile}
                                       if config.model_profile != 'installed' else {})
                    runtime = discover_ocr_runtime(config.languages, orientation=config.orientation,
                                                   remaining=remaining, **profile_options)
                    if runtime is None:
                        result['status'] = 'review_required'
                        result['warnings'].append('ocr_engine_missing')
                        return result
                    exe = runtime.executable
                    if runtime.portable or getattr(runtime, 'managed', False):
                        provenance['ocr_runtime'] = runtime.fingerprint()

                    with tempfile.TemporaryDirectory() as tmp:
                        def run(args):
                            return subprocess.run(runtime.command(args), check=True, capture_output=True,
                                                  timeout=remaining(), cwd=runtime.root,
                                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

                        model_args = []
                        if config.model_policy == POLICY:
                            try:
                                report = run([exe, '--list-langs']).stdout.decode('utf-8')
                                model_args, model_provenance = stage_pinned_models(
                                    report, config.languages, Path(tmp) / 'verified-models', remaining)
                            except (ValueError, OSError, subprocess.SubprocessError):
                                result['status'] = 'review_required'
                                result['warnings'].append('pinned_ocr_model_unavailable_or_invalid')
                                return result
                            provenance['ocr_model'] = model_provenance

                        def recognize(raster, name):
                            inp, out = Path(tmp) / (name + '.png'), Path(tmp) / name
                            raster.save(inp, dpi=(config.dpi, config.dpi))
                            run([exe, str(inp), str(out), '-l', config.languages,
                                 '--dpi', str(config.dpi), '--psm', '3', *model_args,
                                 '-c', 'tessedit_create_txt=1', '-c', 'tessedit_create_tsv=1'])
                            return (_read_ocr_output(out.with_suffix('.txt')),
                                    _read_ocr_output(out.with_suffix('.tsv')))

                        provenance['ocr_engine'] = run([exe, '--version']).stdout.decode('utf-8', 'replace').splitlines()[0]
                        raw, tsv = recognize(image, 'baseline')
                        provenance['ocr_coordinate_frame'] = 'source_render_pixels_top_left'
                        if config.orientation:
                            from .ocr_orientation_bridge import orientation_candidate
                            provenance['orientation'] = orientation_candidate(
                                image, config, exe, remaining, run, recognize,
                                native_cli=runtime.portable or getattr(runtime, 'managed', False))
                        runtime.assert_unchanged(remaining)
                except _OcrOutputLimit:
                    result['status'] = 'review_required'
                    result['warnings'].append('ocr_output_limit')
                    return result
                except OcrRuntimeError:
                    result['status'] = 'review_required'
                    result['warnings'].append('portable_ocr_runtime_unavailable_or_invalid')
                    return result
                except (OSError, subprocess.SubprocessError, UnicodeError):
                    result['status'] = 'review_required'
                    result['warnings'].append('ocr_failed_or_timed_out')
                    return result
    provenance.update(method='ocr_candidate', ocr_text=raw, ocr_tsv=tsv, psm=3)
    try:
        result['blocks'] = _ocr_blocks(tsv, provenance['render_size'])
    except (csv.Error, ValueError, OverflowError):
        # Preserve both raw outputs for review, but never expose a partial or
        # malformed layout as usable word-coordinate evidence.
        result['warnings'].append('ocr_layout_invalid')
    # Mixed pages retain both candidates; no automatic concatenation or publication.
    if not native.strip() or suspect:
        result['raw_text'] = raw
    result['status'] = 'review_required'
    result['warnings'].append('ocr_not_verified' if raw.strip() else 'ocr_empty_nonblank')
    return result


def _native_visibility_warning(page, pdfium, max_pixels):
    """Conservative contradiction checks, not a full rendered-glyph verifier."""
    raw = pdfium.raw
    try:
        left, bottom, right, top = page.get_bbox()
        objects = []
        text_bounds = []
        # PDFium enumerates top-level page objects in painting order. Treat
        # overlapping later non-text objects as review signals, not proof of
        # occlusion: an outline or transparent fill can also overlap text.
        for index, obj in enumerate(page.get_objects(max_depth=1)):
            if index >= 256:
                return 'native_visibility_work_limit'
            if obj.type == raw.FPDF_PAGEOBJ_FORM:
                return 'native_form_coordinates_unverified'
            if obj.type != raw.FPDF_PAGEOBJ_TEXT:
                if text_bounds:
                    a, b, c, d = obj.get_bounds()
                    if not all(math.isfinite(v) for v in (a, b, c, d)):
                        return 'native_bounds_unverified'
                    if any(a < x1 and c > x0 and b < y1 and d > y0
                           for x0, y0, x1, y1 in text_bounds):
                        return 'native_later_object_overlap'
                continue
            if len(objects) >= 128:
                return 'native_visibility_work_limit'
            objects.append(obj)
            mode = raw.FPDFTextObj_GetTextRenderMode(obj.raw)
            if mode not in (raw.FPDF_TEXTRENDERMODE_FILL, raw.FPDF_TEXTRENDERMODE_STROKE,
                            raw.FPDF_TEXTRENDERMODE_FILL_STROKE,
                            raw.FPDF_TEXTRENDERMODE_FILL_CLIP, raw.FPDF_TEXTRENDERMODE_STROKE_CLIP,
                            raw.FPDF_TEXTRENDERMODE_FILL_STROKE_CLIP):
                return 'native_text_nonpainting_or_unknown'
            # An internal clipping path can hide only part of a text object
            # while leaving its bounds inside the page and its prefix visible.
            clip = raw.FPDFPageObj_GetClipPath(obj.raw)
            if clip and raw.FPDFClipPath_CountPaths(clip) != -1:
                return 'native_clip_path_unverified'
            # Form object transforms need separate coordinate verification.
            if obj.level:
                return 'native_form_coordinates_unverified'
            x0, y0, x1, y1 = obj.get_bounds()
            if not all(math.isfinite(v) for v in (x0, y0, x1, y1, left, bottom, right, top)):
                return 'native_bounds_unverified'
            if x1 <= left or x0 >= right or y1 <= bottom or y0 >= top:
                return 'native_text_outside_page'
            if x0 < left or y0 < bottom or x1 > right or y1 > top:
                return 'native_text_partially_outside_page'
            text_bounds.append((x0, y0, x1, y1))
        if not objects:
            return 'native_text_objects_missing'
        width, height = page.get_size()
        if not all(math.isfinite(v) and v > 0 for v in (width, height)) or math.ceil(width) * math.ceil(height) > max_pixels:
            return 'native_visibility_render_limit'
        # Bound aggregate raster work, not just the size of an individual render.
        if math.ceil(width) * math.ceil(height) * (1 + len(objects)) > 64_000_000:
            return 'native_visibility_work_limit'
        with closing(page.render(scale=1)) as bitmap:
            image = bitmap.to_pil().convert('RGB')
            if all(lo == 255 for lo, hi in image.getextrema()):
                return 'native_text_on_blank_render'
            baseline = sha256(image.tobytes()).digest()
        # Use PDFium's existing object activation API in this disposable document.
        # A fully occluded or white-on-white object may pass mode/bounds checks.
        # No pixel change is a contradiction signal, not proof of glyph accuracy.
        for obj in objects:
            if not raw.FPDFPageObj_SetIsActive(obj.raw, False):
                return 'native_visibility_check_failed'
            try:
                with closing(page.render(scale=1)) as bitmap:
                    without = sha256(bitmap.to_pil().convert('RGB').tobytes()).digest()
            finally:
                if not raw.FPDFPageObj_SetIsActive(obj.raw, True):
                    raise RuntimeError('PDFium object restoration failed')
            if without == baseline:
                return 'native_text_object_not_painted'
    except (pdfium.PdfiumError, ValueError, OSError, RuntimeError, AttributeError):
        return 'native_visibility_check_failed'
    return None
