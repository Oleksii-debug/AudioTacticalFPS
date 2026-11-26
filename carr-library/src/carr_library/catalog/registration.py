"""Compose the existing catalog in a caller-owned transaction; no schema changes."""
from dataclasses import dataclass
from .membership import Catalog, _identity, _metadata


@dataclass(frozen=True)
class Registration:
    work: str
    edition_id: str
    manifest: dict
    title: str | None = None
    authors: tuple[str, ...] = ()
    language: str | None = None
    edition: str | None = None
    volume: str | None = None
    collections: tuple[str, ...] = ()


def register_batch(catalog: Catalog, records: tuple[Registration, ...]):
    """Register exact explicit identities or reject conflicts; never infer editions.

    The caller supplies manifests from bounded intake. No source bytes are read.
    This service does not commit the outer transaction; SQLite itself may roll it back.
    An exact retry adds only requested collection memberships; metadata is not edited.
    """
    catalog._write()
    if not isinstance(records, tuple) or not 1 <= len(records) <= 5000:
        raise ValueError('invalid batch')
    db, scope = catalog.db, catalog.workspace
    db.execute('SAVEPOINT catalog_register_batch')
    try:
        for r in records:
            if not isinstance(r, Registration):
                raise ValueError('invalid registration')
            work, eid = _identity(r.work), _identity(r.edition_id)
            title = _metadata(r.title)
            if not isinstance(r.authors, tuple) or len(r.authors) > 100:
                raise ValueError('invalid authors')
            authors = tuple(_metadata(a) for a in r.authors)
            if any(not a or not a.strip() for a in authors):
                raise ValueError('empty author')
            if not isinstance(r.collections, tuple) or len(r.collections) > 100:
                raise ValueError('invalid collections')
            language, edition, volume = map(_metadata, (r.language, r.edition, r.volume))
            digest, mime, size = (r.manifest[k] for k in ('sha256', 'mime', 'size'))
            if type(size) is not int or not isinstance(digest, str) or not isinstance(mime, str):
                raise ValueError('invalid manifest')
            row = db.execute('SELECT title FROM catalog_work WHERE workspace=? AND id=?', (scope, work)).fetchone()
            if row is None:
                catalog.add_work(work, title, authors)
            else:
                actual = tuple(a[0] for a in db.execute('SELECT name FROM catalog_author WHERE workspace=? AND work=? ORDER BY position', (scope, work)))
                if row[0] != title or actual != authors:
                    raise ValueError('work identity conflict')
            row = db.execute('''SELECT e.work,e.asset,a.mime,a.size,e.language,e.edition,e.volume
                FROM catalog_edition e JOIN catalog_asset a ON a.digest=e.asset
                WHERE e.workspace=? AND e.id=?''', (scope, eid)).fetchone()
            if row is None:
                catalog.add_edition(eid, work, r.manifest, language=language, edition=edition, volume=volume)
            elif tuple(row) != (work, digest, mime, size, language, edition, volume):
                raise ValueError('edition identity conflict')
            for collection in r.collections:
                catalog.assign(collection, eid)
    except BaseException:
        # SQLite RAISE(ROLLBACK) may already have ended the outer transaction.
        if db.in_transaction:
            db.execute('ROLLBACK TO catalog_register_batch')
            db.execute('RELEASE catalog_register_batch')
        raise
    db.execute('RELEASE catalog_register_batch')
