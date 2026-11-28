"""Bounded continuation over explicit libraries using the host's opaque table.

The service keeps only the latest result page and reauthorizes each visited
library through LibraryApp. This is neither a simultaneous multi-library
snapshot nor global ranking. A source window with no matches can still expose
a next page. Cursor positions are never decoded from caller-controlled JSON.
"""
from collections.abc import Mapping
from copy import copy
from dataclasses import dataclass
from hashlib import sha256
import json
import unicodedata
from uuid import uuid4

from ..application import SearchResult, _SearchBudget
from ..catalog.membership import _identity, _metadata
from .federated import FederatedHit, LibraryResults, FederatedSession, _Completed
from .literal import MAX_QUERY
from .pagination import Continuation
from .source_windows import DEFAULT_PAGE_SOURCES, MAX_PAGE_CHARACTERS, validate_budgets

MAX_SELECTED_LIBRARIES = 10_000
MAX_LIBRARIES_PER_PAGE = 20
MAX_REQUEST_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class FederatedPage:
    session: FederatedSession
    next_cursor: str | None
    scanned_libraries: int
    scanned_segments: int
    selected_libraries: int
    ordering: str

    @property
    def complete(self):
        return self.next_cursor is None

    @property
    def hits(self):
        return self.session.hits

    @property
    def groups(self):
        return self.session.groups


def _selection(values, check):
    if (not isinstance(values, (tuple, list)) or not values
            or len(values) > MAX_SELECTED_LIBRARIES):
        raise ValueError('Explicitly select between 1 and 10000 libraries for paged search.')
    selected = {}
    for value in values:
        check()
        value = _identity(value)
        if not value.strip():
            raise ValueError('A library identity cannot be blank.')
        selected.setdefault(value, None)
    return tuple(selected)


def _request(identity, selected, query, mode, result_mode, filters, scopes,
             limit, library_budget, page_budget, character_budget):
    value = {
        'data': tuple(str(item) for item in identity), 'workspaces': selected,
        'query': query, 'mode': mode, 'result_mode': result_mode,
        'filters': filters, 'collections': tuple((w, scopes.get(w)) for w in selected),
        'limit': limit, 'library_budget': library_budget,
        'page_budget': page_budget, 'character_budget': character_budget,
    }
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True,
                         separators=(',', ':'), allow_nan=False).encode('ascii')
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError('The explicit paged search request exceeds its metadata allowance.')
    return sha256(encoded).hexdigest()


def search_page(service, workspace_ids, query, *, mode='phrase', result_mode=None,
                title=None, author=None, language=None, edition=None, collections=None,
                limit_per_library=50, library_budget=MAX_LIBRARIES_PER_PAGE,
                page_budget=DEFAULT_PAGE_SOURCES, character_budget=MAX_PAGE_CHARACTERS,
                cursor=None, timeout=60.0, cancelled=lambda: False, on_status=None):
    """Publish at most 20 x 100 fresh result handles after the complete page."""
    if not service._search_lock.acquire(blocking=False):
        raise RuntimeError('A selected-library search is already running.')
    try:
        with service._search_state():
            service._check_host()
            invalidation, previous = service._invalidation, service._completed
        if not callable(cancelled):
            raise ValueError('A search cancellation callback is required.')
        budget = _SearchBudget(timeout, lambda: invalidation.is_set() or cancelled())
        budget.check()
        if mode not in ('phrase', 'word', 'terms', 'advanced', 'thematic', 'semantic'):
            raise ValueError('Unknown search mode.')
        if result_mode not in (None, 'short', 'thematic'):
            raise ValueError('Choose Short quotations or Thematic excerpts.')
        if type(limit_per_library) is not int or not 1 <= limit_per_library <= 100:
            raise ValueError('Choose between 1 and 100 results per library.')
        if type(library_budget) is not int or not 1 <= library_budget <= MAX_LIBRARIES_PER_PAGE:
            raise ValueError('Inspect between 1 and 20 libraries per page.')
        validate_budgets(page_budget, character_budget)
        if on_status is not None and not callable(on_status):
            raise ValueError('Search status callback must be callable.')
        if (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY
                or any(unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\n\r' for c in query)):
            raise ValueError(f'Enter a nonblank search query of at most {MAX_QUERY} characters.')
        selected = _selection(workspace_ids, budget.check)
        filters = dict(zip(('title', 'author', 'language', 'edition'),
                           map(_metadata, (title, author, language, edition))))
        if any(value == '' for value in filters.values()):
            raise ValueError('Empty metadata filters are not supported.')
        if collections is not None and not isinstance(collections, Mapping):
            raise ValueError('Collections must be mapped to selected library IDs.')
        scopes = dict(collections or {})
        if any(workspace not in selected for workspace in scopes):
            raise ValueError('A collection scope belongs to an unselected library.')
        for collection in scopes.values():
            if collection is not None:
                _identity(collection)

        request = _request(service._identity, selected, query, mode, result_mode, filters,
                           scopes, limit_per_library, library_budget, page_budget, character_budget)
        if cursor is None:
            base = service._host
            continuation = Continuation(request, kind='federated')
            index, source_cursor = 0, None
        else:
            if previous is None:
                raise ValueError('The paged search continuation expired. Start a new search.')
            base = previous.app
            continuation = base._search_cursors.begin(request, cursor)
            if continuation.kind != 'federated':
                raise ValueError('This continuation belongs to another search route.')
            position = continuation.after
            if (type(position) is not tuple or len(position) != 2
                    or type(position[0]) is not int or not 0 <= position[0] < len(selected)
                    or position[1] is not None and type(position[1]) is not str):
                raise ValueError('Invalid internal selected-library position.')
            index, source_cursor = position

        # Reuse the existing per-app table, isolated just like candidate refs.
        # A later failure publishes neither newly allocated tokens nor results.
        working = copy(base)
        working._selections = {}
        working._search_cursors = base._search_cursors.fork()
        session_id = uuid4().hex
        groups, owned_hits, scanned, visited = [], {}, 0, 0
        while index < len(selected) and visited < library_budget:
            budget.check()
            workspace = selected[index]
            with working._search_read(workspace, budget.remaining(), budget.stopped) as (db, _):
                name = db.execute('SELECT name FROM workspace_meta WHERE workspace=?',
                                  (workspace,)).fetchone()[0]

            def report(message, workspace=workspace, name=name):
                budget.check()
                if on_status is not None:
                    on_status(f'{name} [{workspace}]: {message}')
                budget.check()

            page = working.search_page(workspace, query, mode=mode, result_mode=result_mode,
                collection=scopes.get(workspace), **filters, limit=limit_per_library,
                page_budget=page_budget, character_budget=character_budget, cursor=source_cursor,
                timeout=budget.remaining(), cancelled=budget.stopped, on_status=report)
            budget.check()
            if len(page) > limit_per_library:
                raise ValueError('The library search exceeded its result limit.')
            hits = []
            for result in page:
                budget.check()
                if (not isinstance(result, SearchResult) or result.workspace != workspace
                        or result.ref in owned_hits or result.ref not in working._selections):
                    raise ValueError('The search returned a result outside its selected scope.')
                hit = FederatedHit(session_id, name, result)
                owned_hits[result.ref] = hit
                hits.append(hit)
            groups.append(LibraryResults(workspace, name, tuple(hits)))
            visited += 1
            scanned += page.scanned_segments
            source_cursor = page.next_cursor
            if source_cursor is not None:
                break
            index += 1

        session = FederatedSession(session_id, service._identity[0], query, mode, result_mode,
            tuple(filters.items()), tuple((w, scopes.get(w)) for w in selected),
            limit_per_library, tuple(groups))
        next_cursor, prepared = None, None
        if index < len(selected):
            next_cursor, prepared = working._search_cursors.stage(
                Continuation(request, after=(index, source_cursor), kind='federated'))
        ordering = ('Libraries retain the explicit selection order; source windows retain source order. '
                    'Relevance is local to each ranked window, never across libraries.')
        answer = FederatedPage(session, next_cursor, visited, scanned, len(selected), ordering)
        completed = _Completed(session, working, owned_hits)
        with service._search_state():
            budget.check()
            service._check_host()
            if invalidation is not service._invalidation:
                raise TimeoutError('The search session was cleared before publication.')
            working._search_cursors.publish(prepared)
            service._completed = completed
        return answer
    finally:
        service._search_lock.release()
