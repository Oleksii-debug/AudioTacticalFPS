"""Catalog-scoped literal retrieval on one SQLite read snapshot.

The catalog and page reader must observe the same transaction. Edition IDs
are resolved at the action boundary, never accepted from a previously cached
collection listing. This composes the existing catalog and literal engines.
"""
from pathlib import Path
import sqlite3
from time import monotonic

from ..catalog.membership import Catalog
from .literal import search


def _budget(timeout, cancelled):
    if type(timeout) not in (int, float) or not 0 < timeout <= 60 or not callable(cancelled):
        raise ValueError('invalid search budget')
    deadline = monotonic() + timeout
    latched = False

    def stopped():
        nonlocal latched
        latched = latched or bool(cancelled()) or monotonic() >= deadline
        return latched

    def check():
        if stopped():
            raise TimeoutError('literal search cancelled or timed out')

    return stopped, check


def collection_search(db, workspace, query, *, collection=None, mode='phrase',
                      limit=50, offset=0, timeout=5.0, cancelled=lambda: False,
                      eligible_edition_ids=None):
    """Search within a caller-owned, already active transaction.

    The caller owns connection lifecycle and its SQL progress handler. This
    function neither commits nor changes the caller's progress handler. The
    file wrapper additionally applies the same budget to native source SQL.
    A missing/archived collection or workspace raises; it is never broadened
    into an all-library query. None explicitly selects the entire workspace.
    """
    stopped, check = _budget(timeout, cancelled)
    check()
    if not db.in_transaction:
        raise ValueError('one active catalog and source transaction required')
    try:
        catalog = Catalog(db, workspace)
        check()
        ids = tuple(row[0] for row in catalog.editions(collection))
        if eligible_edition_ids is not None:
            from itertools import islice
            if isinstance(eligible_edition_ids, (str, bytes, bytearray)):
                raise ValueError('eligible editions must be a bounded ID iterable')
            subset = tuple(islice(eligible_edition_ids, 501))
            if len(subset) > 500 or any(not isinstance(value, str) or not value for value in subset):
                raise ValueError('invalid eligible edition filter')
            # Eligibility can narrow the freshly resolved catalog permission;
            # it can never authorize another workspace or collection.
            allowed = set(subset)
            ids = tuple(edition for edition in ids if edition in allowed)
        check()
        result = search(db, workspace, query, edition_ids=ids, mode=mode,
                        limit=limit, offset=offset, timeout=timeout,
                        cancelled=stopped)
        check()
        return result
    except sqlite3.OperationalError as exc:
        if stopped():
            raise TimeoutError('literal search cancelled or timed out') from exc
        raise


def collection_search_file(path, workspace, query, *, collection=None,
                           mode='phrase', limit=50, offset=0, timeout=5.0,
                           cancelled=lambda: False):
    """Own a read-only snapshot and one sticky catalog/search deadline.

    Uses WAL-aware mode=ro, not immutable=1. Concurrent updates can commit but
    cannot mix newer source text with older collection membership. A subsequent
    search opens a new snapshot and observes any committed removal/revocation.
    """
    stopped, check = _budget(timeout, cancelled)
    check()
    db = None
    try:
        db = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro',
                             uri=True, timeout=0.0)
        db.set_progress_handler(lambda: int(stopped()), 1000)
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA query_only=ON')
        db.execute('PRAGMA trusted_schema=OFF')
        db.execute('BEGIN')
        return collection_search(db, workspace, query, collection=collection,
                                 mode=mode, limit=limit, offset=offset,
                                 timeout=timeout, cancelled=stopped)
    except sqlite3.OperationalError as exc:
        if stopped():
            raise TimeoutError('literal search cancelled or timed out') from exc
        raise
    finally:
        if db is not None:
            db.close()
