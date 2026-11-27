"""Independent PR56 handler audit. Set CARR_TOPIC_PANEL to exact topic_panel.py.
Uses real candidate Python class, synthetic rows and wx/service doubles only.
"""
import ast
import os
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
import unittest


@dataclass(frozen=True)
class Row:
    id: str
    parent: str | None
    name: str
    position: int = 0


class Item:
    def __init__(self, label='', data=None, valid=True):
        self.label, self.data, self.valid = label, data, valid
    def IsOk(self):
        return self.valid


class Tree:
    def __init__(self):
        self.selected = Item(valid=False)
        self.focused = False
    def DeleteAllItems(self):
        self.selected = Item(valid=False)
    def AddRoot(self, label):
        return Item(label)
    def AppendItem(self, parent, label, data=None):
        return Item(label, data)
    def SelectItem(self, item):
        self.selected = item
    def GetSelection(self):
        return self.selected
    def GetItemData(self, item):
        return item.data
    def EnsureVisible(self, item):
        pass
    def Expand(self, item):
        pass
    def SetFocus(self):
        self.focused = True


class Choice:
    def __init__(self):
        self.Clear()
    def Clear(self):
        self.labels, self.selected = [], -1
    def Append(self, label):
        self.labels.append(label)
    def SetSelection(self, index):
        self.selected = index
    def GetSelection(self):
        return self.selected


class Audit(unittest.TestCase):
    def setUp(self):
        src = Path(os.environ['CARR_TOPIC_PANEL'])
        parsed = ast.parse(src.read_text(encoding='utf-8'))
        cls, = [n for n in parsed.body if isinstance(n, ast.ClassDef) and n.name == 'TopicPanel']
        ns = {'wx': SimpleNamespace(Panel=object)}
        exec(compile(ast.Module(body=[cls], type_ignores=[]), str(src), 'exec'), ns)
        self.panel = p = ns['TopicPanel'].__new__(ns['TopicPanel'])
        self.rows = [Row('a', None, 'History'), Row('b', None, 'History', 1),
                     Row('c', 'a', 'Notes'), Row('d', 'b', 'Notes'),
                     Row('r', None, 'Root (no parent)', 2), Row('x', None, 'Move me', 3)]
        p.workspace = 'w'
        p.rows, p.items, p.parents = {}, {}, []
        p.tree, p.destination = Tree(), Choice()
        p.status = SimpleNamespace(SetValue=lambda text: None)
        @contextmanager
        def transaction():
            yield SimpleNamespace(workspace='w', list_topics=lambda: tuple(self.rows), move=self.move)
        p.transaction = transaction
        p.refresh(selected='x')

    def move(self, selected, parent, position):
        self.rows = [replace(r, parent=parent, position=position) if r.id == selected else r
                     for r in self.rows]

    def label(self, identity):
        p = self.panel
        return p.destination.labels[p.parents.index(identity)]

    def test_same_name_siblings_distinguishable(self):
        self.assertNotEqual(self.label('a'), self.label('b'))

    def test_same_name_nested_destinations_distinguishable(self):
        self.assertNotEqual(self.label('c').strip(), self.label('d').strip())

    def test_real_topic_does_not_alias_root_action(self):
        self.assertNotEqual(self.label(None), self.label('r'))

    def test_selected_identity_preserved_after_refresh(self):
        self.panel.refresh()
        self.assertEqual(self.panel._selected(), 'x')

    def test_move_uses_exact_selected_destination_and_restores_focus(self):
        p = self.panel
        p.destination.SetSelection(p.parents.index('b'))
        p.move()
        self.assertEqual(p.rows['x'].parent, 'b')
        self.assertEqual(p._selected(), 'x')
        self.assertTrue(p.tree.focused)


if __name__ == '__main__':
    unittest.main()
