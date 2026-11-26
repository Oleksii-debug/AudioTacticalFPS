"""Page-preserving adapter over pypdf, PDFium and Tesseract; trusted inputs only."""
from dataclasses import dataclass
from contextlib import closing
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import importlib.metadata as metadata
import math
import re
import shutil
import subprocess
import tempfile


@dataclass(frozen=True)
class Config:
    languages: str = 'eng'
    dpi: int = 300
    timeout: int = 30
    max_pixels: int = 20_000_000

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9_]+(?:\+[A-Za-z0-9_]+)*', self.languages):
            raise ValueError('invalid language selection')
        if not 72 <= self.dpi <= 400 or not 1 <= self.timeout <= 120 or not 1 <= self.max_pixels <= 20_000_000:
            raise ValueError('invalid resource limit')


def extract_page(asset, physical_index, config=Config()):
    """Return a candidate, never a verified printed label or corrected quote.

    OCR TSV coordinates are pixels in the rendered, rotated page image.
    Parser isolation and durable job checkpoints belong to the ingest runner.
    """
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
                if all(lo >= 250 for lo, hi in image.getextrema()) and not native.strip():
                    result['status'] = 'blank'
                    provenance['method'] = 'render_blank'
                    return result
                exe = shutil.which('tesseract')
                if not exe:
                    result['status'] = 'review_required'
                    result['warnings'].append('ocr_engine_missing')
                    return result
                try:
                    with tempfile.TemporaryDirectory() as tmp:
                        inp, out = Path(tmp) / 'page.png', Path(tmp) / 'ocr'
                        image.save(inp, dpi=(config.dpi, config.dpi))
                        opts = dict(check=True, capture_output=True, timeout=config.timeout)
                        provenance['ocr_engine'] = subprocess.run([exe, '--version'], **opts).stdout.decode('utf-8', 'replace').splitlines()[0]
                        subprocess.run([exe, str(inp), str(out), '-l', config.languages,
                                        '--dpi', str(config.dpi), '--psm', '3', 'txt', 'tsv'], **opts)
                        raw = out.with_suffix('.txt').read_bytes().decode('utf-8')
                        tsv = out.with_suffix('.tsv').read_bytes().decode('utf-8')
                except (OSError, subprocess.SubprocessError, UnicodeError):
                    result['status'] = 'review_required'
                    result['warnings'].append('ocr_failed_or_timed_out')
                    return result
    provenance.update(method='ocr_candidate', ocr_text=raw, ocr_tsv=tsv, psm=3)
    import csv
    for row in csv.DictReader(tsv.splitlines(), delimiter='\t'):
        if row['level'] == '5' and row['text']:
            result['blocks'].append(dict(text=row['text'], confidence=float(row['conf']),
                                        bbox=tuple(int(row[k]) for k in ('left', 'top', 'width', 'height')),
                                        line=tuple(int(row[k]) for k in ('block_num', 'par_num', 'line_num'))))
    # Mixed pages retain both candidates; no automatic concatenation or publication.
    if not native.strip() or suspect:
        result['raw_text'] = raw
    result['status'] = 'review_required'
    result['warnings'].append('ocr_not_verified' if raw.strip() else 'ocr_empty_nonblank')
    return result
