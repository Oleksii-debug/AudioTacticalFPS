"""Explicit source policy for searches that return verified quotations.

The application currently has independent original-byte attestation for its
strict UTF-8 adapter. Native PDF/OCR candidates remain review-required and
must not enter quotation ranking until a separate approved attestor is wired.
Eligibility is not attestation: every returned hit is still freshly verified.
Readable PDF candidates are classified separately for explicitly unverified
previews; they never enter the verified-source map or its result budget.
"""
from dataclasses import dataclass
import json

from ..catalog.membership import Catalog
from ..citations.text_attestation import CONFIG, MAX_SEGMENTS


@dataclass(frozen=True)
class SearchEligibility:
    # edition_id, work_id, asset_sha256, active_revision_id
    editions: tuple[tuple[str, str, str, str], ...]
    review_required: int
    not_indexed: int
    preview_editions: tuple[tuple[str, str, str, str], ...] = ()

    @property
    def edition_to_work(self):
        return {edition: work for edition, work, _, _ in self.editions}

    @property
    def preview_edition_to_work(self):
        return {edition: work for edition, work, _, _ in self.preview_editions}

    def status(self):
        return dict(policy='strict_utf8_source_verification',
                    eligible_sources=len(self.editions),
                    review_required_sources=self.review_required,
                    not_indexed_sources=self.not_indexed,
                    preview_sources=len(self.preview_editions))


def eligible_sources(db, workspace, *, collection=None):
    """Resolve authorized active sources in the caller's existing snapshot.

    Classify only known source formats/configurations. Identity, seal and strict
    TXT locator defects raise; callers must never catch verification errors and
    silently remove corrupt sources from search results.
    """
    if (not db.in_transaction or db.execute('PRAGMA read_uncommitted').fetchone()[0]
            or db.execute('PRAGMA foreign_keys').fetchone()[0] != 1):
        raise ValueError('consistent authorized source snapshot required')
    catalog = Catalog(db, workspace).editions(collection)
    if len(catalog) > 500:
        raise ValueError('catalog scope exceeds edition budget')
    eligible, previews = [], []
    review_required = not_indexed = 0
    for edition, work, asset, *_ in catalog:
        manifest = db.execute('SELECT mime FROM catalog_asset WHERE digest=?', (asset,)).fetchone()
        if manifest is None:
            raise ValueError('missing catalog asset manifest')
        row = db.execute('''SELECT a.revision,r.workspace,r.edition,r.asset,r.config,s.revision
            FROM active a LEFT JOIN revision r ON r.id=a.revision
            LEFT JOIN sealed s ON s.revision=r.id WHERE a.workspace=? AND a.edition=?''',
            (workspace, edition)).fetchone()
        if row is None:
            if db.execute('SELECT 1 FROM revision WHERE workspace=? AND edition=? LIMIT 1',
                          (workspace, edition)).fetchone():
                raise ValueError('published source has no active revision')
            not_indexed += 1
            continue
        revision, actual_workspace, actual_edition, actual_asset, config, seal = row
        if (seal != revision or (actual_workspace, actual_edition, actual_asset) !=
                (workspace, edition, asset)):
            raise ValueError('active revision differs from catalog identity or seal')
        if manifest[0] == 'text/plain' and config == CONFIG:
            count, invalid = db.execute('''SELECT count(*),coalesce(sum(
                physical IS NOT NULL OR printed IS NOT NULL OR verified<>0),0)
                FROM page WHERE revision=?''', (revision,)).fetchone()
            if not 1 <= count <= MAX_SEGMENTS or invalid:
                raise ValueError('invalid strict UTF-8 source locators')
            eligible.append((edition, work, asset, revision))
        else:
            review_required += 1
            if manifest[0] == 'application/pdf':
                try:
                    settings = json.loads(config)
                except (TypeError, ValueError, RecursionError) as exc:
                    raise ValueError('invalid PDF extraction configuration') from exc
                if (isinstance(settings, dict) and settings.get('adapter') == 'page_ocr-v1'
                        and settings.get('kind') == 'physical_page'):
                    count, distinct, first, last, invalid = db.execute('''
                        SELECT count(*),count(DISTINCT physical),min(physical),max(physical),coalesce(sum(
                            typeof(physical)<>'integer' OR physical<1 OR physical>1000),0)
                        FROM page WHERE revision=?''', (revision,)).fetchone()
                    if (not 1 <= count <= 1000 or distinct != count or invalid
                            or first != 1 or last != count):
                        raise ValueError('invalid PDF preview locators')
                    previews.append((edition, work, asset, revision))
    return SearchEligibility(tuple(eligible), review_required, not_indexed, tuple(previews))
