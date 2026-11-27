"""Exercise actual native-tool handlers and reader closure against real recovery."""
import ast
from importlib.util import find_spec
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.speech.marks import join_next_break, split_paragraph
from carr_library.speech.navigation import native_cursor_position, native_insertion_offset
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui import reading_tools


class Widget:
    def __init__(self, parent=None, *args, **kwargs):
        self.owner, self.parent, self.dead = get_ident(), parent, False
        self.value, self.selection, self.handlers = kwargs.get('value', ''), -1, {}
        self.rows, self.name, self.children = [], '', []
        if parent is not None and hasattr(parent, 'children'): parent.children.append(self)
    def native(self):
        assert get_ident() == self.owner and not self.dead
    def SetName(self, value): self.native(); self.name = value
    def Wrap(self, value): self.native()
    def Add(self, *args, **kwargs): self.native()
    def SetSizer(self, value): self.native()
    def SetFocus(self): self.native()
    def Bind(self, event, handler): self.native(); self.handlers[event] = handler
    def GetValue(self): self.native(); return self.value
    def ChangeValue(self, value): self.native(); self.value = value
    def Set(self, rows): self.native(); self.rows, self.selection = list(rows), -1
    def GetSelection(self): self.native(); return self.selection
    def SetSelection(self, value): self.native(); self.selection = value
    def EndModal(self, result): self.native()
    def Destroy(self): self.native(); self.dead = True


@pytest.fixture
def native():
    wx = SimpleNamespace(Dialog=Widget, TextCtrl=Widget, StaticText=Widget, ListBox=Widget,
        BoxSizer=Widget, Button=Widget, EVT_BUTTON='press', EVT_CLOSE='close', ID_CANCEL=2)
    for key in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
                'ALL', 'EXPAND', 'LEFT', 'RIGHT', 'TE_READONLY', 'TE_MULTILINE'):
        setattr(wx, key, 0)
    doc = ReadingDocument('First paragraph.\nSecond paragraph.')
    state = SimpleNamespace(document=doc, cursor=5, allowed=True, actions=[], stops=[])
    def guard():
        if not state.allowed: raise ValueError('Source scope changed.')
    def capture(): return reading_tools.ReadingToolSnapshot.capture(doc, state.cursor)
    def mutate(kind, value, snapshot):
        guard()
        assert snapshot.document is doc
        if snapshot.signature != doc._signature(): raise ValueError('Reading copy changed.')
        state.actions.append(kind)
        if kind == 'add': doc.add_bookmark(value, snapshot.cursor)
        elif kind == 'remove': doc.remove_bookmark(value)
        elif kind == 'split': state.cursor = split_paragraph(doc, snapshot.cursor)
        elif kind == 'join': state.cursor = join_next_break(doc, snapshot.cursor)
    def goto(identity, snapshot):
        guard()
        if snapshot.signature != doc._signature(): raise ValueError('Reading copy changed.')
        state.cursor = doc.bookmark_position(identity)
    state.dialog = reading_tools.create_reading_tools(object(), capture=capture,
        mutate=mutate, goto=goto, guard=guard, wx_module=wx)
    state.press = lambda name: state.dialog.buttons[name].handlers['press'](None)
    yield state
    state.dialog.alive = False
    state.dialog.Destroy()


def test_native_bookmark_add_go_to_and_late_callback_are_explicit(native):
    native.press('add')
    mark = native.document.bookmarks[0]
    assert mark.offset == 5 and native.dialog.rows == (mark,)
    native.cursor = 0
    native.dialog.list.SetSelection(0)
    native.press('goto')
    assert native.cursor == 5 and not native.dialog.alive
    before = native.document.to_dict()
    for action in ('add', 'remove', 'split', 'join', 'goto', 'close'):
        native.press(action)
    assert native.document.to_dict() == before


def test_native_changed_text_cannot_be_followed_as_the_old_bookmark(native):
    native.press('add')
    native.document.set_text('Other text with a different position.')
    native.dialog.refresh()
    assert 'text changed' in native.dialog.list.rows[0]
    native.dialog.list.SetSelection(0)
    native.press('goto')
    assert native.dialog.alive and native.cursor == 5
    assert 'text changed' in native.dialog.status.GetValue()
    native.press('remove')
    assert not native.document.bookmarks


def test_native_paragraph_actions_preserve_source_and_support_undo(native):
    original = native.document.source_text
    native.press('split')
    assert native.document.text == original[:5] + '\n\n' + original[5:]
    assert native.document.source_text == original and native.document.can_undo
    assert native.document.undo() and native.document.text == original
    native.dialog.refresh()
    native.press('join')
    assert native.document.text == 'First paragraph. Second paragraph.'
    assert native.document.undo() and native.document.text == original


def test_native_stale_or_revoked_scope_does_not_apply_a_queued_action(native):
    native.document.set_text('A newer reading copy.')
    before = native.document.to_dict()
    native.press('add')
    assert native.document.to_dict() == before and 'changed' in native.dialog.status.GetValue()
    native.dialog.refresh()
    native.allowed = False
    native.press('split')
    assert native.document.to_dict() == before and 'scope changed' in native.dialog.status.GetValue()


class Editor:
    def __init__(self, value): self.value, self.cursor, self.focus = value, 5, 0
    def GetValue(self): return self.value
    def GetRange(self, first, last): return self.value[first:last]
    def GetInsertionPoint(self): return self.cursor
    def GetLastPosition(self): return len(self.value)
    def SetInsertionPoint(self, value): self.cursor = value
    def ChangeValue(self, value): self.value = value
    def SetFocus(self): self.focus += 1


def reader_hook(state):
    path = Path(find_spec('carr_library.ui.speech_dialog').origin)
    module = ast.parse(path.read_text(encoding='utf-8'))
    outer = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == 'show_speech_reader')
    callback = next(n for n in outer.body if isinstance(n, ast.FunctionDef) and n.name == 'choose_reading_tools')
    factory = ast.parse("""
def factory(state):
    alive, busy, source_loaded = True, state.busy, True
    parent, dialog, document, editor = state.parent, object(), state.document, state.editor
    playback_scope, stop_for_reading_tab = state.guard, state.stop
    capabilities = state.capabilities
    def report(message, **kwargs): state.messages.append(message)
    def sync_document(): document.set_text(editor.GetValue())
    def save_recovery(**kwargs):
        state.saved.append(document.to_dict())
        state.store.save(ReaderResume(document, 'missing:exact', 0, 71, editor.cursor))
    def placeholder(): pass
    return choose_reading_tools
""")
    body = factory.body[0].body
    body[next(i for i, n in enumerate(body) if isinstance(n, ast.FunctionDef) and n.name == 'placeholder')] = callback
    namespace = dict(__package__='carr_library.ui', native_cursor_position=native_cursor_position,
                     native_insertion_offset=native_insertion_offset)
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), namespace)
    return namespace['factory'](state)


@pytest.mark.parametrize('action', ['add', 'split', 'join', 'goto', 'remove'])
def test_actual_reader_hook_mutates_and_saves_only_the_reviewed_reading_copy(tmp_path, monkeypatch, action):
    doc = ReadingDocument('First paragraph.\nSecond paragraph.')
    original = doc.source_text
    state = SimpleNamespace(document=doc, editor=Editor(doc.text), parent=SimpleNamespace(_closing=False),
        busy=False, guard=Mock(), stop=Mock(), capabilities=Mock(), saved=[], messages=[],
        store=ReaderDraftStore(tmp_path))
    mark = doc.add_bookmark('Existing', 3) if action in ('goto', 'remove') else None
    def show(_parent, **options):
        reviewed = options['capture']()
        if action == 'goto': options['goto'](mark.id, reviewed)
        else: options['mutate'](action, mark.id if mark else 'Native bookmark', reviewed)
    monkeypatch.setattr(reading_tools, 'show_reading_tools', show)
    reader_hook(state)()
    saved = state.store.load()
    assert saved.document.to_dict() == doc.to_dict() and saved.voice_id == 'missing:exact'
    assert doc.source_text == original and doc.source_anchor is None
    assert state.editor.focus == 1 and not state.messages
    if action in ('split', 'join'):
        state.stop.assert_called_once()
        assert doc.undo() and doc.text == original
    else:
        state.stop.assert_not_called()
    if action == 'goto': assert state.editor.cursor == 3


@pytest.mark.parametrize('failure', ['stop', 'source', 'changed', 'busy', 'close'])
def test_actual_reader_hook_vetoes_unsafe_paragraph_mutation(tmp_path, monkeypatch, failure):
    doc = ReadingDocument('First paragraph.\nSecond paragraph.')
    state = SimpleNamespace(document=doc, editor=Editor(doc.text),
        parent=SimpleNamespace(_closing=failure == 'close'), busy=failure == 'busy',
        guard=Mock(), stop=Mock(), capabilities=Mock(), saved=[], messages=[],
        store=ReaderDraftStore(tmp_path))
    if failure == 'stop': state.stop.side_effect = RuntimeError('Device close unconfirmed.')
    if failure == 'source': state.guard.side_effect = ValueError('Source scope revoked.')
    before = doc.to_dict()
    def show(_parent, **options):
        reviewed = options['capture']()
        if failure == 'changed':
            state.editor.value = 'Independently changed text.'
        options['mutate']('split', None, reviewed)
    monkeypatch.setattr(reading_tools, 'show_reading_tools', show)
    reader_hook(state)()
    assert state.messages and not state.saved and not state.store.path.exists()
    assert doc.source_text == before['source_text']
    if failure != 'changed': assert doc.to_dict() == before
    else: assert doc.text == state.editor.value and '\n\n' not in doc.text
