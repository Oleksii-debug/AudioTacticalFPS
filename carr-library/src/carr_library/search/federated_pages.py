"""Portable metadata continuation over explicitly selected library searches."""
from base64 import b64decode, urlsafe_b64encode
from collections.abc import Mapping
from copy import copy
from dataclasses import dataclass
import json
from uuid import uuid4

from ..application import SearchResult, _SearchBudget
from ..catalog.membership import _identity, _metadata
from .pagination import MAX_CURSOR_BYTES, query_binding
from .federated import FederatedHit, LibraryResults, FederatedSession, _Completed


MAX_SELECTED_LIBRARIES = 10_000
MAX_LIBRARIES_PER_PAGE = 20
SCHEMA = 'carr-federated-search-cursor/1'


@dataclass(frozen=True)
class FederatedPage:
    session: FederatedSession
    next_cursor: str | None
    scanned_libraries: int
    scanned_segments: int
    selected_libraries: int

    @property
    def complete(self):
        return self.next_cursor is None


def _cursor(value, binding, count):
    if value is None:
        return 0, None
    try:
        if not isinstance(value, str) or not 0 < len(value) <= MAX_CURSOR_BYTES * 4:
            raise ValueError('size')
        raw = b64decode(value.encode('ascii'), altchars=b'-_', validate=True)
        if len(raw) > MAX_CURSOR_BYTES * 2:
            raise ValueError('size')
        def pairs(items):
            row = {}
            for key, child in items:
                if key in row:
                    raise ValueError('duplicate')
                row[key] = child
            return row
        row = json.loads(raw, object_pairs_hook=pairs)
        if (type(row) is not dict or set(row) != {'schema', 'binding', 'index', 'source'}
                or row['schema'] != SCHEMA or row['binding'] != binding
                or type(row['index']) is not int or not 0 <= row['index'] < count
                or (row['source'] is not None and
                    (not isinstance(row['source'], str) or len(row['source']) > MAX_CURSOR_BYTES * 2))):
            raise ValueError('scope')
        return row['index'], row['source']
    except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError('Selected libraries or search options changed; start a new search.') from exc


def _encode(binding, index, source):
    raw = json.dumps(dict(schema=SCHEMA, binding=binding, index=index, source=source),
                     separators=(',', ':'), sort_keys=True).encode('utf-8')
    return urlsafe_b64encode(raw).decode('ascii')


def search_page(service, workspace_ids, query, *, cursor=None, mode='phrase', result_mode=None,
                title=None, author=None, language=None, edition=None, collections=None,
                limit_per_library=50, library_budget=20, page_budget=500, timeout=60.0,
                cancelled=lambda: False, on_status=None):
    """Publish only the completed current page; older action handles expire.

    Each library is a separate authorized snapshot, as in ordinary federation.
    Selected-library order is preserved. A partially searched library is resumed
    before moving to the next one. At most20*100 current result handles exist.
    """
    if not service._search_lock.acquire(blocking=False):
        raise RuntimeError('A selected-library search is already running.')
    try:
        with service._search_state():
            service._check_host()
            invalidation = service._invalidation
        if not callable(cancelled):
            raise ValueError('A search cancellation callback is required.')
        budget = _SearchBudget(timeout, lambda: invalidation.is_set() or cancelled(), maximum=3600.0)
        budget.check()
        if (not callable(cancelled) or (on_status is not None and not callable(on_status))
                or type(limit_per_library) is not int or not 1 <= limit_per_library <= 100
                or type(library_budget) is not int or not 1 <= library_budget <= MAX_LIBRARIES_PER_PAGE
                or type(page_budget) is not int or not 1 <= page_budget <= 20_000
                or not isinstance(workspace_ids, (tuple, list)) or not workspace_ids
                or len(workspace_ids) > MAX_SELECTED_LIBRARIES):
            raise ValueError('Choose bounded pages over explicitly selected libraries.')
        selected = []
        known = set()
        for value in workspace_ids:
            budget.check()
            value = _identity(value)
            if not value.strip():
                raise ValueError('A library identity cannot be blank.')
            if value not in known:
                selected.append(value)
                known.add(value)
        filters = dict(zip(('title', 'author', 'language', 'edition'),
                           map(_metadata, (title, author, language, edition))))
        if any(value == '' for value in filters.values()):
            raise ValueError('Empty metadata filters are not supported.')
        if collections is not None and not isinstance(collections, Mapping):
            raise ValueError('Collections must be mapped to selected library IDs.')
        scopes = dict(collections or {})
        if any(workspace not in known for workspace in scopes):
            raise ValueError('A collection belongs to an unselected library.')
        for collection in scopes.values():
            if collection is not None:
                _identity(collection)
        binding = query_binding(service._host.root, service._host.database, '', query,
            dict(workspaces=selected, filters=filters, collections=scopes,
                 mode=mode, result_mode=result_mode))
        index, source_cursor = _cursor(cursor, binding, len(selected))
        working = copy(service._host)
        working._selections = {}
        session_id = uuid4().hex
        groups, owned_hits = [], {}
        scanned = 0
        while index < len(selected) and len(groups) < library_budget:
            budget.check()
            workspace = selected[index]
            with working._search_read(workspace, budget.remaining(), budget.stopped,
                                      maximum=3600.0) as (db, _):
                name = db.execute('SELECT name FROM workspace_meta WHERE workspace=?',
                                  (workspace,)).fetchone()[0]
            def report(message):
                budget.check()
                if on_status is not None:
                    on_status(f'{name} [{workspace}]: {message}')
                budget.check()
            page = working.search_page(workspace, query, cursor=source_cursor, mode=mode,
                result_mode=result_mode, collection=scopes.get(workspace), **filters,
                limit=limit_per_library, page_budget=page_budget, timeout=budget.remaining(),
                cancelled=budget.stopped, on_status=report)
            budget.check()
            rows = page.results
            if len(rows) > limit_per_library:
                raise ValueError('A library exceeded its result-page limit.')
            hits = []
            for row in rows:
                budget.check()
                if (not isinstance(row, SearchResult) or row.workspace != workspace
                        or row.ref in owned_hits or row.ref not in working._selections):
                    raise ValueError('A search result is outside its selected scope.')
                hit = FederatedHit(session_id, name, row)
                owned_hits[row.ref] = hit
                hits.append(hit)
            groups.append(LibraryResults(workspace, name, tuple(hits)))
            scanned += page.scanned_segments
            source_cursor = page.next_cursor
            if source_cursor is not None:
                break
            index += 1
        continuation = _encode(binding, index, source_cursor) if index < len(selected) else None
        session = FederatedSession(session_id, service._identity[0], query, mode, result_mode,
            tuple(filters.items()), tuple((w, scopes.get(w)) for w in selected),
            limit_per_library, tuple(groups))
        completed = _Completed(session, working, owned_hits)
        with service._search_state():
            budget.check()
            service._check_host()
            if invalidation is not service._invalidation:
                raise TimeoutError('The search was cleared before publication.')
            service._completed = completed
        return FederatedPage(session, continuation, len(groups), scanned, len(selected))
    finally:
        service._search_lock.release()
