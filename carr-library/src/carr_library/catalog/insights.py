"""Read-only source catalog insights in one authorized SQLite snapshot.

This is an inventory of stored records, not a quotation/authenticity verifier.
It deliberately never reads retained files, launches parsers or returns source
text, evidence payloads or private research notes. Existing Catalog and topic
visibility contracts remain authoritative; no additional persistence is used.
"""
from collections import Counter, defaultdict
from dataclasses import dataclass

from .membership import Catalog
from .read_views import active_collections, browse_editions
from ..citations.anchors import validate_span
from ..research.span_snapshot import MAX_PAYLOAD, _decode_snapshot
from ..research.topic_tree import TopicTree, _VISIBLE


MAX_SOURCES = 10_000
MAX_TOPICS = 10_000
MAX_EVIDENCE = 50_000
MAX_LINKS = 100_000
MAX_METADATA_BYTES = 16 * 1024 * 1024
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024

NOTICE = ('This report describes catalog metadata and stored research links. '
          'Recorded PDF reviews are not a fresh check of source authenticity, '
          'quotation text or printed page numbers. Source text and research '
          'notes are not included.')

ISSUE_LABELS = {
    'missing_title': 'Add a source title in Books and Collections.',
    'missing_authors': 'Add author names if known in Books and Collections.',
    'missing_language': 'Add a language code if known in Books and Collections.',
    'no_active_text': 'No active text is available. Check or resume its import.',
    'not_fully_indexed': 'Some active text units are absent from the search index.',
    'missing_pdf_review': 'Some PDF pages have no matching active review record.',
    'unfiled': 'Assign this source to a collection if useful for your research.',
}


@dataclass(frozen=True)
class SourceInsight:
    edition_id: str
    title: str | None
    authors: tuple[str, ...]
    language: str | None
    edition: str | None
    volume: str | None
    collections: tuple[str, ...]
    text_units: int
    indexed_units: int
    physical_pages: int
    logical_segments: int
    recorded_review_pages: int
    evidence_count: int
    topics: tuple[str, ...]
    tags: tuple[str, ...]
    issues: tuple[str, ...]
    asset_sha256: str
    revision: str | None


@dataclass(frozen=True)
class CatalogAnalysis:
    workspace: str
    workspace_name: str
    collection_id: str | None
    collection_name: str | None
    sources: tuple[SourceInsight, ...]
    warnings: tuple[str, ...] = ()


class _TextBudget:
    def __init__(self):
        self.used = 0

    def add(self, *values):
        for value in values:
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError('Invalid catalog text.')
                self.used += len(value.encode('utf-8'))
        if self.used > MAX_METADATA_BYTES:
            raise ValueError('Catalog metadata exceeds the report limit. Choose a smaller collection.')


def _rows(cursor, limit, check, name):
    """Never return a misleading, silently truncated report."""
    try:
        for number, row in enumerate(cursor, 1):
            check()
            if number > limit:
                raise ValueError(f'Too many {name} for this report.')
            yield row
    finally:
        cursor.close()


def _topics(db, workspace, check, text_budget):
    rows = {}
    query = _VISIBLE + '''SELECT t.id,t.parent,t.name FROM research_topic t
        JOIN visible v ON v.id=t.id WHERE t.workspace=? ORDER BY t.id'''
    for identity, parent, name in _rows(db.execute(query, (workspace,) * 3),
                                      MAX_TOPICS, check, 'research topics'):
        text_budget.add(identity, parent, name)
        rows[identity] = parent, name
    labels = {}
    for identity, (parent, name) in rows.items():
        check()
        path, seen, current = [], set(), identity
        while current is not None:
            check()
            if current in seen or current not in rows or len(path) >= 256:
                raise ValueError('Invalid or excessively deep visible topic hierarchy.')
            seen.add(current)
            current, name = rows[current]
            path.append(display_value(name))
        # Identity disambiguates equal topic names and equal full paths.
        label = ' / '.join(reversed(path)) + f' [topic {display_value(identity)}]'
        text_budget.add(label)
        labels[identity] = label
    return labels


def _evidence_links(db, workspace, selected, topic_labels, check, text_budget):
    counts = Counter()
    source_topics, source_tags = defaultdict(set), defaultdict(set)
    evidence_sources, checked_identities = {}, set()
    invalid, payload_bytes = 0, 0
    query = _VISIBLE + '''SELECT e.topic,e.id,e.checksum,
        length(CAST(e.payload AS BLOB)),
        CASE WHEN length(CAST(e.payload AS BLOB))<=? THEN e.payload ELSE NULL END
        FROM research_evidence e JOIN visible v ON v.id=e.topic
        WHERE e.workspace=? ORDER BY e.topic,e.id'''
    values = (workspace, workspace, MAX_PAYLOAD, workspace)
    for topic, evidence, checksum, size, payload in _rows(
            db.execute(query, values), MAX_EVIDENCE, check, 'saved evidence records'):
        payload_bytes += size
        if payload_bytes > MAX_PAYLOAD_BYTES:
            raise ValueError('Saved evidence exceeds the catalog report reading limit.')
        try:
            if payload is None:
                raise ValueError('Oversized evidence snapshot.')
            anchors = validate_span(_decode_snapshot(payload, checksum))
            identity = anchors[0].identity
            if identity.workspace != workspace:
                raise ValueError('Evidence belongs to a different library.')
            # A checksum proves neither source bytes nor attribution. We only
            # use the canonical identity to count a stored link to a sealed
            # revision that still belongs to this exact catalog edition.
            key = identity.edition_id, identity.revision, identity.asset_sha256
            if key not in checked_identities:
                exists = db.execute('''SELECT 1 FROM revision r
                    JOIN sealed s ON s.revision=r.id
                    JOIN catalog_edition e ON e.workspace=r.workspace
                      AND e.id=r.edition AND e.asset=r.asset
                    WHERE r.workspace=? AND r.edition=? AND r.id=? AND r.asset=?''',
                    (workspace, *key)).fetchone()
                if exists is None:
                    raise ValueError('Unknown stored source identity.')
                checked_identities.add(key)
            if identity.edition_id not in selected:
                continue
            if topic not in topic_labels:
                raise ValueError('Unavailable research topic.')
            counts[identity.edition_id] += 1
            source_topics[identity.edition_id].add(topic_labels[topic])
            evidence_sources[(topic, evidence)] = identity.edition_id
        except (ValueError, TypeError, UnicodeError):
            invalid += 1
        check()
    tag_query = _VISIBLE + '''SELECT et.topic,et.evidence,t.name
        FROM research_evidence_tag et
        JOIN visible v ON v.id=et.topic
        JOIN research_tag t ON t.workspace=et.workspace AND t.id=et.tag
        WHERE et.workspace=? ORDER BY et.topic,et.evidence,t.name,t.id'''
    for topic, evidence, tag in _rows(db.execute(tag_query, (workspace,) * 3),
                                    MAX_LINKS, check, 'research tag links'):
        edition_id = evidence_sources.get((topic, evidence))
        if edition_id is not None:
            text_budget.add(tag)
            source_tags[edition_id].add(tag)
    return counts, source_topics, source_tags, invalid


def analyze_catalog(library, workspace, *, collection=None, authorized_topic=None,
                    cancelled=lambda: False, timeout=30):
    """Inventory one active library/collection without modifying any data.

    All queries use the existing bounded read transaction, SQLite progress
    cancellation and visibility rules. Text units are explicitly split into
    physical PDF pages and unpaginated logical segments. Evidence counts cover
    visible topics, including their historical source revisions, once per saved
    record even when that record spans multiple pages. A damaged stored evidence
    link is reported as a warning rather than guessed from untrusted payloads.
    """
    with library._search_read(workspace, timeout, cancelled) as (db, budget):
        check = budget.check
        check()
        if authorized_topic is not None:
            TopicTree(db, workspace)._visible(authorized_topic)
        catalog = Catalog(db, workspace)
        collections = dict(active_collections(catalog))
        if collection is not None and collection not in collections:
            raise ValueError('The selected collection is missing or archived.')
        workspace_name = db.execute('SELECT name FROM workspace_meta WHERE workspace=?',
                                    (workspace,)).fetchone()[0]
        text_budget = _TextBudget()
        text_budget.add(workspace_name, *collections.values())
        editions, after = [], None
        while True:
            check()
            page = browse_editions(catalog, collection=collection, after=after, limit=500)
            if not page:
                break
            for row in page:
                text_budget.add(row.edition_id, row.title, row.language, row.edition,
                                row.volume, *row.authors)
            editions.extend(page)
            if len(editions) > MAX_SOURCES:
                raise ValueError('Too many sources for this report. Choose a smaller collection.')
            after = page[-1].edition_id
        selected = {row.edition_id for row in editions}
        if not selected:
            return CatalogAnalysis(workspace, workspace_name, collection,
                                   collections.get(collection), ())
        memberships = defaultdict(list)
        for edition_id, collection_id in _rows(db.execute('''
                SELECT m.edition,m.collection FROM catalog_membership m
                JOIN catalog_collection c ON c.workspace=m.workspace AND c.id=m.collection
                WHERE m.workspace=? AND c.archived=0 ORDER BY m.edition,c.name,c.id''',
                (workspace,)), MAX_LINKS, check, 'collection memberships'):
            if edition_id in selected:
                memberships[edition_id].append(collections[collection_id])
        # The joins are by exact workspace/edition/revision/physical-page and
        # raw digest, so reviews from older metadata revisions are not copied.
        page_query = '''SELECT a.edition,a.revision,COUNT(p.id),
                SUM(CASE WHEN p.physical IS NOT NULL THEN 1 ELSE 0 END),
                SUM(CASE WHEN p.id IS NOT NULL AND p.physical IS NULL THEN 1 ELSE 0 END),
                COUNT(i.id),COUNT(rv.physical)
            FROM active a
            LEFT JOIN page p ON p.revision=a.revision
            LEFT JOIN indexed i ON i.id=p.id
            LEFT JOIN source_page_review rv ON rv.workspace=a.workspace AND rv.edition=a.edition
                AND rv.revision=a.revision AND rv.physical=p.physical AND rv.active=1
                AND rv.raw_sha256=p.digest
            WHERE a.workspace=? AND (? IS NULL OR EXISTS (
                SELECT 1 FROM catalog_membership m WHERE m.workspace=a.workspace
                AND m.edition=a.edition AND m.collection=?))
            GROUP BY a.edition,a.revision ORDER BY a.edition'''
        page_counts = {row[0]: tuple(row[1:]) for row in _rows(
            db.execute(page_query, (workspace, collection, collection)), MAX_SOURCES,
            check, 'active sources') if row[0] in selected}
        topic_labels = _topics(db, workspace, check, text_budget)
        evidence, topics, tags, invalid = _evidence_links(
            db, workspace, selected, topic_labels, check, text_budget)
        result = []
        for row in editions:
            check()
            revision, units, physical, logical, indexed, reviewed = page_counts.get(
                row.edition_id, (None, 0, 0, 0, 0, 0))
            assigned = tuple(memberships[row.edition_id])
            issues = []
            if not row.title or not row.title.strip():
                issues.append('missing_title')
            if not row.authors or not any(name.strip() for name in row.authors):
                issues.append('missing_authors')
            if not row.language or not row.language.strip():
                issues.append('missing_language')
            if not units:
                issues.append('no_active_text')
            elif indexed != units:
                issues.append('not_fully_indexed')
            if physical > reviewed:
                issues.append('missing_pdf_review')
            if not assigned:
                issues.append('unfiled')
            source_topics = tuple(sorted(topics[row.edition_id]))
            source_tags = tuple(sorted(tags[row.edition_id]))
            text_budget.add(*assigned, *source_topics, *source_tags)
            result.append(SourceInsight(row.edition_id, row.title, row.authors, row.language,
                row.edition, row.volume, assigned, units, indexed, physical, logical, reviewed,
                evidence[row.edition_id], source_topics, source_tags, tuple(issues), row.asset, revision))
        check()
        warnings = ((f'{invalid} stored evidence record(s) could not be linked to a source. '
                     'Their counts, topics and tags are omitted; inspect Evidence and Notes.'),) if invalid else ()
        return CatalogAnalysis(workspace, workspace_name, collection,
                               collections.get(collection), tuple(result), warnings)


def display_value(value):
    """Readable one-line catalog text; never used as an original quotation."""
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('Catalog display text must be a string.')
    result = []
    for char in value:
        if char in '\r\n\t':
            result.append(' ')
        elif ord(char) < 32 or 127 <= ord(char) <= 159 or char in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069':
            result.append(f'\\u{ord(char):04x}')
        else:
            result.append(char)
    return ''.join(result)


def _heading(analysis, title):
    scope = 'Library: ' + display_value(analysis.workspace_name)
    if analysis.collection_id is not None:
        scope += '\nCollection: ' + display_value(analysis.collection_name)
    else:
        scope += '\nScope: all sources in this library.'
    return [title, scope, NOTICE, *analysis.warnings]


def format_overview(analysis):
    """Screen-reader-friendly inventory; all values describe the selected scope."""
    rows = analysis.sources
    parts = _heading(analysis, 'Library overview')
    total = lambda field: sum(getattr(row, field) for row in rows)
    parts.extend([
        f'Source editions: {len(rows)}.',
        f'Active text units: {total("text_units")}; indexed text units: {total("indexed_units")}.',
        f'Physical PDF pages: {total("physical_pages")}; unpaginated logical segments: {total("logical_segments")}.',
        f'Physical pages with a matching active review record: {total("recorded_review_pages")}.',
        f'Saved evidence links in visible topics: {total("evidence_count")}.',
        f'Sources used in visible research topics: {sum(row.evidence_count > 0 for row in rows)}.',
        f'Sources outside active collections: {sum(not row.collections for row in rows)}.',
        f'Sources with checklist items: {sum(bool(row.issues) for row in rows)}.',
    ])
    languages = Counter(row.language if row.language and row.language.strip() else
                        'Unknown language' for row in rows)
    if languages:
        parts.append('Languages:\n' + '\n'.join(f'{display_value(name)}: {count} source(s).'
                     for name, count in sorted(languages.items())))
    collections = Counter(name for row in rows for name in row.collections)
    if collections:
        parts.append('Active collection membership (a source can belong to several):\n' + '\n'.join(
            f'{display_value(name)}: {count} source(s).' for name, count in sorted(collections.items())))
    if not rows:
        parts.append('No sources in this scope. Import a source or choose another collection.')
    return '\n\n'.join(parts)


def format_checklist(analysis):
    """Explain concrete catalog tasks without treating missing optional data as false facts."""
    parts = _heading(analysis, 'Source metadata and indexing checklist')
    parts.append('Missing authors or language mean no value is recorded; they do not prove an error. '
                 'Collection membership is optional. Do not invent metadata to clear this list.')
    for number, row in enumerate((row for row in analysis.sources if row.issues), 1):
        actions = '\n'.join('- ' + ISSUE_LABELS[code] for code in row.issues)
        parts.append(f'{number}. {display_value(row.title).strip() or "Untitled source"}\n'
                     f'Source ID: {display_value(row.edition_id)}\n'
                     f'Authors: {display_value("; ".join(row.authors)).strip() or "Not recorded"}\n{actions}')
    if not any(row.issues for row in analysis.sources):
        parts.append('No checklist items were found in this scope.' if analysis.sources else
                     'No sources in this scope. Import a source or choose another collection.')
    return '\n\n'.join(parts)
