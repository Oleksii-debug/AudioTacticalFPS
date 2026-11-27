"""Bounded source windows over the existing catalog and retrieval engines.

A continuation is a read cursor, never a citation or permission. Every request
opens a fresh authorized snapshot; a changed catalog/source generation requires
a new search. Rankings belong to one source window, not the entire library.
No source passages or result handles are retained in a continuation.
"""
from dataclasses import dataclass
from hashlib import sha256
import json
import unicodedata

from ..catalog.membership import Catalog, _identity, _metadata
from ..catalog.read_views import browse_editions
from .literal import MAX_QUERY


MAX_EDITIONS = 500


def _digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True).encode('ascii')).hexdigest()


def scope_generation(db, workspace, check):
    """Stream compact catalog/revision metadata; never collect a whole corpus."""
    queries = (
        'SELECT workspace,name,description,archived FROM workspace_meta WHERE workspace=?',
        'SELECT id,title FROM catalog_work WHERE workspace=? ORDER BY id COLLATE BINARY',
        'SELECT work,position,name FROM catalog_author WHERE workspace=? ORDER BY work COLLATE BINARY,position',
        'SELECT id,work,asset,language,edition,volume FROM catalog_edition WHERE workspace=? ORDER BY id COLLATE BINARY',
        'SELECT id,name,archived FROM catalog_collection WHERE workspace=? ORDER BY id COLLATE BINARY',
        'SELECT collection,edition FROM catalog_membership WHERE workspace=? ORDER BY collection COLLATE BINARY,edition COLLATE BINARY',
        'SELECT edition,revision FROM active WHERE workspace=? ORDER BY edition COLLATE BINARY',
    )
    digest = sha256()
    for number, query in enumerate(queries):
        check()
        digest.update(bytes((number,)))
        cursor = db.execute(query, (workspace,))
        try:
            for row in cursor:
                check()
                # One metadata row at a time, independent of edition count.
                encoded = json.dumps(tuple(row), ensure_ascii=True,
                                     separators=(',', ':')).encode('ascii')
                if len(encoded) > 2 * 1024 * 1024:
                    raise ValueError('Catalog metadata exceeds the continuation row budget.')
                digest.update(len(encoded).to_bytes(8, 'big'))
                digest.update(encoded)
        finally:
            cursor.close()
    check()
    return digest.hexdigest()


@dataclass(frozen=True)
class SearchCursor:
    criteria: str
    generation: str
    edition_after: str | None
    editions_before: int
    offset: int
    edition_limit: int


@dataclass(frozen=True)
class SearchPage:
    results: tuple
    next_cursor: SearchCursor | None
    edition_start: int
    edition_end: int
    result_offset: int
    has_more_editions: bool


@dataclass(frozen=True)
class PageRequest:
    cursor: SearchCursor | None = None
    edition_limit: int = MAX_EDITIONS


@dataclass(frozen=True)
class CatalogWindow:
    views: tuple
    criteria: str
    generation: str
    after: str | None
    editions_before: int
    offset: int
    edition_limit: int
    more_editions: bool

    def finish(self, results, consumed, more_results):
        continuation = None
        if more_results:
            continuation = SearchCursor(self.criteria, self.generation, self.after,
                self.editions_before, self.offset + consumed, self.edition_limit)
        elif self.more_editions:
            continuation = SearchCursor(self.criteria, self.generation,
                self.views[-1].edition_id, self.editions_before + len(self.views),
                0, self.edition_limit)
        return SearchPage(tuple(results), continuation,
            self.editions_before + bool(self.views), self.editions_before + len(self.views),
            self.offset, self.more_editions)


def begin_page(app, db, workspace, query, request, options, check):
    if type(request) is not PageRequest or type(request.edition_limit) is not int \
            or not 1 <= request.edition_limit <= MAX_EDITIONS:
        raise ValueError('Choose a bounded catalog page of at most 500 editions.')
    if (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY
            or any(unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\n\r' for c in query)):
        raise ValueError('Enter a nonblank, bounded search query.')
    filters = {key: _metadata(options[key]) for key in ('title', 'author', 'language', 'edition')}
    if any(value == '' for value in filters.values()):
        raise ValueError('Empty metadata filters are not supported.')
    collection = options['collection']
    catalog = Catalog(db, workspace)
    if collection is not None:
        catalog._active_collection(_identity(collection))
    ranker = app.semantic_ranker
    criteria = _digest(dict(options, **filters, query=query, workspace=workspace,
        roots=[str(app.root), str(app.database), str(app.assets_dir), str(app.assets.root)],
        edition_limit=request.edition_limit, ranker=id(ranker),
        threshold=getattr(ranker, 'minimum_similarity', None)))
    cursor = request.cursor
    after, before, offset = None, 0, 0
    if cursor is not None:
        if (type(cursor) is not SearchCursor or cursor.criteria != criteria
                or cursor.edition_limit != request.edition_limit
                or type(cursor.editions_before) is not int or cursor.editions_before < 0
                or type(cursor.offset) is not int or cursor.offset < 0):
            raise ValueError('The search continuation belongs to different options or library data.')
        after, before, offset = cursor.edition_after, cursor.editions_before, cursor.offset
        if after is not None:
            _identity(after)
    generation = scope_generation(db, workspace, check)
    if cursor is not None and cursor.generation != generation:
        raise ValueError('The source catalog changed. Start a new search before continuing.')
    views = browse_editions(catalog, collection=collection, **filters,
                            after=after, limit=request.edition_limit)
    check()
    more = bool(views and len(views) == request.edition_limit and browse_editions(
        catalog, collection=collection, **filters, after=views[-1].edition_id, limit=1))
    check()
    return CatalogWindow(views, criteria, generation, after, before, offset,
                         request.edition_limit, more)
