"""Headless shell probe. Fake wx and child panels; not native accessibility proof."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch


class Control:
    def __init__(self, *args, **kw):
        self.items, self.pages = [], []
        self.selection, self.value = -1, ''
        self.destroyed = self.focused = self.hidden = False
    def SetSizer(self, x): pass
    def SetName(self, x): pass
    def Bind(self, *args): pass
    def Layout(self): pass
    def Add(self, *args): pass
    def Detach(self, *args): pass
    def Clear(self): self.items, self.selection = [], -1
    def Append(self, x): self.items.append(x)
    def SetSelection(self, x): self.selection = x
    def GetSelection(self): return self.selection
    def SetValue(self, x): self.value = x
    def GetValue(self): return self.value
    def SetFocus(self): self.focused = True
    def Hide(self): self.hidden = True
    def Destroy(self): self.destroyed = True
    def AddPage(self, page, label):
        self.pages.append((page, label))
        return True


class Child:
    def __init__(self, *args, **kw):
        self.args = args
        self.query = self.note = Control()
        self.displayed = SimpleNamespace(note='saved')
        self.note.SetValue('saved')
    def set_workspace(self, workspace): self.workspace = workspace


def load():
    wx = ModuleType('wx')
    for name in ('Frame', 'Panel', 'Choice', 'Button', 'TextCtrl', 'StaticText', 'BoxSizer', 'Notebook'):
        setattr(wx, name, Control)
    for name in ('VERTICAL', 'EXPAND', 'TE_READONLY', 'EVT_BUTTON', 'EVT_CLOSE', 'YES_NO', 'NO_DEFAULT', 'ICON_QUESTION', 'YES'):
        setattr(wx, name, 1)
    wx.MessageBox = lambda *args: 0
    pkg = ModuleType('shell_probe')
    pkg.__path__ = []
    mods = {'wx': wx, 'shell_probe': pkg}
    for path, name in [('paged_search_panel', 'PagedSearchPanel'), ('catalog_panel', 'CatalogPanel'),
                       ('topic_panel', 'TopicPanel'), ('evidence_panel', 'EvidencePanel')]:
        mod = ModuleType('shell_probe.' + path)
        setattr(mod, name, Child)
        mods[mod.__name__] = mod
    source = Path(__file__).resolve().parent / 'workspace_shell.py'
    if not source.exists():
        source = Path(__file__).resolve().parents[2] / 'src/carr_library/ui/workspace_shell.py'
    spec = importlib.util.spec_from_file_location('shell_probe.workspace_shell', source)
    mod = importlib.util.module_from_spec(spec)
    mods[spec.name] = mod
    with patch.dict(sys.modules, mods):
        spec.loader.exec_module(mod)
    return mod


M = load()


class Tests(unittest.TestCase):
    def setUp(self):
        self.scopes = [('a', 't', 'Same title'), ('b', 't', 'Same title'), ('b', None, 'Library')]
        M.wx.MessageBox = lambda *args: 0
        self.f = M.WorkspaceFrame(None, lambda: self.scopes, self.bind)
    def bind(self, w, t):
        cb = lambda *args: None
        return M.ScopeServices(w, t, cb, cb, cb, cb, cb, cb, cb)
    def open(self, i):
        self.f.choice.SetSelection(i)
        self.f.open_selected()
    def test_tabs_and_identity(self):
        self.open(0)
        self.assertEqual(self.f.active, ('a', 't'))
        self.assertEqual([label for _, label in self.f.book.pages], ['Search', 'Books and Collections', 'Topics and Sections', 'Evidence and Notes'])
        self.assertEqual(self.f.book.pages[0][0].workspace, 'a')
        self.assertEqual(self.f.evidence.args[1:3], ('a', 't'))
    def test_dirty_cancel(self):
        self.open(0)
        book = self.f.book
        self.f.evidence.note.SetValue('draft')
        self.open(1)
        self.assertIs(self.f.book, book)
        self.assertFalse(book.destroyed)
        self.assertEqual(self.f.evidence.note.GetValue(), 'draft')
        self.assertTrue(self.f.evidence.note.focused)
    def test_dirty_confirm_switch(self):
        self.open(0)
        book = self.f.book
        self.f.evidence.note.SetValue('draft')
        M.wx.MessageBox = lambda *args: M.wx.YES
        self.open(1)
        self.assertTrue(book.destroyed and book.hidden)
        self.assertEqual(self.f.active, ('b', 't'))
        self.assertEqual(self.f.evidence.note.GetValue(), 'saved')
    def test_library_only(self):
        self.open(2)
        self.assertEqual(self.f.active, ('b', None))
        self.assertIsNone(self.f.evidence)
        self.assertEqual(len(self.f.book.pages), 3)
    def test_revoked(self):
        self.open(0)
        book = self.f.book
        self.scopes.pop(0)
        self.f.refresh_scopes()
        self.assertTrue(book.destroyed)
        self.assertIsNone(self.f.active)
        self.assertIn('Access changed', self.f.status.value)
    def test_mismatched_binding(self):
        self.f.bind_scope = lambda *args: self.bind('b', 't')
        self.open(0)
        self.assertIsNone(self.f.book)
        self.assertIn('unavailable', self.f.status.value)
    def test_constructor_failure(self):
        created = []
        def notebook(*args):
            b = Control()
            created.append(b)
            return b
        with patch.object(M.wx, 'Notebook', notebook), patch.object(M, 'TopicPanel', side_effect=ValueError):
            self.open(0)
        self.assertTrue(created[0].destroyed)
        self.assertIsNone(self.f.active)
    def test_dirty_close_veto(self):
        self.open(0)
        self.f.evidence.note.SetValue('draft')
        e = SimpleNamespace(CanVeto=lambda: True, veto=False, skipped=False)
        e.Veto = lambda: setattr(e, 'veto', True)
        e.Skip = lambda: setattr(e, 'skipped', True)
        self.f.on_close(e)
        self.assertTrue(e.veto)
        self.assertFalse(e.skipped)
    def test_duplicate_ids(self):
        self.open(0)
        book = self.f.book
        self.scopes.append(self.scopes[0])
        self.f.refresh_scopes()
        self.assertTrue(book.destroyed)
        self.assertEqual(self.f.scopes, [])


if __name__ == '__main__':
    unittest.main()
