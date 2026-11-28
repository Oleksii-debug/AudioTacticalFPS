"""Bounded source windows over the existing catalog and active-page snapshot.

Cursors are opaque, owner-bound navigation handles, never source authorization.
Every page resolves the current catalog/collection and exact active revisions.
The existing SQLite database is observed read-only; no second job queue is used.
"""
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import math
import sqlite3
from threading import Event, Lock, RLock
from uuid import uuid4
from weakref import WeakKeyDictionary
from hashlib import sha256
import json

from ..catalog.read_views import browse_editions

MAX_TIMEOUT_SECONDS = 24 * 60 * 60
LIBRARIES_PER_PAGE = 20
MAX_SELECTED_SCOPE_BYTES = 1024 * 1024
MAX_SOURCE_PAGES = 100
MAX_SOURCE_CHARACTERS = 5_000_000
CATALOG_PAGE_SIZE = 500
_CURSOR_KEYS = {'schema', 'scope', 'edition_after', 'after_page'}

@dataclass(frozen=True)
class SearchResources:
    """Explicit operation allowance, not a total library/result limit.

    Memory is the working-window/cache allowance. A window's raw characters
    use at most one thirty-second of it before normalization and offset maps.
    Disk is reserved for owned, disposable full-scope ranking spools.
    """
    timeout: float = 300.0
    memory_bytes: int = 64 * 1024**2
    disk_bytes: int = 2 * 1024**3

    def __post_init__(self):
        if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                or not 0 < self.timeout <= MAX_TIMEOUT_SECONDS
                or type(self.memory_bytes) is not int
                or not 16 * 1024**2 <= self.memory_bytes <= 16 * 1024**3
                or type(self.disk_bytes) is not int
                or not 16 * 1024**2 <= self.disk_bytes <= 1024**4):
            raise ValueError('Invalid search resource budget.')

    @property
    def source_characters(self):
        return self.memory_bytes // 32

    @property
    def identity(self):
        # Time may be extended for a retry without changing source boundaries.
        return self.memory_bytes, self.disk_bytes


@dataclass(frozen=True, eq=False)
class SearchCursor:
    """Opaque process-local continuation, issued by one search owner."""
    token: str = field(default_factory=lambda: uuid4().hex, repr=False)


class CursorOwner:
    """Keep one SQLite change observer; a cursor never authorizes a source.

    data_version is compared only on this same connection. Each retrieval
    still uses LibraryApp's fresh read-only authorized snapshot. Native
    widgets and an executor are deliberately absent from this owner.
    """
    def __init__(self, app):
        self._paths = self._app_paths(app)
        self._guard = Lock()
        self._closed = Event()
        self._db = None
        self._file = None
        self._epoch = None
        self._version = None
        self._file_state = None
        self._issued = WeakKeyDictionary()
        self._ranked = {}
        # Only registration metadata is reentrant; query admission remains Lock.
        self._retirement_guard = RLock()
        self._cleanup_owner = None

    @staticmethod
    def _app_paths(app):
        return app.root, app.database, app.assets_dir, app.assets.root

    def _observe(self, app, *, restart=False):
        if self._closed.is_set() or self._app_paths(app) != self._paths:
            raise ValueError('The search owner or data folder changed. Restart search.')
        app._check_database_paths()
        path = Path(app.database)
        value = path.stat()
        identity = str(path.resolve(strict=True)), value.st_dev, value.st_ino
        file_state = value.st_size, value.st_mtime_ns, value.st_ctime_ns
        if self._db is not None and identity != self._file and restart:
            self._finish_close()
            self._version = None
            self._file_state = None
        if self._db is None:
            self._db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                       isolation_level=None, timeout=0,
                                       check_same_thread=False)
            self._db.execute('PRAGMA query_only=ON')
            self._file = identity
            self._epoch = uuid4().hex
        if identity != self._file:
            raise ValueError('The library database was replaced. Close and restart search.')
        version = self._db.execute('PRAGMA data_version').fetchone()[0]
        if self._version is not None and (self._version != version or self._file_state != file_state):
            self._epoch = uuid4().hex
            self._close_ranked()
        self._version = version
        self._file_state = file_state
        return self._epoch

    @contextmanager
    def operation(self, app, scope, cursor, resources):
        if not self._guard.acquire(blocking=False):
            raise RuntimeError('A search continuation is already being read.')
        try:
            epoch = self._observe(app, restart=cursor is None)
            if cursor is None:
                payload = None
            else:
                saved = self._issued.get(cursor) if type(cursor) is SearchCursor else None
                if saved is None or saved[0] != scope:
                    raise ValueError('This search cursor belongs to another owner or source scope.')
                if saved[1] != epoch:
                    raise ValueError('The library changed since this search cursor. Restart search.')
                payload = json.loads(saved[2])
            yield payload, epoch
        finally:
            # A failed or interrupted native/directory close must never leave
            # query admission locked. The registered owner retries only after
            # this exact active operation relinquishes its guard.
            cleanup = self._cleanup_owner if self._closed.is_set() else None
            self._guard.release()
            if cleanup is not None:
                self._complete_close(cleanup)

    def check(self, app, epoch):
        if self._observe(app) != epoch:
            raise ValueError('The library changed while reading this search page. Restart search.')

    @property
    def closed(self):
        return self._closed.is_set()

    def issue(self, scope, epoch, value, resources):
        if value is None:
            return None
        if self._closed.is_set():
            raise ValueError('The search owner was closed.')
        if len(self._issued) >= resources.memory_bytes // 1024:
            raise ValueError('Retained navigation exceeds the selected memory budget. Release old cursors or restart search.')
        cursor = SearchCursor()
        self._issued[cursor] = scope, epoch, canonical(value)
        return cursor

    def ranked(self, value):
        if (type(value) is not dict or set(value) != {'schema', 'order'}
                or value['schema'] != 'carr-ranked-search-cursor/1'
                or type(value['order']) is not str or value['order'] not in self._ranked):
            raise ValueError('This completed ranking is no longer retained. Restart search.')
        return self._ranked[value['order']]

    def available_disk(self, resources):
        held = sum(value.bytes for value in self._ranked.values())
        remaining = resources.disk_bytes * 3 // 4 - held
        if remaining < 8 * 1024**2:
            raise ValueError('Retained search orders fill the selected disk allowance. Close old searches or increase Disk MiB.')
        return remaining

    def retain_ranked(self, ranked):
        self._ranked[ranked.token] = ranked
        return {'schema': 'carr-ranked-search-cursor/1', 'order': ranked.token}

    def _close_ranked(self):
        complete = True
        for identity, ranked in tuple(self._ranked.items()):
            try:
                closed = ranked.close()
            except OSError:
                closed = False
            if closed:
                del self._ranked[identity]
            else:
                complete = False
        return complete

    def _finish_close(self):
        complete = self._close_ranked()
        if self._db is not None:
            self._db.close()
            self._db = None
        self._issued.clear()
        return complete

    @staticmethod
    def _complete_close(cleanup):
        import atexit
        try:
            return cleanup.close()
        finally:
            if cleanup.quiescent:
                atexit.unregister(cleanup.close)

    def _attempt_close(self):
        if not self._guard.acquire(blocking=False):
            return False
        try:
            return self._finish_close()
        finally:
            self._guard.release()

    def reopen(self):
        """Reuse this owner only after its actual retained cleanup completes."""
        with self._retirement_guard:
            if self._closed.is_set():
                cleanup = self._cleanup_owner
                if cleanup is None or not self._complete_close(cleanup):
                    return False
            if not self._guard.acquire(blocking=False):
                return False
            try:
                if self._closed.is_set():
                    self._file = self._file_state = None
                    self._epoch = self._version = None
                    self._cleanup_owner = None
                    self._closed.clear()
                return True
            finally:
                self._guard.release()

    def close(self):
        """Withdraw immediately; retain failed cleanup without a new worker."""
        import atexit
        from ..audio.cleanup import CleanupOwner
        with self._retirement_guard:
            cleanup = self._cleanup_owner
            if cleanup is None:
                cleanup = CleanupOwner(self._attempt_close)
                self._cleanup_owner = cleanup
                # The existing owner is strongly retained even if a modal and
                # its service are dropped. Retry never accesses native widgets.
                atexit.register(cleanup.close)
            self._closed.set()
        return self._complete_close(cleanup)



def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def scope_identity(root, workspace, query, mode, result_mode, filters):
    return sha256(canonical([str(root), workspace, query, mode, result_mode, filters]).encode('utf-8')).hexdigest()


def checked_page_ids(values):
    if values is None:
        return '', ()
    if (type(values) not in (tuple, list) or len(values) > MAX_SOURCE_PAGES
            or any(type(value) is not int or value < 1 for value in values)
            or len(set(values)) != len(values)):
        raise ValueError('Invalid bounded source-page selection.')
    if not values:
        return ' AND 0', ()
    return ' AND id IN (' + ','.join('?' for _ in values) + ')', tuple(values)


def _text(value):
    return type(value) is str and 0 < len(value) <= 256 and '\x00' not in value


def _cursor(value, scope):
    if value is None:
        return {'schema': 'carr-source-search-cursor/1', 'scope': scope,
                'edition_after': None, 'after_page': None}
    if (type(value) is not dict or set(value) != _CURSOR_KEYS
            or value['schema'] != 'carr-source-search-cursor/1' or value['scope'] != scope
            or value['edition_after'] is not None and not _text(value['edition_after'])):
        raise ValueError('This search cursor belongs to different options or source scope. Restart search.')
    previous = value['after_page']
    if previous is not None and (type(previous) not in (list, tuple) or len(previous) != 4
            or not _text(previous[0]) or not _text(previous[1])
            or any(type(item) is not int or item < 1 for item in previous[2:])):
        raise ValueError('Invalid source search continuation.')
    return json.loads(canonical(value))


@dataclass(frozen=True)
class SourceWindow:
    editions: dict
    page_ids: tuple
    cursor: dict
    next_cursor: dict | None
    identity: str
    characters: int
    source_editions: int = 0


@dataclass(frozen=True)
class SearchPage:
    results: tuple
    cursor: SearchCursor
    next_source_cursor: SearchCursor | None
    has_more_results: bool
    offset: int
    source_pages: int
    source_editions: int
    source_characters: int
    window_identity: str
    ranking_scope: str = 'window'
    total_results: int | None = None
    index_bytes: int = 0
    updated_index_rows: int = 0


def select_window(db, catalog, *, scope, cursor=None, check=lambda: None, resources=None, **filters):
    """Select <=100 source pages within the chosen memory allowance.

    Catalog and raw-page cursors are independent: an edition larger than this
    window continues from its exact active revision instead of being rejected.
    Empty catalog windows still advance. Results are ranked within this source
    window, not presented as global relevance over unvisited material.
    """
    check()
    if resources is None:
        resources = SearchResources()
    if type(resources) is not SearchResources:
        raise ValueError('A validated search resource budget is required.')
    character_limit = resources.source_characters
    current = _cursor(cursor, scope)
    views = browse_editions(catalog, after=current['edition_after'],
                            limit=CATALOG_PAGE_SIZE, **filters)
    check()
    allowed = {view.edition_id: view.work_id for view in views}
    if not views:
        return SourceWindow({}, (), current, None, sha256(b'[]').hexdigest(), 0)
    next_catalog = browse_editions(catalog, after=views[-1].edition_id, limit=1, **filters)
    previous = current['after_page']
    if previous is not None:
        edition, revision, ordinal, page_id = previous
        row = db.execute('SELECT edition,revision,ordinal,id FROM active_page '
                         'WHERE workspace=? AND id=?', (catalog.workspace, page_id)).fetchone()
        if edition not in allowed or row is None or tuple(row) != tuple(previous):
            raise ValueError('The active source changed since this cursor. Restart search.')
    placeholders = ','.join('?' for _ in allowed)
    sql = ('SELECT id,edition,revision,ordinal,length(raw),digest FROM active_page '
           'WHERE workspace=? AND edition IN (' + placeholders + ')')
    parameters = [catalog.workspace, *allowed]
    if previous is not None:
        edition, revision, ordinal, page_id = previous
        sql += (' AND (edition COLLATE BINARY>? OR (edition=? AND '
                '(ordinal>? OR (ordinal=? AND id>?))))')
        parameters += [edition, edition, ordinal, ordinal, page_id]
    sql += ' ORDER BY edition COLLATE BINARY,ordinal,id LIMIT ?'
    parameters.append(MAX_SOURCE_PAGES + 1)
    rows, characters, more_pages = [], 0, False
    source = db.execute(sql, parameters)
    try:
        for row in source:
            check()
            page_id, edition, revision, ordinal, count, digest = row
            if (type(page_id) is not int or page_id < 1 or edition not in allowed
                    or not _text(revision) or type(ordinal) is not int or ordinal < 1
                    or type(count) is not int or count < 0 or type(digest) is not str):
                raise ValueError('Invalid active source navigation metadata.')
            if count > character_limit:
                raise ValueError('One extracted source page exceeds the selected working-memory allowance. Increase the search memory budget or re-import with smaller reading segments.')
            if len(rows) == MAX_SOURCE_PAGES or characters + count > character_limit:
                more_pages = True
                break
            rows.append(tuple(row))
            characters += count
    finally:
        source.close()
    check()
    if more_pages:
        last = rows[-1]
        following = {**current, 'after_page': [last[1], last[2], last[3], last[0]]}
    elif next_catalog:
        following = {**current, 'edition_after': views[-1].edition_id, 'after_page': None}
    else:
        following = None
    identity = sha256(canonical([scope, tuple(allowed.items()), rows]).encode('utf-8')).hexdigest()
    return SourceWindow(allowed, tuple(row[0] for row in rows), current, following, identity, characters,
                        len({row[1] for row in rows}))
