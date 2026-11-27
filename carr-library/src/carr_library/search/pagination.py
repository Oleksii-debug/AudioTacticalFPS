"""Bounded continuation over existing authorized catalog and source adapters.

A page is a source batch, not a new global relevance index. Literal matches
follow edition/ordinal/page/offset order. Thematic/semantic relevance is local
to each explicitly identified source batch. Every call uses a fresh read
snapshot and rejects a changed catalog/revision fingerprint before continuing.
No cursor authorizes a quotation: ordinary source actions still resolve and
verify their retained original bytes.
"""
from base64 import b64decode, urlsafe_b64encode
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
import unicodedata

from ..catalog.membership import Catalog, _identity, _metadata
from ..catalog.read_views import browse_editions
from ..research.topic_tree import TopicTree
from . import literal, thematic

MAX_CURSOR_BYTES = 8192
MAX_SCOPE_BYTES = 2 * 1024 * 1024
ORDERING = ('Source batches in edition, source-page order; exact matches in raw-offset order. '
            'Thematic/semantic relevance is local to each batch, never a global score; '
            'the existing ranker returns up to 100 results per source batch.')


@dataclass(frozen=True)
class SearchPageBudget:
    """Memory/work bounds for one continuation, not whole-library ceilings."""
    editions: int = 500
    pages: int = 100
    characters: int = literal.MAX_CHARS
    fingerprint_rows: int = 1_000_000
    empty_catalog_batches: int = 20

    def __post_init__(self):
        for value, maximum in ((self.editions, 500), (self.pages, 100),
                               (self.characters, literal.MAX_CHARS),
                               (self.fingerprint_rows, 2**63 - 1),
                               (self.empty_catalog_batches, 100)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Invalid search page resource budget.')


@dataclass(frozen=True)
class SearchPage:
    results: tuple
    next_cursor: str | None
    page_number: int
    result_start: int
    result_end: int
    source_pages_examined: int
    total_source_pages: int
    total_editions: int
    snapshot: str
    ordering: str = ORDERING


def _json(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False,
                      sort_keys=True, separators=(',', ':')).encode('ascii')


def binding(value):
    return sha256(_json(value)).hexdigest()


def semantic_identity(app, mode):
    if mode != 'semantic' or app.semantic_ranker is None:
        return None
    from ..semantic import APPROVED_MANIFEST
    ranker = app.semantic_ranker
    return {'adapter': type(ranker).__module__ + '.' + type(ranker).__qualname__,
            'model': binding(APPROVED_MANIFEST),
            'directory': str(getattr(ranker, 'directory', '')),
            'minimum_similarity': getattr(ranker, 'minimum_similarity', None)}


def encode_cursor(kind, scope, state):
    raw = _json({'kind': kind, 'binding': scope, 'state': state})
    token = urlsafe_b64encode(raw).decode('ascii')
    if len(token) > MAX_CURSOR_BYTES:
        raise ValueError('Search continuation exceeds its metadata budget.')
    return token


def decode_cursor(token, kind, scope):
    if not isinstance(token, str) or not token or len(token) > MAX_CURSOR_BYTES:
        raise ValueError('Invalid bounded search continuation.')

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError('Duplicate search continuation field.')
            value[key] = item
        return value

    try:
        raw = b64decode(token.encode('ascii'), altchars=b'-_', validate=True)
        value = json.loads(raw.decode('ascii'), object_pairs_hook=unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError('Invalid bounded search continuation.') from None
    if (type(value) is not dict or set(value) != {'kind', 'binding', 'state'}
            or value['kind'] != kind or value['binding'] != scope
            or type(value['state']) is not dict):
        raise ValueError('The search continuation belongs to another query, scope or data folder.')
    return value['state']


def _key(value, length):
    if value is None:
        return None
    if (type(value) not in (tuple, list) or len(value) != length
            or not isinstance(value[0], str) or not value[0] or len(value[0]) > 256
            or any(type(item) is not int or not 0 <= item < 2**63 for item in value[1:])
            or value[1] < 1 or value[2] < 1
            or (length == 5 and value[3] >= value[4])):
        raise ValueError('Invalid search source continuation.')
    return tuple(value)


def _state(value):
    if set(value) != {'snapshot', 'catalog', 'source', 'match', 'rank', 'last',
                      'number', 'seen', 'scanned'}:
        raise ValueError('Invalid search continuation fields.')
    if not isinstance(value['snapshot'], str) or not re.fullmatch('[0-9a-f]{64}', value['snapshot']):
        raise ValueError('Invalid search snapshot identity.')
    if value['catalog'] is not None and (not isinstance(value['catalog'], str)
            or not value['catalog'] or len(value['catalog']) > 256):
        raise ValueError('Invalid catalog continuation.')
    for name in ('rank', 'number', 'seen', 'scanned'):
        if type(value[name]) is not int or not 0 <= value[name] < 2**63:
            raise ValueError('Invalid search continuation position.')
    if not 0 <= value['rank'] <= 100 or value['number'] < 1:
        raise ValueError('Invalid result continuation position.')
    value['source'] = _key(value['source'], 3)
    value['match'] = _key(value['match'], 5)
    last = value['last']
    if last is not None:
        if (type(last) not in (tuple, list) or len(last) != 5
                or any(not isinstance(v, str) or not v or len(v) > 256 for v in last[:2])
                or any(type(v) is not int or v < 0 for v in last[2:])
                or last[2] < 1 or last[3] >= last[4]):
            raise ValueError('Invalid displayed source span.')
        value['last'] = tuple(last)
    return value


def _fingerprint(db, catalog, filters, policy, budget):
    """Stream catalog metadata and sealed source identities, never raw pages."""
    digest = sha256(b'carr-search-snapshot/1\n')
    after = None
    rows_seen = editions = pages = 0
    while True:
        budget.check()
        views = browse_editions(catalog, after=after, limit=policy.editions, **filters)
        if not views:
            break
        for view in views:
            budget.check()
            rows_seen += 1
            editions += 1
            if rows_seen > policy.fingerprint_rows:
                raise ValueError('Search fingerprint exceeds its configured row budget.')
            digest.update(_json(('edition', asdict(view))))
            digest.update(b'\n')
        ids = tuple(view.edition_id for view in views)
        query = ('SELECT p.id,p.edition,p.revision,p.ordinal,p.physical,p.printed,'
                 'p.verified,p.digest,length(p.raw),p.asset,m.author,m.title,m.volume,m.edition_label '
                 'FROM active_page p LEFT JOIN source_metadata m '
                 'ON m.workspace=p.workspace AND m.edition=p.edition AND m.revision=p.revision '
                 'WHERE p.workspace=? AND p.edition IN (' + ','.join('?' for _ in ids) +
                 ') ORDER BY p.edition COLLATE BINARY,p.ordinal,p.id')
        cursor = db.execute(query, (catalog.workspace, *ids))
        try:
            for row in cursor:
                budget.check()
                rows_seen += 1
                pages += 1
                if rows_seen > policy.fingerprint_rows:
                    raise ValueError('Search fingerprint exceeds its configured row budget.')
                digest.update(_json(('page', tuple(row))))
                digest.update(b'\n')
        finally:
            cursor.close()
        after = views[-1].edition_id
    budget.check()
    return digest.hexdigest(), editions, pages


def _source_batch(db, catalog, filters, policy, budget, state):
    """Return a bounded page-ID batch and its owning catalog slice."""
    for _ in range(policy.empty_catalog_batches):
        budget.check()
        views = browse_editions(catalog, after=state['catalog'],
                                limit=policy.editions, **filters)
        if not views:
            return {}, (), True
        allowed = {view.edition_id: view.work_id for view in views}
        params = (catalog.workspace, *allowed)
        sql = ('SELECT id,edition,ordinal,length(raw) FROM active_page WHERE workspace=? '
               'AND edition IN (' + ','.join('?' for _ in allowed) + ')')
        if state['source'] is not None:
            sql += ' AND (edition,ordinal,id) > (?,?,?)'
            params += state['source']
        sql += ' ORDER BY edition COLLATE BINARY,ordinal,id LIMIT ?'
        cursor = db.execute(sql, (*params, policy.pages + 1))
        selected, size = [], 0
        try:
            for row in cursor:
                budget.check()
                row = tuple(row)
                if type(row[3]) is not int or row[3] < 0:
                    raise ValueError('Invalid source page size.')
                if row[3] > policy.characters and not selected:
                    raise ValueError('One source page exceeds the configured search character budget.')
                if len(selected) == policy.pages or size + row[3] > policy.characters:
                    break
                selected.append(row)
                size += row[3]
        finally:
            cursor.close()
        if selected:
            return allowed, tuple(selected), False
        state.update(catalog=views[-1].edition_id, source=None, match=None, rank=0)
    # Empty editions are still finite work. A continuation can move past the
    # current catalog slice without holding a worker or collecting all metadata.
    return {}, (), False


def search_page(app, workspace, query, *, mode='phrase', collection=None, title=None,
                author=None, language=None, edition=None, limit=100, timeout=60.0,
                cancelled=lambda: False, authorized_topic=None, on_status=None,
                result_mode=None, cursor=None, page_budget=None):
    from ..application import _SearchBudget
    operation = _SearchBudget(timeout, cancelled)
    operation.check()
    if mode not in ('word', 'phrase', 'terms', 'advanced', 'thematic', 'semantic'):
        raise ValueError('Unknown search mode.')
    if result_mode not in (None, 'short', 'thematic'):
        raise ValueError('Choose Short quotations or Thematic excerpts.')
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('Invalid result page size.')
    if (not isinstance(query, str) or not query.strip() or len(query) > literal.MAX_QUERY
            or any(unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\n\r' for c in query)):
        raise ValueError('Enter a bounded nonblank search query.')
    if on_status is not None and not callable(on_status):
        raise ValueError('Search status callback must be callable.')
    policy = page_budget if page_budget is not None else SearchPageBudget()
    if type(policy) is not SearchPageBudget:
        raise ValueError('A declared search page resource budget is required.')
    workspace = _identity(workspace)
    collection = _identity(collection) if collection is not None else None
    authorized_topic = _identity(authorized_topic) if authorized_topic is not None else None
    metadata = dict(zip(('title', 'author', 'language', 'edition'),
                        map(_metadata, (title, author, language, edition))))
    if any(value == '' for value in metadata.values()):
        raise ValueError('Empty metadata filters are not supported.')
    filters = dict(collection=collection, **metadata)
    operation.check()
    scope = binding({'root': str(app.root), 'database': str(app.database),
                     'workspace': workspace, 'query': query, 'mode': mode,
                     'result_mode': result_mode, 'filters': filters, 'limit': limit,
                     'topic': authorized_topic, 'budget': asdict(policy),
                     'ranker': semantic_identity(app, mode)})
    state = (_state(decode_cursor(cursor, 'carr-search-page/1', scope)) if cursor is not None
             else dict(snapshot='', catalog=None, source=None, match=None, rank=0,
                       last=None, number=1, seen=0, scanned=0))
    with app._search_read(workspace, operation.remaining(), operation.stopped) as (db, budget):
        if authorized_topic is not None:
            TopicTree(db, workspace)._visible(authorized_topic)
        catalog = Catalog(db, workspace)
        snapshot, edition_count, page_count = _fingerprint(db, catalog, filters, policy, budget)
        if cursor is not None and state['snapshot'] != snapshot:
            raise ValueError('The source catalog or active revisions changed. Start a new search.')
        if state['scanned'] > page_count:
            raise ValueError('Search continuation exceeds its source scope.')
        state['snapshot'] = snapshot
        allowed, source_rows, complete = _source_batch(db, catalog, filters, policy, budget, state)
        hits = []
        batch_done = True
        if source_rows:
            page_ids = tuple(row[0] for row in source_rows)
            by_page = {row[0]: row for row in source_rows}
            if mode in ('word', 'phrase', 'terms', 'advanced'):
                if state['rank']:
                    raise ValueError('Invalid literal continuation rank.')
                if state['match'] is not None and state['match'][2] not in by_page:
                    raise ValueError('The match continuation is outside its source batch.')
                hits = literal.search(db, workspace, query, mode=mode,
                    edition_ids=tuple(allowed), page_ids=page_ids, after=state['match'],
                    limit=limit, timeout=budget.remaining(), cancelled=budget.stopped)
                batch_done = len(hits) < limit
                if hits:
                    last = hits[-1]
                    row = by_page[last['page_id']]
                    state['match'] = (row[1], row[2], row[0], last['start'], last['end'])
                    if not batch_done:
                        # A bounded one-match lookahead avoids an empty page at
                        # an exact boundary without collecting more results.
                        following = literal.search(db, workspace, query, mode=mode,
                            edition_ids=tuple(allowed), page_ids=page_ids, after=state['match'],
                            limit=1, timeout=budget.remaining(), cancelled=budget.stopped)
                        batch_done = not following
            else:
                if state['match'] is not None:
                    raise ValueError('Invalid ranked continuation locator.')
                if mode == 'semantic' and app.semantic_ranker is not None:
                    from ..semantic import search_snapshot
                    ranked = search_snapshot(db, workspace, allowed, query, app.semantic_ranker,
                        limit=100, page_ids=page_ids, timeout=budget.remaining(),
                        cancelled=budget.stopped, on_status=on_status, cache=app.semantic_cache)
                else:
                    if mode == 'semantic' and on_status is not None:
                        on_status('Local model unavailable; keyword search used.')
                        budget.check()
                    ranked = thematic.search_active(db, workspace, allowed, query, limit=100,
                        page_ids=page_ids, timeout=budget.remaining(), cancelled=budget.stopped)
                if state['rank'] > len(ranked):
                    raise ValueError('The ranked continuation is outside its source batch.')
                hits = ranked[state['rank']:state['rank'] + limit]
                state['rank'] += len(hits)
                batch_done = state['rank'] >= len(ranked)
        results, selections, last_span = app._search_hits(db, workspace, query, hits,
            collection=collection, mode=mode, result_mode=result_mode, budget=budget,
            previous_span=state['last'])
        examined = min(page_count, state['scanned'] + len(source_rows))
        first, last = state['seen'] + 1, state['seen'] + len(results)
        page_number = state['number']
        state.update(last=last_span, seen=last, number=page_number + 1)
        if source_rows and batch_done:
            row = source_rows[-1]
            state.update(source=(row[1], row[2], row[0]), match=None, rank=0, scanned=examined)
        complete = complete or (batch_done and state['scanned'] >= page_count)
        next_cursor = None if complete else encode_cursor('carr-search-page/1', scope, state)
        page = SearchPage(tuple(results), next_cursor, page_number,
            first if results else last, last, examined, page_count, edition_count, snapshot)
        budget.check()
    # Retire only this query/scope's completed page. Other library/query and
    # desktop/context capabilities keep their existing bounded lifetime.
    groups = dict(getattr(app, '_page_selection_refs', {}))
    for ref in groups.pop(scope, ()):
        app._selections.pop(ref, None)
    app._selections.update(selections)
    if selections:
        groups[scope] = tuple(selections)
    while len(app._selections) > 2000:
        del app._selections[next(iter(app._selections))]
    # Group metadata is bounded by the same live-handle cap, not search count.
    retained = {}
    for key, refs in groups.items():
        current = tuple(ref for ref in refs if ref in app._selections)
        if current:
            retained[key] = current
    app._page_selection_refs = retained
    return page
