"""Persistent workspace lifecycle in the shared V3 database."""
from dataclasses import dataclass

from ..research.topic_tree import _id


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    description: str
    archived: bool


def _text(value, label, limit, *, required=False):
    if (not isinstance(value, str) or len(value) > limit or '\x00' in value
            or (required and not value.strip())):
        raise ValueError(f'invalid workspace {label}')
    return value


class WorkspaceStore:
    """Caller authenticates local library access and owns the transaction.

    Workspace selection is data isolation in the local single-user desktop
    application; these IDs are not security credentials.
    """
    def __init__(self, db):
        self.db = db

    def _ready(self):
        if (not self.db.in_transaction
                or self.db.execute('PRAGMA foreign_keys').fetchone()[0] != 1
                or self.db.execute('PRAGMA recursive_triggers').fetchone()[0] != 1
                or self.db.execute('PRAGMA read_uncommitted').fetchone()[0]):
            raise ValueError('consistent transaction with integrity guards required')

    def create(self, workspace, name, description=''):
        self._ready()
        _id(workspace)
        _text(name, 'name', 512, required=True)
        _text(description, 'description', 100000)
        # Keep the identity and its profile atomic even when the caller catches
        # an individual statement failure inside a larger transaction.
        self.db.execute('SAVEPOINT workspace_create')
        try:
            self.db.execute('INSERT INTO workspace(id) VALUES(?)', (workspace,))
            self.db.execute('INSERT INTO workspace_profile VALUES(?,?,?,0)',
                            (workspace, name, description))
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK TO workspace_create')
                self.db.execute('RELEASE workspace_create')
            raise
        self.db.execute('RELEASE workspace_create')
        return Workspace(workspace, name, description, False)

    def require_active(self, workspace):
        self._ready()
        row = self.db.execute('SELECT workspace,name,description,archived '
                              'FROM workspace_profile WHERE workspace=? AND archived=0',
                              (_id(workspace),)).fetchone()
        if row is None:
            raise ValueError('unknown or archived workspace')
        return Workspace(row[0], row[1], row[2], bool(row[3]))

    def update(self, workspace, *, name=None, description=None):
        current = self.require_active(workspace)
        name = current.name if name is None else _text(name, 'name', 512, required=True)
        description = current.description if description is None else _text(description, 'description', 100000)
        self.db.execute('UPDATE workspace_profile SET name=?,description=? WHERE workspace=?',
                        (name, description, workspace))
        return Workspace(workspace, name, description, False)

    def archive(self, workspace):
        self.require_active(workspace)
        self.db.execute('UPDATE workspace_profile SET archived=1 WHERE workspace=?', (workspace,))

    def list_workspaces(self, *, include_archived=False):
        self._ready()
        if type(include_archived) is not bool:
            raise ValueError('explicit archive flag required')
        query = 'SELECT workspace,name,description,archived FROM workspace_profile '
        if not include_archived:
            query += 'WHERE archived=0 '
        return tuple(Workspace(row[0], row[1], row[2], bool(row[3]))
                     for row in self.db.execute(query + 'ORDER BY name,workspace'))
