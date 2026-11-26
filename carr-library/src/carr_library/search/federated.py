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
from pathlib import Path
from threading import Event, Lock, RLock
import unicodedata
from uuid import uuid4

from ..application import LibraryApp, SearchResult, _SearchBudget
from ..catalog.membership import _identity, _metadata
from .literal import MAX_QUERY


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
               limit_per_library=50, timeout=60.0, cancelled=lambda: False, on_status=None):
        """Replace current only after every selected library succeeds.

        collections optionally maps selected workspace IDs to their own active
        collection IDs. Metadata filters use LibraryApp's existing semantics.
        The total cooperative timeout is 0 < timeout <= 60 seconds, with at most
        100 results per library and 20 distinct explicitly selected libraries.
        A failed/cancelled attempt preserves current; clear() is explicit.
        Status callbacks receive one string labelled with library name and ID.
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
            if mode not in ('phrase', 'word', 'terms', 'thematic', 'semantic'):
                raise ValueError('Unknown search mode.')
            if result_mode not in (None, 'short', 'thematic'):
                raise ValueError('Choose Short quotations or Thematic excerpts.')
            if type(limit_per_library) is not int or not 1 <= limit_per_library <= 100:
                raise ValueError('Choose between 1 and 100 results per library.')
            if on_status is not None and not callable(on_status):
                raise ValueError('Search status callback must be callable.')
            if (not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY
                    or any(unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\n\r'
                           for c in query)):
                raise ValueError(f'Enter a nonblank search query of at most {MAX_QUERY} characters.')
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
            groups, owned_hits = [], {}
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

                rows = tuple(working.search(workspace, query, mode=mode,
                    result_mode=result_mode, collection=scopes.get(workspace), **filters,
                    limit=limit_per_library, timeout=budget.remaining(),
                    cancelled=budget.stopped, on_status=report))
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

            session = FederatedSession(session_id, self._identity[0], query, mode, result_mode,
                tuple(filters.items()), tuple((w, scopes.get(w)) for w in selected),
                limit_per_library, tuple(groups))
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
