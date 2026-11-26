"""Readable extracted PDF text whose agreement with the page is unverified.

The caller owns the authorized catalog transaction and supplies the sealed
page plus retained original bytes. A preview has no citation Anchor and cannot
be saved or copied through the verified-evidence APIs.
"""
from dataclasses import dataclass
from hashlib import sha256

from ..citations.anchors import Identity, Locator, Metadata, digest


UNVERIFIED_STATUS = 'Unverified extraction: compare with the original before citation'
MAX_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class PreviewPage:
    identity: Identity
    locator: Locator
    raw_sha256: str
    raw_text: str
    start: int
    end: int
    metadata: Metadata

    @property
    def quote(self):
        return self.raw_text[self.start:self.end]

    @property
    def citation_verified(self):
        return False

    @property
    def verification_status(self):
        return UNVERIFIED_STATUS

    @property
    def page_label(self):
        # PDF label metadata is a candidate. A preview never promotes it to an
        # independently verified printed label, even if an imported flag says so.
        return f'PDF page {self.locator.index} (printed page unverified)'

    @property
    def citation(self):
        m = self.metadata
        fields = [m.author or 'Unknown author', m.title or 'Unknown title']
        if m.volume:
            fields.append(f'vol. {m.volume}')
        if m.edition:
            fields.append(f'edition {m.edition}')
        return ', '.join([*fields, self.page_label]) + '\n' + UNVERIFIED_STATUS


@dataclass(frozen=True)
class SourcePreview:
    pages: tuple[PreviewPage, ...]

    def page(self, position):
        if type(position) is not int or not 0 <= position < len(self.pages):
            raise IndexError('preview page outside span')
        return self.pages[position]

    def clipboard(self, *args, **kwargs):
        raise ValueError('unverified extraction: verify against the original before citation')


def open_pdf_preview(workspace, identity, locator, raw, start, end, metadata,
                     *, stored_digest, original_bytes):
    """Validate a detached, explicitly unverified view of an authorized PDF page.

    Original and stored-text hashes prevent a damaged source from being silently
    downgraded into a preview. They do not prove that extracted characters agree
    with visible glyphs, so this function never creates a verified citation.
    """
    if (not isinstance(identity, Identity) or identity.workspace != workspace
            or not isinstance(locator, Locator) or locator.kind != 'physical_page'
            or not isinstance(metadata, Metadata)):
        raise ValueError('invalid or cross-workspace preview source')
    if (type(original_bytes) is not bytes or not 0 < len(original_bytes) <= MAX_BYTES
            or sha256(original_bytes).hexdigest() != identity.asset_sha256):
        raise ValueError('retained original changed')
    if (not isinstance(raw, str) or '\x00' in raw
            or digest(raw) != stored_digest):
        raise ValueError('stored text digest mismatch')
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(raw):
        raise ValueError('invalid source offsets')
    return SourcePreview((PreviewPage(identity, locator, stored_digest, raw,
                                      start, end, metadata),))
