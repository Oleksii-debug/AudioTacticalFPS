"""Execute the installed reader's actual Find/Replace closure with native offsets."""
import ast
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.ui import find_replace_dialog as search_ui
from carr_library.ui import speech_dialog as reader_ui

def reader_hook(state):
    source = ast.parse(Path(reader_ui.__file__).read_text(encoding='utf-8'))
    reader = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'show_speech_reader')
    hook = next(node for node in reader.body if isinstance(node, ast.FunctionDef) and node.name == 'find_replace')
    template = ast.parse('''
def factory(state):
    parent, document, editor = state.parent, state.document, state.editor
    alive, busy, source_loaded = True, state.busy, state.source_loaded
    dialog, cancel = object(), Event()
    live = state.live
    displayed = {'value': editor.GetValue(), 'text': document.text}
    def source_guard():
        if not state.authorized: raise ValueError('Source is no longer available.')
    def sync_document():
        capture_document_text(document, editor.GetValue(), displayed_value=displayed['value'],
                              displayed_text=displayed['text'])
    def display_document():
        editor.ChangeValue(document.text)
        displayed.update(value=editor.GetValue(), text=document.text)
        sync_document()
    def save_recovery(*args, **kwargs):
        if state.save_failure: raise OSError('Recovery storage unavailable')
        state.saved.append(document.to_dict())
    def report(value, **kwargs): state.messages.append(value)
    def with_recovery_warning(value): return value + state.recovery_warning
    def run(*args, **kwargs): raise AssertionError('No additional reader worker')
    def placeholder(): pass
    def playback(): return len(state.stops), live.core.busy
    return find_replace, playback
''')
    body = template.body[0].body
    index = next(i for i, node in enumerate(body) if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')
    body[index] = hook
    namespace = dict(reader_ui.__dict__, Event=Event)
    exec(compile(ast.fix_missing_locations(template), reader_ui.__file__, 'exec'), namespace)
    return namespace['factory'](state)


@pytest.fixture
def reader(tmp_path, monkeypatch, request):
    root = tmp_path / 'library'; root.mkdir()
    original = '😀 retained\r\nDr. First. Dr. Second.'
    normalize = getattr(request, 'param', False)
    class Editor:
        def __init__(self, value): self.ChangeValue(value)
        def at(self, index): return len(self.value[:index].encode('utf-16-le')) // 2
        def GetValue(self): return self.value
        def GetLastPosition(self): return self.at(len(self.value))
        def GetSelection(self): return self.selection
        def GetInsertionPoint(self): return self.selection[0]
        def GetRange(self, start, end):
            return self.value.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')
        def GetStringSelection(self): return self.GetRange(*self.selection)
        def SetSelection(self, start, end): self.selection = (start, end)
        def SetFocus(self): self.focused = True
        def ChangeValue(self, value):
            self.value = value.replace('\r\n', '\n') if normalize else value
            self.selection = (0, 0)
    editor = Editor(original)
    start = original.rindex('Dr.')
    displayed_start = editor.value.rindex('Dr.')
    editor.selection = (editor.at(displayed_start), editor.at(displayed_start + 3))
    state = SimpleNamespace(parent=SimpleNamespace(library=SimpleNamespace(root=root), _closing=False),
        document=ReadingDocument(original), editor=editor, busy=False, source_loaded=True,
        authorized=True, save_failure=False, saved=[], messages=[], stops=[], refreshes=[], reviews=[],
        recovery_warning='', capabilities=0,
        driver=lambda **kwargs: None)
    state.player = SimpleNamespace(stop=lambda: state.stops.append(True))
    state.progress = SimpleNamespace(refresh_audio_state=lambda: state.refreshes.append(True))
    state.live = SimpleNamespace(core=SimpleNamespace(busy=False), stop=state.player.stop,
        capabilities=lambda: setattr(state, 'capabilities', state.capabilities + 1))
    class Review:
        def __init__(self, callbacks):
            self.callbacks, self.alive, self.destroyed = callbacks, True, False
        def ShowModal(self): state.driver(**self.callbacks)
        def __bool__(self): return not self.destroyed
        def finish_close(self): self.alive = False
        def Destroy(self): self.destroyed = True
    def create(parent, **kwargs):
        result = Review(kwargs); state.reviews.append(result); return result
    monkeypatch.setattr(search_ui, 'create_find_replace_dialog', create)
    return state, original, start


@pytest.mark.parametrize('reader', [False, True], indirect=True, ids=['retained-crlf', 'native-lf'])
def test_real_hook_selects_utf16_crlf_match_then_applies_and_undoes_one_edit(reader):
    state, original, start = reader
    def exercise(*, capture, select, apply, undo, **kwargs):
        captured = capture()
        assert captured.selection == (start, start + 3)
        assert state.editor.at(1) != 1, 'The fixture must use native UTF-16 positions.'
        result = search_ui.find_matches(captured, search_ui.FindQuery('Dr.', 'Доктор 😀'))
        first = result.matches[0][:2]
        select(captured, first)
        assert state.editor.GetStringSelection() == 'Dr.'
        assert state.document.text == captured.text
        current = capture()
        proposal = search_ui.preview_replacement(search_ui.find_matches(current,
            search_ui.FindQuery('Dr.', 'Доктор 😀')), index=0)
        revision = state.document.revision
        applied, warning = apply(proposal)
        assert warning == '' and state.document.revision == revision + 1
        assert state.document.text == captured.text.replace('Dr.', 'Доктор 😀', 1)
        assert state.document.source_text == original
        assert undo(applied) == '' and state.document.text == captured.text
    state.driver = exercise
    command, playback = reader_hook(state); command()
    assert not state.messages
    assert playback() == (2, False)
    assert len(state.saved) == len(state.stops) == 2 and not state.refreshes
    assert state.capabilities == 1
    assert state.reviews[0].destroyed and state.editor.focused


@pytest.mark.parametrize('changed', ['library', 'root', 'closing', 'source', 'selection'])
def test_hook_rechecks_current_source_before_select_and_apply(reader, tmp_path, changed):
    state, original, _ = reader
    def exercise(*, capture, select, apply, **kwargs):
        captured = capture()
        proposal = search_ui.preview_replacement(search_ui.find_matches(captured, search_ui.FindQuery('Dr.', 'Doctor')))
        if changed == 'library':
            state.parent.library = SimpleNamespace(root=state.parent.library.root)
        elif changed == 'root':
            other = tmp_path / 'other'; other.mkdir(); state.parent.library.root = other
        elif changed == 'closing':
            state.parent._closing = True
        elif changed == 'source':
            state.authorized = False
        else:
            state.editor.selection = (0, 0)
        before = state.document.to_dict()
        with pytest.raises(ValueError): select(captured, (0, 1))
        with pytest.raises(ValueError): apply(proposal)
        assert state.document.to_dict() == before
    state.driver = exercise
    reader_hook(state)[0]()
    assert state.document.source_text == original and not state.saved and not state.messages


def test_hook_reports_recovery_failure_after_committed_edit_and_keeps_undo(reader):
    state, original, _ = reader
    state.save_failure = True
    def exercise(*, capture, apply, undo, **kwargs):
        captured = capture()
        proposal = search_ui.preview_replacement(search_ui.find_matches(captured, search_ui.FindQuery('Dr.', 'Doctor')))
        applied, warning = apply(proposal)
        assert 'in memory' in warning and 'Save As' in warning
        assert state.document.text != captured.text
        assert 'recovery save failed' in undo(applied)
        assert state.document.text == captured.text
    state.driver = exercise
    reader_hook(state)[0]()
    assert state.document.source_text == original and not state.saved and not state.messages


@pytest.mark.parametrize('busy,loaded', [(True, True), (False, False)])
def test_hook_refuses_busy_or_unloaded_reader(reader, busy, loaded):
    state, _, _ = reader
    state.busy, state.source_loaded = busy, loaded
    reader_hook(state)[0]()
    assert not state.reviews and 'Wait for the reader operation' in state.messages[-1]


def test_reader_registers_find_keys_and_accessible_button():
    source = ast.parse(Path(reader_ui.__file__).read_text(encoding='utf-8'))
    reader = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'button'
               and len(node.args) == 3 and isinstance(node.args[0], ast.Constant)
               and node.args[0].value == '&Find and replace'
               and isinstance(node.args[1], ast.Name) and node.args[1].id == 'find_replace'
               for node in ast.walk(reader))
    keys = []
    for node in ast.walk(reader):
        if (isinstance(node, ast.Tuple) and len(node.elts) == 3
                and isinstance(node.elts[2], ast.Name) and node.elts[2].id == 'find_replace'
                and isinstance(node.elts[1], ast.Call)):
            keys.append(ast.literal_eval(node.elts[1].args[0]))
    assert set(keys) == {'F', 'H'}
