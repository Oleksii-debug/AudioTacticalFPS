"""Independent approved-page rendering for an exact large retained PDF."""
import re

from .anchors import Identity, Locator
from .pdf_review_attestation import ReviewedPage


def file_pdf_attestor(db, workspace, resolve_original, lookup_review, *,
                      cancelled=lambda: False):
    """A saved review is necessary; parser/OCR agreement never creates approval."""
    from ..ingest.source_profiles import PdfProfile, profile_from_config
    from ..ingest.worker import isolated_render_file_fingerprint

    def attest(identity, locator, raw_sha256):
        if cancelled():
            raise InterruptedError('PDF source verification cancelled.')
        if (not db.in_transaction or db.execute('PRAGMA read_uncommitted').fetchone()[0]
                or not isinstance(identity, Identity) or identity.workspace != workspace
                or not isinstance(locator, Locator) or locator.kind != 'physical_page'
                or type(raw_sha256) is not str or re.fullmatch('[0-9a-f]{64}', raw_sha256) is None):
            return False
        row = db.execute('SELECT r.config FROM revision r JOIN sealed s ON s.revision=r.id '
            'WHERE r.workspace=? AND r.edition=? AND r.asset=? AND r.id=?',
            (workspace, identity.edition_id, identity.asset_sha256, identity.revision)).fetchone()
        if row is None:
            return False
        try:
            profile = profile_from_config(row[0])
            if not isinstance(profile, PdfProfile):
                return False
            review = lookup_review(identity, locator.index)
            if (not isinstance(review, ReviewedPage) or review.identity != identity
                    or review.physical != locator.index or type(review.physical) is not int
                    or review.raw_sha256 != raw_sha256 or type(review.reviewer) is not str
                    or not review.reviewer.strip()):
                return False
            renderer, fingerprint = isolated_render_file_fingerprint(resolve_original(identity),
                locator.index, profile, expected_digest=identity.asset_sha256, cancelled=cancelled)
            return (review.renderer, review.rendered_sha256) == (renderer, fingerprint)
        except InterruptedError:
            raise
        except (OSError, LookupError, ValueError, RuntimeError, ImportError):
            return False

    return attest
