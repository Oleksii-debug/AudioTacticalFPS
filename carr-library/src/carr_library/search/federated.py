"""Explicit selected-library search composed from the existing local host.

Each library search and name lookup opens a fresh authorized SQLite snapshot.
This is not a simultaneous snapshot of all libraries, and groups retain the
selection order rather than claiming a global relevance ranking. Names are
display labels; immutable workspace IDs remain the authority for every action.

An isolated shallow host copy owns each candidate session's selections. A
failure never publishes partial results, modifies the real host's selections,
or replaces the previous completed session. No database is initialized here.
Cancellation/deadlines are cooperative, including the existing host's source
verification and SQLite progress checks; blocking native calls are not preempted.
"""
from collections.abc import Mapping
from contextlib import contextmanager
from copy import copy
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
import stat
from threading import Event, Lock, RLock
import unicodedata
from uuid import uuid4

from ..application import LibraryApp, SearchResult, _SearchBudget
from ..catalog.membership import _identity, _metadata
from .literal import MAX_QUERY
from .catalog_pages import SearchCursor, _digest


MAX_SELECTED_LIBRARIES = 10000


@dataclass(frozen=True)
class FederatedCursor:
    """In-memory continuation only; every page opens fresh authorized scopes."""
    criteria: str
    library_start: int
    cursors: tuple[tuple[str, SearchCursor | None], ...] = ()


@dataclass(frozen=True)
class _CatalogRequest:
    selected: tuple[str, ...]
    criteria: str
    start: int
    page_size: int
    edition_limit: int
    cursor: FederatedCursor | None


@dataclass(frozen=True)
class FederatedHit:
    session_id: str
    library_name: str
    result: SearchResult

    @property
    def workspace(self):
        return self.result.workspace


@dataclass(frozen=True)
class LibraryResults:
    workspace: str
    library_name: str
    hits: tuple[FederatedHit, ...]


@dataclass(frozen=True)
class FederatedSession:
    id: str
    data_root: Path
    query: str
    mode: str
    result_mode: str | None
    filters: tuple[tuple[str, str | None], ...]
    collections: tuple[tuple[str, str | None], ...]
    limit_per_library: int
    groups: tuple[LibraryResults, ...]
    offset_per_library: int = 0
    has_more: bool = False
    selected_workspaces: tuple[str, ...] = ()
    next_cursor: FederatedCursor | None = None
    library_start: int = 0
    library_end: int = 0
    source_ranges: tuple[tuple[str, int, int, int], ...] = ()

    @property
    def hits(self):
        return tuple(hit for group in self.groups for hit in group.hits)

    @property
    def workspaces(self):
        return tuple(group.workspace for group in self.groups)


@dataclass(frozen=True)
class _Completed:
    session: FederatedSession
    app: LibraryApp
    hits: dict[str, FederatedHit]


def _data_identity(app):
    paths = (app.root, app.database, app.assets_dir, app.assets.root)
    identities = []
    for path in paths:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0)
                & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0)):
            raise ValueError('The library data directory changed. Open a new search session.')
        identities.append((info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode)))
    return (*paths, tuple(identities))


def _workspaces(values, check):
    if not isinstance(values, (tuple, list)) or not values:
        raise ValueError('Explicitly select between 1 and 20 libraries.')
    selected = {}
    for value in values:
        check()
        value = _identity(value)
        if not value.strip():
            raise ValueError('A library identity cannot be blank.')
        selected.setdefault(value, None)
        if len(selected) > 20:
            raise ValueError('Select at most 20 distinct libraries.')
    return tuple(selected)


def _bounded_rows(rows, limit, check):
    """Inspect at most one excess row, including an accidental lazy response."""
    collected = []
    for row in islice(rows, limit + 1):
        check()
        collected.append(row)
    return tuple(collected)


class FederatedSearch:
    """One replaceable search session over an explicitly selected local scope.

    The argument is the trusted, already initialized LibraryApp. Public action
    methods accept an exact FederatedHit returned by the latest completed
    session, never free-form workspace/ref/revision fields from the UI. Sessions
    and action handles are in-memory only and are not portable to another
    controller. Explicit clear() invalidates them and cancels pending publication.
    """
    def __init__(self, app: LibraryApp):
        if not isinstance(app, LibraryApp):
            raise TypeError('An initialized LibraryApp is required.')
        self._host = app
        self._identity = _data_identity(app)
        self._completed = None
        self._invalidation = Event()
        self._state_lock = RLock()
        self._search_lock = Lock()

    @property
    def current(self):
        with self._state_lock:
            return self._completed.session if self._completed is not None else None

    def clear(self):
        """Explicit user scope/edit invalidation, including an in-flight search."""
        with self._state_lock:
            self._invalidation.set()
            self._invalidation = Event()
            self._completed = None

    def _check_host(self):
        try:
            changed = _data_identity(self._host) != self._identity
        except OSError:
            changed = True
        if changed:
            raise ValueError('The library data directory changed. Open a new search session.')

    @contextmanager
    def _search_state(self):
        # Source actions serialize session ownership through commit. A search
        # must not wait behind an action outside its cooperative time allowance.
        if not self._state_lock.acquire(blocking=False):
            raise RuntimeError('A selected-library action is running. Retry after it completes.')
        try:
            yield
        finally:
            self._state_lock.release()

    def search_page(self, workspace_ids, query, *, cursor=None, library_page_size=20,
                    edition_limit=500, **options):
        """Visit every explicitly selected library through bounded source pages.

        Each request admits at most 20 libraries, 500 editions per library and
        100 results per library. Rankings apply to each source window only.
        Returned handles replace the previous page only after all work succeeds.
        """
        if 'offset_per_library' in options or '_catalog_request' in options:
            raise ValueError('Continue using the returned selected-library cursor.')
        accepted = {'mode', 'result_mode', 'title', 'author', 'language', 'edition',
                    'collections', 'limit_per_library', 'timeout', 'cancelled', 'on_status'}
        if set(options) - accepted:
            raise ValueError('Unknown selected-library search option.')
        stopped = options.get('cancelled', lambda: False)
        if not callable(stopped):
            raise ValueError('A search cancellation callback is required.')
        budget = _SearchBudget(options.get('timeout', 60.0), stopped)
        budget.check()
        if (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY
                or any(unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\n\r' for c in query)):
            raise ValueError('Enter a nonblank, bounded search query.')
        if options.get('mode', 'phrase') not in ('phrase', 'word', 'terms', 'thematic', 'semantic', 'advanced'):
            raise ValueError('Unknown search mode.')
        if options.get('result_mode') not in (None, 'short', 'thematic'):
            raise ValueError('Choose Short quotations or Thematic excerpts.')
        limit = options.get('limit_per_library', 50)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Choose between 1 and 100 results per library.')
        for key in ('title', 'author', 'language', 'edition'):
            if key in options:
                options[key] = _metadata(options[key])
        if (not isinstance(workspace_ids, (tuple, list)) or not workspace_ids
                or len(workspace_ids) > MAX_SELECTED_LIBRARIES):
            raise ValueError('Explicitly select between 1 and 10000 libraries.')
        if type(library_page_size) is not int or not 1 <= library_page_size <= 20:
            raise ValueError('Choose between 1 and 20 libraries per page.')
        if type(edition_limit) is not int or not 1 <= edition_limit <= 500:
            raise ValueError('Choose between 1 and 500 source editions per library page.')
        values = []
        for value in workspace_ids:
            budget.check()
            values.append(_identity(value))
        selected = tuple(dict.fromkeys(values))
        if any(not value.strip() for value in selected):
            raise ValueError('A library identity cannot be blank.')
        scopes = options.get('collections')
        if scopes is not None and (not isinstance(scopes, Mapping)
                or any(workspace not in selected for workspace in scopes)):
            raise ValueError('A collection scope belongs to an unselected library.')
        if scopes is not None:
            scopes = {workspace: _identity(value) if value is not None else None
                      for workspace, value in scopes.items()}
            options['collections'] = scopes
        bound = {key: value for key, value in options.items()
                 if key not in ('timeout', 'cancelled', 'on_status')}
        criteria = _digest(dict(options=bound, query=query, selected=selected,
            library_page_size=library_page_size, edition_limit=edition_limit,
            root=[str(path) for path in self._identity]))
        start = 0
        if cursor is not None:
            if (type(cursor) is not FederatedCursor or cursor.criteria != criteria
                    or type(cursor.library_start) is not int
                    or not 0 <= cursor.library_start < len(selected)
                    or cursor.library_start % library_page_size):
                raise ValueError('The search continuation belongs to different selected libraries or options.')
            start = cursor.library_start
        window = selected[start:start + library_page_size]
        if cursor is not None and cursor.cursors:
            if (type(cursor.cursors) is not tuple or len(cursor.cursors) != len(window)
                    or any(type(row) is not tuple or len(row) != 2 for row in cursor.cursors)
                    or tuple(row[0] for row in cursor.cursors) != window
                    or any(row[1] is not None and type(row[1]) is not SearchCursor for row in cursor.cursors)):
                raise ValueError('Invalid selected-library source continuation.')
        request = _CatalogRequest(selected, criteria, start, library_page_size, edition_limit, cursor)
        if scopes is not None:
            options['collections'] = {workspace: scopes.get(workspace) for workspace in window}
        options['timeout'] = budget.remaining()
        return self.search(window, query, _catalog_request=request, **options)

    def search(self, workspace_ids, query, *, mode='phrase', result_mode=None,
               title=None, author=None, language=None, edition=None, collections=None,
               limit_per_library=50, offset_per_library=0, timeout=60.0,
               cancelled=lambda: False, on_status=None, _catalog_request=None):
        """Replace current only after every selected library succeeds.

        collections optionally maps selected workspace IDs to their own active
        collection IDs. Metadata filters use LibraryApp's existing semantics.
        The total cooperative timeout is 0 < timeout <= 60 seconds, with at most
        100 results per library and 20 distinct explicitly selected libraries.
        A failed/cancelled attempt preserves current; clear() is explicit.
        Status callbacks receive one string labelled with library name and ID.
        Terms/advanced modes permit bounded result pages because they produce
        one hit per source page. Every request rechecks the live source scope;
        ordering is stable when that scope is unchanged, not a frozen snapshot.
        A one-result lookahead establishes has_more within the same time budget.
        """
        if not self._search_lock.acquire(blocking=False):
            raise RuntimeError('A selected-library search is already running.')
        try:
            with self._search_state():
                self._check_host()
                invalidation = self._invalidation
            if not callable(cancelled):
                raise ValueError('A search cancellation callback is required.')
            budget = _SearchBudget(timeout, lambda: invalidation.is_set() or cancelled())
            budget.check()
            if mode not in ('phrase', 'word', 'terms', 'thematic', 'semantic', 'advanced'):
                raise ValueError('Unknown search mode.')
            if result_mode not in (None, 'short', 'thematic'):
                raise ValueError('Choose Short quotations or Thematic excerpts.')
            if type(limit_per_library) is not int or not 1 <= limit_per_library <= 100:
                raise ValueError('Choose between 1 and 100 results per library.')
            if type(offset_per_library) is not int or offset_per_library < 0:
                raise ValueError('Choose a nonnegative integer result-page offset.')
            if offset_per_library and mode not in ('terms', 'advanced'):
                raise ValueError('Result-page navigation is available for All words and Advanced searches.')
            if on_status is not None and not callable(on_status):
                raise ValueError('Search status callback must be callable.')
            if (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY
                    or any(unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\n\r'
                           for c in query)):
                raise ValueError(f'Enter a nonblank search query of at most {MAX_QUERY} characters.')
            if mode == 'advanced':
                # Validate before opening any selected library. The exact same
                # parser then verifies each page in the existing literal engine;
                # a failed query never replaces a completed federated session.
                from .advanced import compile_query
                compile_query(query, check=budget.check)
            selected = _workspaces(workspace_ids, budget.check)
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

            # copy.copy does not invoke LibraryApp.__init__ or open/migrate a DB.
            # At most 20 * 100 refs fit the host's existing 2000-selection budget.
            working = copy(self._host)
            working._selections = {}
            session_id = uuid4().hex
            groups, owned_hits, has_more = [], {}, False
            source_cursors, source_ranges = [], []
            continued = (dict(_catalog_request.cursor.cursors)
                if _catalog_request is not None and _catalog_request.cursor is not None
                and _catalog_request.cursor.cursors else None)
            for workspace in selected:
                budget.check()
                with working._search_read(workspace, budget.remaining(), budget.stopped) as (db, _):
                    name = db.execute('SELECT name FROM workspace_meta WHERE workspace=?',
                                      (workspace,)).fetchone()[0]

                def report(message, workspace=workspace, name=name):
                    budget.check()
                    if on_status is not None:
                        on_status(f'{name} [{workspace}]: {message}')
                    budget.check()

                options = dict(mode=mode, result_mode=result_mode,
                    collection=scopes.get(workspace), **filters,
                    limit=limit_per_library, cancelled=budget.stopped, on_status=report)
                if offset_per_library:
                    options['offset'] = offset_per_library
                if _catalog_request is not None:
                    if continued is not None and continued[workspace] is None:
                        rows = ()
                        source_cursors.append((workspace, None))
                        source_ranges.append((workspace, 0, 0, 0))
                    else:
                        page = working.search_page(workspace, query, **options,
                            cursor=continued[workspace] if continued is not None else None,
                            edition_limit=_catalog_request.edition_limit, timeout=budget.remaining())
                        rows = _bounded_rows(page.results, limit_per_library, budget.check)
                        source_cursors.append((workspace, page.next_cursor))
                        source_ranges.append((workspace, page.edition_start, page.edition_end, page.result_offset))
                else:
                    rows = _bounded_rows(working.search(workspace, query, **options,
                        timeout=budget.remaining()), limit_per_library, budget.check)
                budget.check()
                if len(rows) > limit_per_library:
                    raise ValueError('The library search exceeded its result limit.')
                hits = []
                for result in rows:
                    budget.check()
                    if (not isinstance(result, SearchResult) or result.workspace != workspace
                            or result.ref in owned_hits or result.ref not in working._selections):
                        raise ValueError('The search returned a result outside its selected scope.')
                    hit = FederatedHit(session_id, name, result)
                    owned_hits[result.ref] = hit
                    hits.append(hit)
                groups.append(LibraryResults(workspace, name, tuple(hits)))
                if _catalog_request is None and mode in ('terms', 'advanced') and len(rows) == limit_per_library:
                    next_offset = offset_per_library + limit_per_library
                    # Lookahead must not evict any published page handle from
                    # the existing 2000-selection host budget (20 * 100 hits).
                    probe = copy(working)
                    probe._selections = {}
                    options.update(limit=1, offset=next_offset)
                    more = _bounded_rows(probe.search(workspace, query, **options,
                        timeout=budget.remaining()), 1, budget.check)
                    budget.check()
                    if len(more) > 1 or any(not isinstance(hit, SearchResult)
                            or hit.workspace != workspace or hit.ref not in probe._selections
                            for hit in more):
                        raise ValueError('The search returned a lookahead outside its selected scope.')
                    has_more = has_more or bool(more)

            continuation = None
            if _catalog_request is not None:
                request = _catalog_request
                if any(cursor is not None for _, cursor in source_cursors):
                    continuation = FederatedCursor(request.criteria, request.start, tuple(source_cursors))
                elif request.start + len(selected) < len(request.selected):
                    continuation = FederatedCursor(request.criteria, request.start + len(selected))
                has_more = continuation is not None
            session = FederatedSession(session_id, self._identity[0], query, mode, result_mode,
                tuple(filters.items()), tuple((w, scopes.get(w)) for w in selected),
                limit_per_library, tuple(groups), offset_per_library, has_more,
                _catalog_request.selected if _catalog_request is not None else (),
                continuation, _catalog_request.start + 1 if _catalog_request is not None else 0,
                _catalog_request.start + len(selected) if _catalog_request is not None else 0,
                tuple(source_ranges))
            completed = _Completed(session, working, owned_hits)
            with self._search_state():
                budget.check()
                self._check_host()
                if invalidation is not self._invalidation:
                    raise TimeoutError('The search session was cleared before publication.')
                self._completed = completed
            return session
        finally:
            self._search_lock.release()

    def _selected(self, hit, *, verified=False):
        self._check_host()
        current = self._completed
        if (not isinstance(hit, FederatedHit) or current is None
                or not isinstance(hit.result, SearchResult)
                or hit.session_id != current.session.id
                or current.hits.get(hit.result.ref) is not hit):
            raise ValueError('Select a result from the latest completed search session.')
        if _data_identity(current.app) != self._identity:
            raise ValueError('The search session data directory changed.')
        if verified and hit.result.verified is not True:
            raise ValueError('Unverified extraction can be previewed, not copied or saved as evidence.')
        return current.app, hit.result

    @staticmethod
    def _check_cancelled(cancelled):
        if not callable(cancelled):
            raise ValueError('An action cancellation callback is required.')
        if cancelled():
            raise InterruptedError('Selected-library action cancelled.')

    def _action(self, hit, cancelled, *, verified=False):
        app, result = self._selected(hit, verified=verified)
        if not callable(cancelled):
            raise ValueError('An action cancellation callback is required.')
        completed = self._completed
        latched = False

        def stopped():
            nonlocal latched
            if not latched:
                latched = bool(cancelled())
                # A callback can reenter clear() while this thread owns RLock.
                # Check ownership after it returns, even when it returns False.
                latched = (latched or self._completed is not completed
                           or _data_identity(self._host) != self._identity
                           or _data_identity(app) != self._identity)
            return latched

        self._check_cancelled(stopped)
        return app, result, stopped

    def context(self, hit, *, cancelled=lambda: False):
        with self._state_lock:
            app, result, stopped = self._action(hit, cancelled, verified=True)
            context = app.context(result.workspace, result.ref, result.revision, cancelled=stopped)
            self._check_cancelled(stopped)
            return context

    def preview(self, hit, *, cancelled=lambda: False):
        with self._state_lock:
            app, result, stopped = self._action(hit, cancelled)
            preview = app.preview(result.workspace, result.ref, result.revision, cancelled=stopped)
            self._check_cancelled(stopped)
            return preview

    def copy(self, hit, field='both', *, cancelled=lambda: False):
        with self._state_lock:
            app, result, stopped = self._action(hit, cancelled, verified=True)
            text = app.copy(result.workspace, result.ref, result.revision, field, cancelled=stopped)
            self._check_cancelled(stopped)
            return text

    def topics(self, hit, *, cancelled=lambda: False):
        """Fresh visible Topic objects from the selected result's own library."""
        with self._state_lock:
            app, result, stopped = self._action(hit, cancelled)
            topics = app.topics(result.workspace)
            self._check_cancelled(stopped)
            return topics

    def save_hit(self, hit, topic, note='', *, cancelled=lambda: False):
        with self._state_lock:
            app, result, stopped = self._action(hit, cancelled, verified=True)
            return app.save_hit(result.workspace, topic, result.ref, result.revision, note,
                                authorized_topic=topic, cancelled=stopped)
