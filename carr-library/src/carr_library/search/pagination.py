"""Bounded opaque continuations for the existing scoped search service.

Tokens are local to one open LibraryApp. They are not saved authority and do not
contain source text, paths or credentials. Every use re-enters current catalog
authorization and checks the complete scoped source snapshot before retrieval.
"""
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
import json
from secrets import token_urlsafe
from threading import RLock


@dataclass(frozen=True)
class SearchPage(Sequence):
    results: tuple
    next_cursor: str | None
    scanned_segments: int = 0
    scope_editions: int = 0
    ordering: str = 'Source edition, segment and raw offset.'

    @property
    def complete(self):
        return self.next_cursor is None

    def __len__(self):
        return len(self.results)

    def __getitem__(self, index):
        return self.results[index]


@dataclass(frozen=True)
class Continuation:
    request: str
    snapshot: str | None = None
    after: tuple | None = None
    kind: str = 'literal'


def request_identity(workspace, query, *, mode, collection, title, author,
                     language, edition, limit, authorized_topic, result_mode,
                     page_budget=None, character_budget=None):
    values = dict(workspace=workspace, query=query, mode=mode,
        collection=collection, title=title, author=author, language=language,
        edition=edition, limit=limit, topic=authorized_topic, result_mode=result_mode,
        page_budget=page_budget, character_budget=character_budget)
    # Public service validation still owns semantic validation. Reject objects
    # with executable serialization or an unbounded opaque token before lookup.
    if any(value is not None and type(value) not in (str, int)
           for value in values.values()):
        raise ValueError('Invalid search continuation request.')
    encoded = json.dumps(values, sort_keys=True, ensure_ascii=True,
                         separators=(',', ':')).encode('ascii')
    if len(encoded) > 32_768:
        raise ValueError('Search continuation request is too large.')
    return sha256(encoded).hexdigest()


def catalog_snapshot(request, views):
    """Bind current metadata/filter membership to the literal source snapshot."""
    digest = sha256(request.encode('ascii'))
    for view in views:
        digest.update(json.dumps(
            (view.edition_id, view.work_id, view.asset, view.title, view.language,
             view.edition, view.volume, view.authors),
            ensure_ascii=True, separators=(',', ':')).encode('ascii') + b'\n')
    return digest.hexdigest()


class CursorStore:
    """A fixed-size, per-application table; failed work publishes no cursor."""
    def __init__(self, maximum=128):
        if type(maximum) is not int or not 2 <= maximum <= 512:
            raise ValueError('Invalid search continuation allowance.')
        self.maximum = maximum
        self._values = OrderedDict()
        self._lock = RLock()

    def begin(self, request, cursor):
        if cursor is None:
            return Continuation(request)
        if type(cursor) is not str or not 20 <= len(cursor) <= 128:
            raise ValueError('The search continuation is invalid. Start a new search.')
        with self._lock:
            value = self._values.get(cursor)
            if value is None or value.request != request:
                raise ValueError('The search continuation expired or belongs to different search options.')
            # A failed or cancelled attempt must not mutate even the LRU order.
            return value

    def fork(self):
        """Isolate tokens with the existing temporary LibraryApp search copy."""
        result = CursorStore(self.maximum)
        with self._lock:
            result._values = self._values.copy()
        return result

    def stage(self, continuation):
        """Prepare an internal typed position; failed callers publish nothing."""
        if type(continuation) is not Continuation or continuation.kind not in ('literal', 'window', 'federated'):
            raise ValueError('Invalid internal search continuation.')
        token = token_urlsafe(32)
        return token, (token, continuation)

    def prepare(self, continuation, results, literal_page, *, scanned_segments=None, scope_editions=None):
        scanned = (getattr(literal_page, 'scanned_segments', 0)
                   if scanned_segments is None else scanned_segments)
        editions = (getattr(literal_page, 'scope_editions', 0)
                    if scope_editions is None else scope_editions)
        ordering = getattr(literal_page, 'ordering', 'Source edition, segment and raw offset.')
        if literal_page.after is None:
            return SearchPage(tuple(results), None, scanned, editions, ordering), None
        value = Continuation(continuation.request, literal_page.snapshot, literal_page.after,
                             getattr(literal_page, 'kind', 'literal'))
        token, prepared = self.stage(value)
        return SearchPage(tuple(results), token, scanned, editions, ordering), prepared

    def publish(self, prepared):
        if prepared is None:
            return
        token, value = prepared
        with self._lock:
            self._values[token] = value
            while len(self._values) > self.maximum:
                self._values.popitem(last=False)
