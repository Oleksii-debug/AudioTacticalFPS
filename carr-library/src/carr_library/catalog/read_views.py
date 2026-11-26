"""Read-only scoped views for the existing Catalog; no migration or new index."""
from dataclasses import dataclass
from .membership import Catalog, _identity, _metadata


@dataclass(frozen=True)
class EditionView:
    edition_id: str
    work_id: str
    asset: str
    title: str | None
    language: str | None
    edition: str | None
    volume: str | None
    authors: tuple[str, ...]


def _snapshot(catalog):
    if not isinstance(catalog, Catalog):
        raise TypeError('Catalog required')
    db, scope = catalog.db, _identity(catalog.workspace)
    if not db.in_transaction or not db.execute('PRAGMA foreign_keys').fetchone()[0]:
        raise ValueError('caller snapshot and foreign keys required')
    if not db.execute('SELECT 1 FROM workspace WHERE id=?', (scope,)).fetchone():
        raise ValueError('unknown workspace')
    return db, scope


def active_collections(catalog: Catalog):
    """PR64 callback: ordered (id, name) pairs in the authorized snapshot."""
    db, scope = _snapshot(catalog)
    rows = tuple(tuple(r) for r in db.execute(
        'SELECT id,name FROM catalog_collection WHERE workspace=? AND archived=0 '
        'ORDER BY id COLLATE BINARY LIMIT 10001', (scope,)))
    if len(rows) > 10000:
        raise ValueError('too many collections for this view')
    return rows


def browse_editions(catalog: Catalog, *, collection=None, title=None, author=None,
                    language=None, edition=None, edition_id=None, after=None, limit=100):
    """Bounded edition page, ordered by ID. Pass last ID as after for next page.

    Title/author are literal, case-sensitive Unicode substrings (not LIKE or
    FTS syntax). Language/edition are exact matches. None disables a filter.
    Author lists remain complete and in source order, even with author filter.
    Keep one caller snapshot across pages when consistent pagination is needed.
    Metadata is catalog data, not independent source/citation attestation.
    """
    db, scope = _snapshot(catalog)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError('invalid page size')
    if after is not None:
        after = _identity(after)
    if edition_id is not None:
        edition_id = _identity(edition_id)
    if collection is not None:
        collection = _identity(collection)
        catalog._active_collection(collection)
    title, author, language, edition = map(_metadata, (title, author, language, edition))
    if any(v == '' for v in (title, author, language, edition)):
        raise ValueError('empty filter')
    rows = db.execute('''WITH selected AS (
        SELECT e.workspace,e.id,e.work,e.asset,w.title,e.language,e.edition,e.volume
        FROM catalog_edition e JOIN catalog_work w ON w.workspace=e.workspace AND w.id=e.work
        WHERE e.workspace=:scope AND (:after IS NULL OR e.id COLLATE BINARY > :after)
        AND (:edition_id IS NULL OR e.id=:edition_id)
        AND (:collection IS NULL OR EXISTS (SELECT 1 FROM catalog_membership m
            WHERE m.workspace=e.workspace AND m.edition=e.id AND m.collection=:collection))
        AND (:title IS NULL OR instr(w.title,:title)>0)
        AND (:author IS NULL OR EXISTS (SELECT 1 FROM catalog_author a
            WHERE a.workspace=e.workspace AND a.work=e.work AND instr(a.name,:author)>0))
        AND (:language IS NULL OR e.language=:language)
        AND (:edition IS NULL OR e.edition=:edition)
        ORDER BY e.id COLLATE BINARY LIMIT :limit
    ) SELECT s.id,s.work,s.asset,s.title,s.language,s.edition,s.volume,a.name
      FROM selected s LEFT JOIN catalog_author a ON a.workspace=s.workspace AND a.work=s.work
      ORDER BY s.id COLLATE BINARY,a.position''',
        dict(scope=scope, after=after, edition_id=edition_id, collection=collection, title=title,
             author=author, language=language, edition=edition, limit=limit))
    grouped = {}
    for row in rows:
        fields, name = tuple(row)[:7], row[7]
        names = grouped.setdefault(fields, [])
        if name is not None:
            names.append(name)
    return tuple(EditionView(*fields, tuple(names)) for fields, names in grouped.items())


def get_edition(catalog: Catalog, edition_id: str) -> EditionView:
    """Read one exact edition in the caller's authorized snapshot.

    Uses the same ordered metadata view as browsing. The identity filter never
    treats an edition ID as a title, edition label or SQL/FTS expression.
    A returned frozen view can also guard an optimistic metadata edit.
    """
    rows = browse_editions(catalog, edition_id=_identity(edition_id), limit=1)
    if not rows:
        raise ValueError('unknown edition in this library')
    return rows[0]
