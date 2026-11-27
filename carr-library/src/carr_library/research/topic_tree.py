"""Candidate research topic service; migration and caller authorization are external."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Topic:
    id: str
    parent: str | None
    name: str
    position: int
    archived: bool


def _id(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256 or '\x00' in value:
        raise ValueError('invalid topic or workspace identity')
    return value


def _name(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 512 or '\x00' in value:
        raise ValueError('invalid topic name')
    return value


def _position(value):
    if type(value) is not int or not 0 <= value <= 2**31 - 1:
        raise ValueError('invalid topic order')
    return value


# Native SQLite tree traversal. Adapted from the PR17 bootstrap move_topic
# recursive descendant query; this service never creates or migrates tables.
_VISIBLE = '''WITH RECURSIVE visible(id) AS (
    SELECT id FROM research_topic WHERE workspace=? AND parent IS NULL AND archived=0
    UNION
    SELECT t.id FROM research_topic t JOIN visible v ON t.parent=v.id
      WHERE t.workspace=? AND t.archived=0
) '''


class TopicTree:
    """Bind to one already-authorized workspace and caller-owned transaction.

    Mutations require the caller's BEGIN IMMEDIATE (e.g. Storage.transaction).
    No implicit commit, migration, global query, deletion, or source mutation.
    """
    def __init__(self, db, workspace):
        self.db = db
        self.workspace = _id(workspace)

    def _ready(self):
        if (not self.db.in_transaction
                or self.db.execute('PRAGMA foreign_keys').fetchone()[0] != 1
                or self.db.execute('PRAGMA read_uncommitted').fetchone()[0]):
            raise ValueError('consistent transaction with foreign keys required')

    def _visible(self, topic):
        self._ready()
        if not self.db.execute(_VISIBLE + 'SELECT 1 FROM visible WHERE id=?',
                               (self.workspace, self.workspace, _id(topic))).fetchone():
            raise ValueError('unknown or archived topic')

    def create(self, topic, name, *, parent=None, position=0):
        self._ready()
        if parent is not None:
            self._visible(parent)
        self.db.execute('INSERT INTO research_topic(workspace,id,parent,name,position,archived) '
                        'VALUES(?,?,?,?,?,0)',
                        (self.workspace, _id(topic), parent, _name(name), _position(position)))

    def rename(self, topic, name):
        self._visible(topic)
        self.db.execute('UPDATE research_topic SET name=? WHERE workspace=? AND id=?',
                        (_name(name), self.workspace, topic))

    def move(self, topic, parent=None, *, position=0):
        self._visible(topic)
        if parent is not None:
            self._visible(parent)
            # PR17's UNION-based traversal terminates even on malformed cycles.
            if self.db.execute('''WITH RECURSIVE d(id) AS (
                SELECT ? UNION SELECT t.id FROM research_topic t JOIN d ON t.parent=d.id
                WHERE t.workspace=?) SELECT 1 FROM d WHERE id=?''',
                (topic, self.workspace, parent)).fetchone():
                raise ValueError('topic cycle')
        self.db.execute('UPDATE research_topic SET parent=?,position=? WHERE workspace=? AND id=?',
                        (parent, _position(position), self.workspace, topic))

    def archive(self, topic):
        self._visible(topic)
        self.db.execute('UPDATE research_topic SET archived=1 WHERE workspace=? AND id=?',
                        (self.workspace, topic))

    def list_topics(self, *, include_archived=False, limit=None):
        self._ready()
        fields = 'SELECT id,parent,name,position,archived FROM research_topic WHERE workspace=? '
        order = 'ORDER BY parent IS NOT NULL,parent,position,id'
        if type(include_archived) is not bool:
            raise ValueError('explicit archive flag required')
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 10001):
            raise ValueError('bounded topic result limit required')
        suffix = ' LIMIT ?' if limit is not None else ''
        parameters = (limit,) if limit is not None else ()
        if include_archived:
            rows = self.db.execute(fields + order + suffix, (self.workspace, *parameters))
        else:
            rows = self.db.execute(_VISIBLE + fields + 'AND id IN (SELECT id FROM visible) ' + order + suffix,
                                   (self.workspace, self.workspace, self.workspace, *parameters))
        return tuple(Topic(r[0], r[1], r[2], r[3], bool(r[4])) for r in rows)
