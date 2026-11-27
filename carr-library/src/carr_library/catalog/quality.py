"""Read-only catalog checks in one existing, authorized library snapshot.

Metadata similarities are review hints, never identities or instructions to merge.
The original asset digest is catalog provenance, not a new source-file attestation.
"""
from collections import defaultdict
from dataclasses import dataclass
import sqlite3
import unicodedata

from .membership import Catalog
from . import insights
from .read_views import EditionView, browse_editions


ISSUE_LABELS = (
    ('missing_title', 'Missing title'),
    ('missing_authors', 'Missing authors'),
    ('missing_language', 'Missing language'),
    ('unfiled', 'No active collection'),
    ('same_asset', 'Same registered source asset'),
    ('possible_duplicate', 'Possible duplicate metadata'),
)
MAX_EDITIONS = 100_000


@dataclass(frozen=True)
class QualityIssue:
    category: str
    edition_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class QualitySummary:
    sources: int
    works: int
    authors: int
    languages: int
    collections: int
    unique_assets: int
    affected_sources: int


@dataclass(frozen=True)
class CatalogQualitySnapshot:
    workspace: str
    collection: str | None
    collections: tuple[tuple[str, str], ...]
    editions: tuple[EditionView, ...]
    issues: tuple[QualityIssue, ...]
    summary: QualitySummary


def normalized_metadata(value: str | None) -> str:
    """NFC Unicode, case folding and whitespace only; retain punctuation/accents."""
    return unicodedata.normalize('NFC', ' '.join((value or '').split()).casefold())


def catalog_quality(library, workspace, *, collection=None, cancelled=lambda: False):
    """Check scoped editions without changing catalog, sources or stored evidence.

Sources means edition records; works/assets and normalized nonempty author names
and languages are distinct counts within that scope. Collections counts all active
collections for a library-wide check, or the single selected collection. Authors
are names in the catalog, not independently disambiguated people.

All edition pages and memberships use one LibraryApp.read transaction. Missing
metadata never becomes a duplicate key. A possible duplicate requires title,
ordered authors and language; edition/volume distinctions are always retained.
Cancellation raises instead of returning a misleading partial report.
"""
    if not callable(cancelled):
        raise ValueError('A cancellation callback is required.')

    def check():
        if cancelled():
            raise InterruptedError('Catalog check cancelled; no report was published.')

    check()
    with library.read(workspace) as db:
        db.set_progress_handler(lambda: int(bool(cancelled())), 1000)
        try:
            check()
            catalog = Catalog(db, workspace)
            collections = insights.report_collections(catalog)
            if collection is not None:
                catalog._active_collection(collection)
            insights._preflight_editions(catalog, collection,
                insights.MAX_METADATA_BYTES - sum(len(name.encode('utf-8'))
                                                  for _, name in collections))
            editions = []
            after = None
            while True:
                check()
                page = browse_editions(catalog, collection=collection, after=after, limit=500)
                check()
                editions.extend(page)
                if len(editions) > MAX_EDITIONS:
                    raise ValueError('This catalog is too large for one check. Choose a collection.')
                if len(page) < 500:
                    break
                after = page[-1].edition_id

            selected_ids = {row.edition_id for row in editions}
            filed = set()
            # Archived collections no longer organize the active library. This
            # read uses the same workspace-bound snapshot as the public views.
            for row in db.execute(
                    'SELECT DISTINCT m.edition FROM catalog_membership m '
                    'JOIN catalog_collection c ON c.workspace=m.workspace AND c.id=m.collection '
                    'WHERE m.workspace=? AND c.archived=0', (workspace,)):
                check()
                if row[0] in selected_ids:
                    filed.add(row[0])

            issues = []
            assets, metadata = defaultdict(list), defaultdict(list)
            authors, languages = set(), set()
            for row in editions:
                check()
                title = normalized_metadata(row.title)
                names = tuple(normalized_metadata(name) for name in row.authors)
                language = normalized_metadata(row.language)
                authors.update(name for name in names if name)
                if language:
                    languages.add(language)
                missing = (
                    ('missing_title', not title, 'No title is recorded for this work.'),
                    ('missing_authors', not names or not all(names),
                     'Author metadata is missing or incomplete. Unknown authors should remain unknown.'),
                    ('missing_language', not language, 'No language is recorded for this edition.'),
                    ('unfiled', row.edition_id not in filed,
                     'This edition is not assigned to any active collection.'),
                )
                issues.extend(QualityIssue(kind, (row.edition_id,), reason)
                              for kind, present, reason in missing if present)
                assets[row.asset].append(row.edition_id)
                if title and names and all(names) and language:
                    metadata[(title, names, language)].append(row.edition_id)

            for digest, ids in assets.items():
                check()
                if len(ids) > 1:
                    issues.append(QualityIssue('same_asset', tuple(ids),
                        f'These edition records refer to the same registered source asset (SHA-256 {digest}). '
                        'The retained files were not re-verified by this catalog check. '
                        'Keep the editions separate unless you have reviewed their provenance.'))
            for ids in metadata.values():
                check()
                if len(ids) > 1:
                    issues.append(QualityIssue('possible_duplicate', tuple(ids),
                        'Possible duplicate metadata: title, authors in the same order, and language '
                        'match after Unicode, letter-case and whitespace normalization. '
                        'This is a tentative review hint. Different editions, volumes and source files '
                        'remain separate; matching metadata does not establish that they are the same work.'))
            check()
            summary = QualitySummary(len(editions), len({r.work_id for r in editions}),
                len(authors), len(languages), len(collections) if collection is None else 1,
                len(assets), len({identity for issue in issues for identity in issue.edition_ids}))
            return CatalogQualitySnapshot(workspace, collection, collections,
                                          tuple(editions), tuple(issues), summary)
        except sqlite3.OperationalError as exc:
            if cancelled() or getattr(exc, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT:
                raise InterruptedError('Catalog check cancelled; no report was published.') from exc
            raise
        finally:
            db.set_progress_handler(None, 0)
