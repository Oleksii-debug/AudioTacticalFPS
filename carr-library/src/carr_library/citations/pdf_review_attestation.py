"""Bind independently reviewed PDF text to original bytes and rendered page.

No OCR/parser agreement or locally minted record is an approval. The caller
must supply authenticated review records and an authorized immutable asset
resolver; rendering must run in the existing isolated PDF worker.
"""
from contextlib import closing
from dataclasses import dataclass
from hashlib import sha256
import math
import re
import struct

from .anchors import Identity, Locator

MAX_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 20_000_000
SCALE = 2


@dataclass(frozen=True)
class ReviewedPage:
    identity: Identity
    physical: int
    raw_sha256: str
    rendered_sha256: str
    renderer: str
    reviewer: str


def render_fingerprint(data, physical):
    """Reuse PDFium; include dimensions and renderer version in review binding."""
    import pypdfium2 as pdfium
    if type(data) is not bytes or not 0 < len(data) <= MAX_BYTES:
        raise ValueError('invalid PDF bytes')
    with pdfium.PdfDocument(data) as doc:
        return render_document_fingerprint(doc, physical)


def render_document_fingerprint(doc, physical):
    """Same reviewed raster identity, with a caller-held current PDF document."""
    import pypdfium2 as pdfium
    if type(physical) is not int or not 1 <= physical <= len(doc) <= 1000:
        raise ValueError('invalid PDF page')
    with closing(doc[physical - 1]) as page:
        w, h = page.get_size()
        if not all(math.isfinite(v) and v > 0 for v in (w, h)):
            raise ValueError('invalid PDF dimensions')
        if math.ceil(w * SCALE) * math.ceil(h * SCALE) > MAX_PIXELS:
            raise ValueError('PDF render limit')
        with closing(page.render(scale=SCALE)) as bitmap:
            image = bitmap.to_pil().convert('RGB')
            if image.width * image.height > MAX_PIXELS:
                raise ValueError('PDF render limit')
            fingerprint = sha256(struct.pack('>II', *image.size) + image.tobytes()).hexdigest()
    return str(pdfium.PYPDFIUM_INFO) + '/' + str(pdfium.PDFIUM_INFO) + '/rgb-scale2-v1', fingerprint


def pdf_review_attestor(workspace, resolve_asset, lookup_review, *, renderer=render_fingerprint):
    """Callback for historical_loader. No printed-label approval is implied.

    lookup_review(identity, physical) returns a trusted ReviewedPage or None.
    Its authority and revocation checks are outside this adapter. Never source
    it from search hits, OCR confidence, or unauthenticated database fields.
    """
    if (not isinstance(workspace, str) or not workspace.strip() or not callable(resolve_asset)
            or not callable(lookup_review) or not callable(renderer)):
        raise ValueError('authorized workspace and resolvers required')

    def attest(identity, locator, raw_sha256):
        if (not isinstance(identity, Identity) or identity.workspace != workspace
                or not isinstance(locator, Locator) or locator.kind != 'physical_page'
                or not isinstance(raw_sha256, str) or not re.fullmatch('[0-9a-f]{64}', raw_sha256)):
            return False
        try:
            review = lookup_review(identity, locator.index)
            if (not isinstance(review, ReviewedPage) or review.identity != identity
                    or type(review.physical) is not int or review.physical != locator.index
                    or review.raw_sha256 != raw_sha256 or not isinstance(review.reviewer, str)
                    or not review.reviewer.strip()):
                return False
            data = resolve_asset(identity)
            if (type(data) is not bytes or not 0 < len(data) <= MAX_BYTES
                    or sha256(data).hexdigest() != identity.asset_sha256):
                return False
            renderer_identity, fingerprint = renderer(data, locator.index)
            return review.renderer == renderer_identity and review.rendered_sha256 == fingerprint
        except (OSError, LookupError, ValueError, RuntimeError, ImportError):
            return False

    return attest
