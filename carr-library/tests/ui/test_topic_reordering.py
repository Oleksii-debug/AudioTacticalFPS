"""Real TopicTree transactions behind reorder handlers; native probe is separate."""
import ast
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from carr_library.research.topic_tree import TopicTree


SOURCE = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/topic_panel.py'


class Item:
    def __init__(self, value=None):
        self.value = value

    def IsOk(self):
        return True


class Tree:
    """Only wx tree storage/focus is substituted; service mutations use SQLite."""
    def __init__(self):
        self.selected = Item()
        self.focused = False

    def GetSelection(self):
        return self.selected

    def GetItemData(self, item):
        return item.value

    def DeleteAllItems(self):
        self.selected = Item()

    def AddRoot(self, label):
        return Item()

    def AppendItem(self, parent, label, data=None):
        return Item(data)

    def SelectItem(self, item):
        self.selected = item

    def EnsureVisible(self, item):
        pass

    def Expand(self, item):
        pass

    def SetFocus(self):
        self.focused = True


class Topics:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.active = 'a'
        self.db.executescript('''
            PRAGMA foreign_keys=ON;
            CREATE TABLE workspace(id TEXT PRIMARY KEY, archived INTEGER NOT NULL DEFAULT 0);
            INSERT INTO workspace(id) VALUES('a'),('b');
        ''')
        self.db.executescript((SOURCE.parents[3] / 'tests/reader/topic_tree_candidate.sql').read_text())
        with self.transaction() as service:
            for ident, position in (('root-a', 0), ('root-b', 0), ('root-c', 2**31 - 1)):
                service.create(ident, ident, position=position)
            for ident, position in (('child-a', 9), ('child-b', 9), ('child-c', 2**31 - 1)):
                service.create(ident, ident, parent='root-a', position=position)
            service.create('grandchild', 'Grandchild', parent='child-b', position=27)
            service.create('archived', 'Archived section', parent='root-a', position=30)
            service.archive('archived')
        self.active = 'b'
        with self.transaction() as service:
            service.create('root-a', 'Private other workspace', position=42)
        self.active = 'a'

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        with self.db:
            if self.db.execute('SELECT archived FROM workspace WHERE id=?', (self.active,)).fetchone()[0]:
                raise ValueError('workspace archived')
            yield TopicTree(self.db, self.active)

    def rows(self):
        return tuple(self.db.execute('SELECT workspace,id,parent,name,position,archived '
                                     'FROM research_topic ORDER BY workspace,id'))

    def order(self, parent=None):
        with self.transaction() as service:
            return tuple(row.id for row in service.list_topics() if row.parent == parent)


@pytest.fixture
def topics(tmp_path):
    fixture = Topics(tmp_path / 'topics.sqlite')
    yield fixture
    fixture.db.close()


@pytest.fixture
def panel(topics):
    # Load the actual class without changing sys.modules['wx'] for other tests.
    module = ast.parse(SOURCE.read_text(encoding='utf-8'))
    cls = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == 'TopicPanel')
    namespace = {'wx': SimpleNamespace(Panel=object), 'uuid4': uuid4}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(SOURCE), 'exec'), namespace)
    current = object.__new__(namespace['TopicPanel'])
    current.workspace, current.transaction = 'a', topics.transaction
    current.rows, current.items, current.parents = {}, {}, []
    current.tree, current.destination, current.status = Tree(), Mock(), Mock()
    current.refresh()
    return current


def select(panel, topic):
    panel.tree.SelectItem(panel.items[topic])
    panel.tree.focused = False


def test_root_reordering_handles_tied_sparse_positions_and_persists(panel, topics):
    select(panel, 'root-b')
    panel.move_up()
    assert topics.order() == ('root-b', 'root-a', 'root-c')
    assert panel._selected() == 'root-b' and panel.tree.focused
    assert panel.status.SetValue.call_args.args[0] == 'Topic moved up.'
    panel.move_down()
    assert topics.order() == ('root-a', 'root-b', 'root-c')
    assert panel._selected() == 'root-b'
    # A fresh connection sees the committed order, not an in-memory UI rearrangement.
    with sqlite3.connect(topics.path) as reopened:
        assert reopened.execute('SELECT id,position FROM research_topic '
                                'WHERE workspace=? AND parent IS NULL ORDER BY position,id', ('a',)).fetchall() == [
                                    ('root-a', 0), ('root-b', 1), ('root-c', 2)]


def test_nested_reordering_preserves_parent_descendants_archives_and_other_workspace(panel, topics):
    siblings = {'child-a', 'child-b', 'child-c'}
    untouched = tuple(row for row in topics.rows() if row[0] != 'a' or row[1] not in siblings)
    select(panel, 'child-b')
    panel.move_down()
    assert topics.order('root-a') == ('child-a', 'child-c', 'child-b')
    assert panel.rows['child-b'].parent == 'root-a'
    assert panel.rows['grandchild'].parent == 'child-b'
    assert panel._selected() == 'child-b' and panel.tree.focused
    assert tuple(row for row in topics.rows() if row[0] != 'a' or row[1] not in siblings) == untouched


@pytest.mark.parametrize('topic,action,edge', [
    ('root-a', 'move_up', 'first'),
    ('root-c', 'move_down', 'last'),
    ('grandchild', 'move_up', 'first'),
    ('grandchild', 'move_down', 'last'),
])
def test_first_last_and_only_sibling_are_noops_with_selection_and_focus(panel, topics, topic, action, edge):
    before, changes = topics.rows(), topics.db.total_changes
    select(panel, topic)
    getattr(panel, action)()
    assert topics.rows() == before and topics.db.total_changes == changes
    assert panel._selected() == topic and panel.tree.focused
    assert f'already {edge}' in panel.status.SetValue.call_args.args[0]


@pytest.mark.parametrize('changed', ['renamed', 'archived', 'parent_archived'])
def test_stale_or_archived_topic_cannot_be_reordered(panel, topics, changed):
    select(panel, 'child-b')
    with topics.transaction() as service:
        if changed == 'renamed':
            service.rename('child-b', 'Changed elsewhere')
        else:
            service.archive('root-a' if changed == 'parent_archived' else 'child-b')
    before, changes = topics.rows(), topics.db.total_changes
    panel.move_up()
    assert topics.rows() == before and topics.db.total_changes == changes
    assert 'rejected' in panel.status.SetValue.call_args.args[0]
    assert panel.tree.focused


@pytest.mark.parametrize('revocation', ['other_workspace', 'archived_workspace'])
def test_workspace_authorization_is_rechecked_before_reordering(panel, topics, revocation):
    select(panel, 'root-b')
    if revocation == 'other_workspace':
        topics.active = 'b'
    else:
        with topics.db:
            topics.db.execute("UPDATE workspace SET archived=1 WHERE id='a'")
    before, changes = topics.rows(), topics.db.total_changes
    panel.move_up()
    assert topics.rows() == before and topics.db.total_changes == changes
    assert panel.rows == {} and panel._selected() is None
    assert 'rejected' in panel.status.SetValue.call_args.args[0]


def test_partial_reorder_failure_rolls_back_the_existing_transaction(panel, topics, monkeypatch):
    select(panel, 'root-b')
    before = topics.rows()
    original = TopicTree.move
    calls = []

    def fail_second_move(service, *args, **kwargs):
        calls.append(args[0])
        if len(calls) == 2:
            raise sqlite3.OperationalError('synthetic write failure')
        return original(service, *args, **kwargs)

    monkeypatch.setattr(TopicTree, 'move', fail_second_move)
    panel.move_up()
    assert len(calls) == 2 and topics.rows() == before
    assert panel._selected() == 'root-b' and panel.tree.focused
    assert 'rejected' in panel.status.SetValue.call_args.args[0]


def test_no_selected_topic_does_not_write(panel, topics):
    before, changes = topics.rows(), topics.db.total_changes
    panel.move_down()
    assert topics.rows() == before and topics.db.total_changes == changes
    assert 'rejected' in panel.status.SetValue.call_args.args[0]
    assert panel.tree.focused


