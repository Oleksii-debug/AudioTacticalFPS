"""Read-only, workspace-scoped reports over the existing catalog and sources.

Call with a fresh authorized ``LibraryApp.read(workspace)`` snapshot. No source
file is opened and no catalog, review or citation is changed. Full SQL counts
are separate from explicitly bounded display samples. A SQLite progress handler
bounds database work as well as the Python formatting work; the caller should
not install a second progress handler on this connection.
"""
from contextlib import contextmanager
import json
import math
import sqlite3
from time import monotonic

from .membership import _identity


_WHITESPACE = ' \t\n\r\v\f\x1c\x1d\x1e\x1f\x85\xa0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000'
_TEXT_LIMIT = 300
_AUTHOR_LIMIT = 5
_MEMBER_LIMIT = 3

_SCOPE = '''WITH scoped AS (
    SELECT e.workspace,e.id,e.work,e.asset,e.language,e.edition,e.volume,
           w.title,a.mime,a.size
    FROM catalog_edition e
    JOIN catalog_work w ON w.workspace=e.workspace AND w.id=e.work
    JOIN catalog_asset a ON a.digest=e.asset
    WHERE e.workspace=:workspace AND (:collection IS NULL OR EXISTS (
        SELECT 1 FROM catalog_membership m WHERE m.workspace=e.workspace
        AND m.edition=e.id AND m.collection=:collection))
    ORDER BY e.id COLLATE BINARY LIMIT :scope_limit
)'''

_STATES = _SCOPE + ''', page_counts AS (
    SELECT s.id,a.revision,count(p.id) AS pages,
        coalesce(sum(length(trim(p.raw,:whitespace))>0),0) AS text_pages,
        coalesce(sum(i.id IS NOT NULL AND i.raw=p.raw COLLATE BINARY),0) AS indexed_pages,
        coalesce(sum(p.verified=1),0) AS printed_pages,
        coalesce(sum(v.active=1 AND v.raw_sha256=p.digest),0) AS reviewed_pages,
        coalesce(sum(v.active=1 AND v.raw_sha256<>p.digest),0) AS stale_reviews
    FROM scoped s
    JOIN active a ON a.workspace=s.workspace AND a.edition=s.id
    JOIN revision r ON r.workspace=s.workspace AND r.edition=s.id
        AND r.id=a.revision AND r.asset=s.asset
    JOIN sealed z ON z.revision=r.id
    LEFT JOIN page p ON p.revision=r.id
    LEFT JOIN indexed i ON i.id=p.id
    LEFT JOIN source_page_review v ON v.workspace=s.workspace AND v.edition=s.id
        AND v.revision=r.id AND v.physical=p.physical
    GROUP BY s.id,a.revision
), states AS (
    SELECT s.*,p.revision,coalesce(p.pages,0) AS pages,
        coalesce(p.text_pages,0) AS text_pages,
        coalesce(p.indexed_pages,0) AS indexed_pages,
        coalesce(p.printed_pages,0) AS printed_pages,
        coalesce(p.reviewed_pages,0) AS reviewed_pages,
        coalesce(p.stale_reviews,0) AS stale_reviews,
        m.revision IS NOT NULL AS citation_metadata,
        (SELECT count(*) FROM catalog_author au
            WHERE au.workspace=s.workspace AND au.work=s.work) AS author_count,
        NOT EXISTS (SELECT 1 FROM catalog_author au
            WHERE au.workspace=s.workspace AND au.work=s.work
            AND length(trim(au.name,:whitespace))>0) AS missing_authors,
        NOT EXISTS (SELECT 1 FROM catalog_membership cm
            JOIN catalog_collection cc ON cc.workspace=cm.workspace AND cc.id=cm.collection
            WHERE cm.workspace=s.workspace AND cm.edition=s.id AND cc.archived=0) AS uncollected
    FROM scoped s LEFT JOIN page_counts p ON p.id=s.id
    LEFT JOIN source_metadata m ON m.workspace=s.workspace AND m.edition=s.id
        AND m.revision=p.revision
)'''


class _Budget:
    def __init__(self, cancelled, timeout):
        if cancelled is not None and not callable(cancelled):
            raise ValueError('cancelled must be a callable or None')
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 60):
            raise ValueError('report timeout must be greater than zero and at most 60 seconds')
        self.cancelled = cancelled or (lambda: False)
        self.deadline = monotonic() + timeout
        self.reason = None
        self.callback_error = None

    def stopped(self):
        if self.reason is None:
            try:
                if self.cancelled():
                    self.reason = 'Catalog report cancelled.'
                elif monotonic() >= self.deadline:
                    self.reason = 'Catalog report timed out.'
            except Exception as exc:
                self.callback_error = exc
                self.reason = 'Catalog report cancellation callback failed.'
        return self.reason is not None

    def check(self):
        if self.stopped():
            if self.callback_error is not None:
                raise self.callback_error
            raise TimeoutError(self.reason)


@contextmanager
def _progress(db, budget):
    budget.check()
    db.set_progress_handler(lambda: int(budget.stopped()), 1000)
    try:
        yield
        budget.check()
    except sqlite3.OperationalError:
        budget.check()
        raise
    finally:
        db.set_progress_handler(None, 0)


def _one(db, sql, parameters, budget):
    budget.check()
    cursor = db.execute(sql, parameters)
    names = tuple(column[0] for column in cursor.description)
    row = cursor.fetchone()
    budget.check()
    return dict(zip(names, row))


def _rows(db, sql, parameters, budget):
    budget.check()
    cursor = db.execute(sql, parameters)
    names = tuple(column[0] for column in cursor.description)
    for row in cursor:
        budget.check()
        yield dict(zip(names, row))


def _display(value, length=None):
    """Quote user metadata so its line breaks cannot impersonate report headings."""
    if value is None:
        return 'Unknown'
    total = len(value) if length is None else length
    text = json.dumps(value[:_TEXT_LIMIT], ensure_ascii=False)
    if total > _TEXT_LIMIT:
        text += f' [first {_TEXT_LIMIT} of {total} characters]'
    return text


def _sample_heading(label, count, limit):
    return f'{label}: first {min(count, limit)} of {count}.'


def _authors(db, workspace, work, count, budget):
    names = [_display(row['name'], row['characters']) for row in _rows(db,
        'SELECT substr(name,1,:characters) AS name,length(name) AS characters '
        'FROM catalog_author WHERE workspace=:workspace AND work=:work '
        'ORDER BY position LIMIT :limit',
        dict(workspace=workspace, work=work, characters=_TEXT_LIMIT, limit=_AUTHOR_LIMIT), budget)]
    if not names:
        return 'Unknown'
    text = '; '.join(names)
    if count > _AUTHOR_LIMIT:
        text += f' [first {_AUTHOR_LIMIT} of {count} authors, in stored order]'
    return text


def _coverage(db, parameters, limit, budget):
    where = '''e.workspace=:workspace AND NOT EXISTS (
        SELECT 1 FROM catalog_membership m JOIN catalog_collection c
        ON c.workspace=m.workspace AND c.id=m.collection
        WHERE m.workspace=e.workspace AND m.edition=e.id AND c.archived=0)'''
    sections = [('Library editions in no active collection', where)]
    if parameters['collection'] is not None:
        sections.append(('Library editions outside the selected collection',
            '''e.workspace=:workspace AND NOT EXISTS (
                SELECT 1 FROM catalog_membership m WHERE m.workspace=e.workspace
                AND m.edition=e.id AND m.collection=:collection)'''))
    lines = ['Collection coverage (whole authorized library)']
    for label, predicate in sections:
        count = _one(db, 'SELECT count(*) AS n FROM catalog_edition e WHERE ' + predicate,
                     parameters, budget)['n']
        lines.append(_sample_heading(label, count, limit))
        for row in _rows(db, '''SELECT e.id,substr(w.title,1,:characters) AS title,
                length(w.title) AS title_characters FROM catalog_edition e
                JOIN catalog_work w ON w.workspace=e.workspace AND w.id=e.work
                WHERE ''' + predicate + ' ORDER BY e.id COLLATE BINARY LIMIT :detail_limit',
                parameters, budget):
            lines.append(f"  Edition {_display(row['id'])}: "
                         f"{_display(row['title'], row['title_characters'])}")
    lines.append('Membership in an archived collection does not count as active collection coverage.')
    return lines


def _duplicates(db, parameters, limit, budget):
    groups = _SCOPE + ''', groups AS (
        SELECT asset,count(*) AS editions FROM scoped GROUP BY asset HAVING count(*)>1
    )'''
    counts = _one(db, groups + '''SELECT count(*) AS groups,
        coalesce(sum(editions),0) AS editions,
        coalesce(sum(editions-1),0) AS additional FROM groups''', parameters, budget)
    lines = ['Exact-byte duplicate candidates (registered SHA-256)',
             f"Duplicate candidate groups: {counts['groups']}.",
             f"Editions in candidate groups: {counts['editions']}.",
             f"Additional edition records sharing an original: {counts['additional']}.",
             _sample_heading('Groups shown', counts['groups'], limit)]
    for row in _rows(db, groups + '''SELECT g.asset,g.editions,a.mime,a.size FROM groups g
            JOIN catalog_asset a ON a.digest=g.asset ORDER BY g.asset COLLATE BINARY
            LIMIT :detail_limit''', parameters, budget):
        lines.append(f"  SHA-256 {row['asset']}; {row['editions']} editions; "
                     f"{_display(row['mime'])}; {row['size']} registered bytes.")
        lines.append('  ' + _sample_heading('Member editions shown', row['editions'], _MEMBER_LIMIT))
        members = dict(parameters, duplicate_asset=row['asset'], member_limit=_MEMBER_LIMIT)
        for member in _rows(db, _SCOPE + ''' SELECT id,substr(title,1,:characters) AS title,
                length(title) AS title_characters FROM scoped WHERE asset=:duplicate_asset
                ORDER BY id COLLATE BINARY LIMIT :member_limit''', members, budget):
            lines.append(f"    Edition {_display(member['id'])}: "
                         f"{_display(member['title'], member['title_characters'])}")
    lines.append('A shared registered digest can represent intentional editions. '
                 'Original bytes are not re-opened here; no records are merged or removed.')
    return lines


def catalog_report(db: sqlite3.Connection, workspace: str, *, collection=None,
                   cancelled=None, timeout=10.0, detail_limit=100) -> str:
    """Return an accessible text report for a caller-owned authorized snapshot.

    Every summary counter covers the full selected scope. Inventory, collection
    gap and duplicate examples are bounded independently and disclose their
    totals; author and text previews also disclose any shortening. Cancellation
    or timeout raises ``TimeoutError`` instead of returning a partial report.
    Active recorded page reviews are not independent quote verification.
    """
    workspace = _identity(workspace)
    if collection is not None:
        collection = _identity(collection)
    if type(detail_limit) is not int or not 1 <= detail_limit <= 200:
        raise ValueError('report detail limit must be between 1 and 200')
    budget = _Budget(cancelled, timeout)
    budget.check()
    if not db.in_transaction or not db.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise ValueError('caller snapshot and foreign keys required')
    parameters = dict(workspace=workspace, collection=collection, whitespace=_WHITESPACE,
                      scope_limit=-1, detail_limit=detail_limit, characters=_TEXT_LIMIT)
    with _progress(db, budget):
        library = db.execute('SELECT name FROM workspace_meta WHERE workspace=? AND archived=0',
                             (workspace,)).fetchone()
        if library is None:
            raise ValueError('The selected library is missing or archived.')
        collection_name = None
        if collection is not None:
            selected = db.execute('SELECT name FROM catalog_collection '
                'WHERE workspace=? AND id=? AND archived=0', (workspace, collection)).fetchone()
            if selected is None:
                raise ValueError('unknown or archived collection in this library')
            collection_name = selected[0]

        summary = _one(db, _STATES + ''' SELECT count(*) AS editions,
            count(DISTINCT work) AS works,count(DISTINCT asset) AS assets,
            coalesce(sum(revision IS NOT NULL),0) AS published,
            coalesce(sum(pages),0) AS pages,coalesce(sum(text_pages),0) AS text_pages,
            coalesce(sum(text_pages>0),0) AS editions_with_text,
            coalesce(sum(indexed_pages),0) AS indexed_pages,
            coalesce(sum(pages>0 AND pages=indexed_pages),0) AS fully_indexed,
            coalesce(sum(printed_pages),0) AS printed_pages,
            coalesce(sum(reviewed_pages),0) AS reviewed_pages,
            coalesce(sum(stale_reviews),0) AS stale_reviews,
            coalesce(sum(citation_metadata),0) AS citation_metadata,
            coalesce(sum(title IS NULL OR length(trim(title,:whitespace))=0),0) AS missing_title,
            coalesce(sum(missing_authors),0) AS missing_authors,
            coalesce(sum(language IS NULL OR length(trim(language,:whitespace))=0),0) AS missing_language,
            coalesce(sum(edition IS NULL OR length(trim(edition,:whitespace))=0),0) AS missing_edition,
            coalesce(sum(mime='text/plain'),0) AS plain_text
            FROM states''', parameters, budget)
        asset_bytes = _one(db, _SCOPE + ''' SELECT coalesce(sum(size),0) AS bytes
            FROM (SELECT DISTINCT asset,size FROM scoped)''', parameters, budget)['bytes']
        active_collections = _one(db, 'SELECT count(*) AS n FROM catalog_collection '
            'WHERE workspace=:workspace AND archived=0', parameters, budget)['n']

        lines = ['CARR catalog report', f'Library: {_display(library[0])}.',
            ('Scope: all catalogued editions.' if collection is None else
             f'Scope: collection {_display(collection_name)}; ID {_display(collection)}.'), '',
            'Catalog overview (selected scope)',
            f"Registered editions: {summary['editions']}.",
            f"Distinct works: {summary['works']}.",
            f"Distinct registered originals: {summary['assets']}.",
            f'Registered original bytes, counted once per digest: {asset_bytes}.',
            f'Active collections in the whole library: {active_collections}.', '',
            'Source and text status (active revisions in selected scope)',
            f"Editions with an active published revision: {summary['published']}.",
            f"Editions without an active published revision: {summary['editions']-summary['published']}.",
            f"Raw pages: {summary['pages']}.",
            f"Pages containing nonblank text: {summary['text_pages']}.",
            f"Editions containing nonblank text: {summary['editions_with_text']}.",
            f"Indexed active pages matching stored raw text: {summary['indexed_pages']}.",
            f"Editions with every active raw page indexed: {summary['fully_indexed']}.",
            f"Pages with recorded verified printed labels: {summary['printed_pages']}.",
            f"Pages with active recorded reviews matching stored text digests: {summary['reviewed_pages']}.",
            f"Active review records with mismatched stored text digests: {summary['stale_reviews']}.",
            f"Active revisions with frozen citation metadata: {summary['citation_metadata']}.",
            f"Editions registered as plain-text sources: {summary['plain_text']}.",
            'Text presence, indexed rows, printed-label records and review records are separate facts. '
            'They do not certify quote accuracy or citation eligibility.',
            'Citation eligibility is checked for the individual source, locator and quote when creating a citation.',
            'This report does not re-open retained originals, recalculate page digests, '
            'validate review provenance or inspect internal full-text index integrity.', '',
            'Catalog metadata gaps (selected scope)',
            f"Editions missing a title: {summary['missing_title']}.",
            f"Editions missing authors: {summary['missing_authors']}.",
            f"Editions missing a language: {summary['missing_language']}.",
            f"Editions missing an edition label: {summary['missing_edition']}.",
            'Unknown metadata fields are prompts for catalog review; an edition label may be inapplicable.', '',
            'Source inventory (selected scope)',
            _sample_heading('Edition details', summary['editions'], detail_limit)]

        detail_parameters = dict(parameters, scope_limit=detail_limit)
        for row in _rows(db, _STATES + ''' SELECT id,work,asset,mime,size,author_count,
                substr(title,1,:characters) AS title,length(title) AS title_characters,
                substr(language,1,:characters) AS language,length(language) AS language_characters,
                substr(edition,1,:characters) AS edition,length(edition) AS edition_characters,
                substr(volume,1,:characters) AS volume,length(volume) AS volume_characters,
                revision,pages,text_pages,indexed_pages,printed_pages,reviewed_pages,
                stale_reviews,citation_metadata,uncollected
                FROM states ORDER BY id COLLATE BINARY''', detail_parameters, budget):
            lines.extend([f"Edition {_display(row['id'])}; work {_display(row['work'])}.",
                f"  Title: {_display(row['title'], row['title_characters'])}.",
                f"  Authors: {_authors(db, workspace, row['work'], row['author_count'], budget)}.",
                f"  Language: {_display(row['language'], row['language_characters'])}; "
                f"edition: {_display(row['edition'], row['edition_characters'])}; "
                f"volume: {_display(row['volume'], row['volume_characters'])}.",
                f"  Registered original: SHA-256 {row['asset']}; "
                f"{_display(row['mime'])}; {row['size']} bytes.",
                ('  Active published revision: absent.' if row['revision'] is None else
                 f"  Active published revision: {_display(row['revision'])}."),
                f"  Raw pages: {row['pages']}; nonblank text: {row['text_pages']}; "
                f"matching indexed pages: {row['indexed_pages']}.",
                f"  Printed-label records: {row['printed_pages']}; "
                f"matching active review records: {row['reviewed_pages']}; "
                f"mismatched review records: {row['stale_reviews']}.",
                f"  Frozen citation metadata: {'present' if row['citation_metadata'] else 'absent'}; "
                f"active collection membership: {'absent' if row['uncollected'] else 'present'}."])
        lines.append('')
        lines.extend(_coverage(db, parameters, detail_limit, budget))
        lines.append('')
        lines.extend(_duplicates(db, parameters, detail_limit, budget))
        lines.extend(['', 'All summary counts cover the full stated scope. '
                      'Detail samples are ordered by edition ID; duplicate groups are ordered by digest. '
                      'Use the catalog or inventory export to inspect entries outside the displayed samples.'])
        budget.check()
        report = '\n'.join(lines) + '\n'
        budget.check()
        return report
