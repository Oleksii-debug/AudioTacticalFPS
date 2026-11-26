"""Persistent topic query definitions; execution delegates to existing topic search."""
from dataclasses import dataclass
import unicodedata

from .topic_tree import TopicTree, _id, _name
from .topic_search import search_topic


@dataclass(frozen=True)
class SavedQuery:
    id: str
    name: str
    query: str
    field: str
    limit: int
    archived: bool


def _definition(name, query, field, limit):
    _name(name)
    if (not isinstance(query, str) or not query.strip() or len(query) > 500
            or any(unicodedata.category(c) in ('Cc', 'Cs') and c not in '\t\n\r' for c in query)):
        raise ValueError('invalid saved query')
    if field not in ('quote', 'note') or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('invalid saved query options')
    return name, query, field, limit


class SavedTopicQueries:
    """One authorized workspace, caller-owned transaction; no implicit migration."""
    def __init__(self, store):
        self.store = store
        self.db = store.db
        self.workspace = _id(store.workspace)
        self.tree = TopicTree(self.db, self.workspace)

    def _topic(self, topic):
        self.tree._ready()
        if not self.db.execute('SELECT 1 FROM research_topic WHERE workspace=? AND id=?',
                               (self.workspace, _id(topic))).fetchone():
            raise ValueError('unknown topic')

    def create(self, topic, identity, name, query, *, field='quote', limit=50):
        self.tree._visible(topic)
        self.db.execute('INSERT INTO research_saved_query(workspace,topic,id,name,query,field,result_limit,archived) '
                        'VALUES(?,?,?,?,?,?,?,0)', (self.workspace, topic, _id(identity),
                                                   *_definition(name, query, field, limit)))

    def update(self, topic, identity, name, query, *, field='quote', limit=50):
        self.tree._visible(topic)
        if self.db.execute('UPDATE research_saved_query SET name=?,query=?,field=?,result_limit=? '
                           'WHERE workspace=? AND topic=? AND id=? AND archived=0',
                           (*_definition(name, query, field, limit), self.workspace, topic,
                            _id(identity))).rowcount != 1:
            raise ValueError('unknown or archived query')

    def archive(self, topic, identity):
        self.tree._visible(topic)
        if self.db.execute('UPDATE research_saved_query SET archived=1 '
                           'WHERE workspace=? AND topic=? AND id=? AND archived=0',
                           (self.workspace, topic, _id(identity))).rowcount != 1:
            raise ValueError('unknown or archived query')

    def list_queries(self, topic, *, include_archived=False):
        self._topic(topic)
        if type(include_archived) is not bool:
            raise ValueError('explicit archive flag required')
        rows = self.db.execute('SELECT id,name,query,field,result_limit,archived '
                               'FROM research_saved_query WHERE workspace=? AND topic=? '
                               'AND (archived=0 OR ?=1) ORDER BY name,id',
                               (self.workspace, topic, int(include_archived)))
        return tuple(self._row(r) for r in rows)

    @staticmethod
    def _row(row):
        identity, name, query, field, limit, archived = row
        if type(archived) is not int or archived not in (0, 1):
            raise ValueError('invalid archive state')
        return SavedQuery(_id(identity), *_definition(name, query, field, limit), bool(archived))

    def run(self, topic, identity):
        self._topic(topic)
        row = self.db.execute('SELECT id,name,query,field,result_limit,archived '
                              'FROM research_saved_query WHERE workspace=? AND topic=? AND id=? AND archived=0',
                              (self.workspace, topic, _id(identity))).fetchone()
        if row is None:
            raise ValueError('unknown or archived query')
        q = self._row(row)
        return search_topic(self.store, self.workspace, topic, q.query, field=q.field, limit=q.limit)
