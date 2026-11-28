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
from threading import Event, Lock, RLock
import unicodedata
from uuid import uuid4

from ..application import LibraryApp, SearchResult, _SearchBudget
from ..catalog.membership import _identity, _metadata
from .literal import MAX_QUERY
from .paging import CursorOwner, SearchResources, LIBRARIES_PER_PAGE, MAX_SELECTED_SCOPE_BYTES


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
    selected_workspaces: tuple = ()
    library_offset: int = 0
    has_more_libraries: bool = False
    next_source_cursors: tuple = ()
    source_windows: tuple = ()
    source_cursors: tuple = ()
    ranking_scope: str = 'window'

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
    return app.root, app.database, app.assets_dir, app.assets.root


def _workspaces(values, check, *, maximum_bytes=MAX_SELECTED_SCOPE_BYTES):
    if not isinstance(values, (tuple, list)) or not values:
        raise ValueError('Explicitly select at least one library.')
    selected, size = {}, 0
    for value in values:
        check()
        value = _identity(value)
        if not value.strip():
            raise ValueError('A library identity cannot be blank.')
        if value not in selected:
            size += len(value.encode('utf-8')) + 128
            if size > maximum_bytes:
                raise ValueError('The selected library identities exceed the bounded navigation memory budget.')
            selected[value] = None
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
        self._cursor_owner = CursorOwner(app)
        self._retired_cursor_owners = []

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
            if not self._cursor_owner.close():
                self._retired_cursor_owners.append(self._cursor_owner)
            self._cursor_owner = CursorOwner(self._host)

    def _check_host(self):
        if _data_identity(self._host) != self._identity:
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

    def search(self, workspace_ids, query, *, mode='phrase', result_mode=None,
               title=None, author=None, language=None, edition=None, collections=None,
               limit_per_library=50, offset_per_library=0, timeout=None,
               cancelled=lambda: False, on_status=None, paged=False,
               source_cursors=None, library_offset=0, resources=None, ranking_scope="window"):
        """Replace current only after every selected library succeeds.

        collections optionally maps selected workspace IDs to their own active
        collection IDs. Metadata filters use LibraryApp's existing semantics.
        A cooperative caller budget covers the complete invocation. At most
        20 selected libraries and 100 results per library are retained per page;
        library_offset and passive source_cursors continue the remaining scope.
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
                retained = [owner for owner in self._retired_cursor_owners if not owner.close()]
                self._retired_cursor_owners = retained
                if retained:
                    raise RuntimeError("The previous search is still releasing its private results. Retry after it finishes.")
                invalidation = self._invalidation
                cursor_owner = self._cursor_owner
            if not callable(cancelled):
                raise ValueError('A search cancellation callback is required.')
            if resources is None:
                resources = SearchResources(timeout=(300.0 if paged else 60.0) if timeout is None else timeout)
            elif type(resources) is not SearchResources:
                raise ValueError('A validated search resource budget is required.')
            elif timeout is not None:
                from dataclasses import replace
                resources = replace(resources, timeout=timeout)
            budget = _SearchBudget(resources.timeout, lambda: invalidation.is_set() or cancelled())
            budget.check()
            if ranking_scope not in ("window", "all") or (ranking_scope == "all" and (not paged or mode not in ("thematic", "semantic"))):
                raise ValueError("Complete-scope ranking requires paged search.")
            if mode not in ('phrase', 'word', 'terms', 'thematic', 'semantic', 'advanced'):
                raise ValueError('Unknown search mode.')
            if result_mode not in (None, 'short', 'thematic'):
                raise ValueError('Choose Short quotations or Thematic excerpts.')
            if type(limit_per_library) is not int or not 1 <= limit_per_library <= 100:
                raise ValueError('Choose between 1 and 100 results per library.')
            if type(offset_per_library) is not int or offset_per_library < 0:
                raise ValueError('Choose a nonnegative integer result-page offset.')
            if offset_per_library and mode not in ('terms', 'advanced') and not paged:
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
            selected = _workspaces(workspace_ids, budget.check,
                                   maximum_bytes=resources.memory_bytes // 8)
            if (type(paged) is not bool or type(library_offset) is not int
                    or not 0 <= library_offset < len(selected)):
                raise ValueError('Choose an existing selected-library navigation page.')
            if source_cursors is not None and (not paged or not isinstance(source_cursors, Mapping)
                    or any(key not in selected for key in source_cursors)):
                raise ValueError('Source cursors must belong to explicitly selected libraries.')
            cursors = dict(source_cursors or {})
            if not paged and (len(selected) > LIBRARIES_PER_PAGE or library_offset):
                raise ValueError("Use paged=True to continue beyond the legacy 20-library request window.")
            page_selected = selected[library_offset:library_offset + LIBRARIES_PER_PAGE]
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
            # Only this library page can own action handles; continuations
            # replace it rather than expanding the existing 2000-handle budget.
            working = copy(self._host)
            working._selections = {}
            working._search_cursor_owner = cursor_owner
            session_id = uuid4().hex
            groups, owned_hits, has_more = [], {}, False
            next_sources, source_windows, current_sources = [], [], []
            for workspace in page_selected:
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
                if paged:
                    if workspace in cursors and cursors[workspace] is None:
                        rows = ()
                        current_sources.append((workspace, None))
                        next_sources.append((workspace, None))
                        source_windows.append((workspace, 0, 0, 0))
                    else:
                        page = working.search_page(workspace, query, cursor=cursors.get(workspace),
                            **options, timeout=budget.remaining(), resources=resources, ranking_scope=ranking_scope)
                        current_sources.append((workspace, page.cursor))
                        rows = _bounded_rows(page.results, limit_per_library, budget.check)
                        has_more = has_more or page.has_more_results
                        next_sources.append((workspace, page.next_source_cursor))
                        source_windows.append((workspace, page.source_pages,
                                               page.source_editions, page.source_characters))
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
                if not paged and mode in ('terms', 'advanced') and len(rows) == limit_per_library:
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

            session = FederatedSession(session_id, self._identity[0], query, mode, result_mode,
                tuple(filters.items()), tuple((w, scopes.get(w)) for w in selected),
                limit_per_library, tuple(groups), offset_per_library, has_more,
                selected, library_offset, library_offset + len(page_selected) < len(selected),
                tuple(next_sources), tuple(source_windows), tuple(current_sources), ranking_scope)
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
