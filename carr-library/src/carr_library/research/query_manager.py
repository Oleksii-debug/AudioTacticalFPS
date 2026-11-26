"""Manage existing topic search definitions without changing the storage schema.

Queries store criteria, never cached result sets. All operations reauthorize the
library and topic. Changes compare the complete definition the user reviewed in
the same transaction as the write, so another editor cannot be overwritten.
"""
from dataclasses import dataclass
from uuid import uuid4

from .saved_evidence import SavedEvidenceStore
from .saved_topic_query import SavedQuery, SavedTopicQueries
from .topic_tree import _id, _name


@dataclass(frozen=True)
class QueryPage:
    rows: tuple[SavedQuery, ...]
    total: int
    next_cursor: tuple[str, str] | None


class QueryManager:
    """One explicit library/topic; compatible with the host's versioned storage."""

    def __init__(self, library, workspace, topic):
        self.library = library
        self.workspace, self.topic = _id(workspace), _id(topic)
        self._location = (library.root, library.database)

    def _bound(self):
        if (self.library.root, self.library.database) != self._location:
            raise ValueError('The library location changed. Open saved searches again.')

    def _queries(self, db, cancelled=lambda: False):
        load, metadata, _ = self.library._loaders(db, self.workspace, cancelled=cancelled)
        store = SavedEvidenceStore(db, self.workspace, load, metadata)
        queries = SavedTopicQueries(store)
        queries.tree._visible(self.topic)
        return queries

    def _get(self, queries, identity):
        row = queries.db.execute(
            'SELECT id,name,query,field,result_limit,archived FROM research_saved_query '
            'WHERE workspace=? AND topic=? AND id=?',
            (self.workspace, self.topic, _id(identity))).fetchone()
        if row is None:
            raise ValueError('The saved search is no longer available in this topic.')
        return queries._row(row)

    def _expected(self, queries, expected):
        if not isinstance(expected, SavedQuery):
            raise ValueError('Select a saved search before using this command.')
        current = self._get(queries, expected.id)
        if current != expected:
            raise ValueError('The saved search changed. Refresh and review it before continuing.')
        return current

    def list(self, *, include_archived=False, limit=100, after=None):
        self._bound()
        if type(include_archived) is not bool or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid saved-search page options.')
        if after is not None:
            if type(after) is not tuple or len(after) != 2:
                raise ValueError('Invalid saved-search page cursor.')
            _name(after[0])
            _id(after[1])
        with self.library.read(self.workspace) as db:
            queries = self._queries(db)
            where = 'workspace=? AND topic=? AND (archived=0 OR ?=1)'
            params = (self.workspace, self.topic, int(include_archived))
            total = db.execute('SELECT COUNT(*) FROM research_saved_query WHERE ' + where,
                               params).fetchone()[0]
            if after is not None:
                where += ' AND (name>? OR (name=? AND id>?))'
                params += (after[0], after[0], after[1])
            rows = tuple(queries._row(row) for row in db.execute(
                'SELECT id,name,query,field,result_limit,archived FROM research_saved_query '
                'WHERE ' + where + ' ORDER BY name,id LIMIT ?', (*params, limit + 1)))
            cursor = (rows[limit - 1].name, rows[limit - 1].id) if len(rows) > limit else None
            return QueryPage(rows[:limit], total, cursor)

    def get(self, identity):
        self._bound()
        with self.library.read(self.workspace) as db:
            return self._get(self._queries(db), identity)

    def create(self, name, query, *, field='quote', limit=50):
        self._bound()
        identity = uuid4().hex
        with self.library.write(self.workspace) as db:
            queries = self._queries(db)
            queries.create(self.topic, identity, name, query, field=field, limit=limit)
            return self._get(queries, identity)

    def update(self, expected, name, query, *, field='quote', limit=50):
        self._bound()
        with self.library.write(self.workspace) as db:
            queries = self._queries(db)
            self._expected(queries, expected)
            queries.update(self.topic, expected.id, name, query, field=field, limit=limit)
            return self._get(queries, expected.id)

    def duplicate(self, expected, name):
        self._bound()
        identity = uuid4().hex
        with self.library.write(self.workspace) as db:
            queries = self._queries(db)
            current = self._expected(queries, expected)
            queries.create(self.topic, identity, name, current.query,
                           field=current.field, limit=current.limit)
            return self._get(queries, identity)

    def archive(self, expected):
        self._bound()
        with self.library.write(self.workspace) as db:
            queries = self._queries(db)
            self._expected(queries, expected)
            queries.archive(self.topic, expected.id)
            return self._get(queries, expected.id)

    def restore(self, expected):
        self._bound()
        with self.library.write(self.workspace) as db:
            queries = self._queries(db)
            current = self._expected(queries, expected)
            if not current.archived:
                raise ValueError('This saved search is already active.')
            if db.execute('UPDATE research_saved_query SET archived=0 '
                          'WHERE workspace=? AND topic=? AND id=? AND archived=1',
                          (self.workspace, self.topic, current.id)).rowcount != 1:
                raise ValueError('The archived search changed. Refresh the list.')
            return self._get(queries, current.id)

    def run(self, expected, *, timeout=60.0, cancelled=lambda: False):
        self._bound()
        with self.library._search_read(self.workspace, timeout, cancelled) as (db, budget):
            queries = self._queries(db, budget.stopped)
            current = self._expected(queries, expected)
            if current.archived:
                raise ValueError('Restore this saved search before running it.')
            return queries.run(self.topic, current.id, timeout=budget.remaining(),
                               cancelled=budget.stopped)
