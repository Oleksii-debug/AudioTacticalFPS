"""Execute the actual reader closure with native UTF-16 positions and real spell reviews."""
import ast
from pathlib import Path
import runpy
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.spellcheck import review_spelling
from carr_library.ui import spellcheck_dialog as ui
from carr_library.ui import speech_dialog as reader_ui

dictionary = runpy.run_path(str(Path(__file__).with_name('test_spellcheck.py')))['dictionary']


def reader_hook(state):
    source = ast.parse(Path(reader_ui.__file__).read_text(encoding='utf-8'))
    reader = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'show_speech_reader')
    hook = next(node for node in reader.body if isinstance(node, ast.FunctionDef) and node.name == 'spelling')
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
    def with_recovery_warning(value): return value
    def run(*args, **kwargs): raise AssertionError('No additional reader worker')
    def placeholder(): pass
    return spelling
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
    selected = dictionary(tmp_path)
    original = '😀 retained\r\nhello wurld world'
    normalize = getattr(request, 'param', False)
    class Editor:
        def __init__(self, value): self.ChangeValue(value)
        def at(self, index): return len(self.value[:index].encode('utf-16-le')) // 2
        def GetValue(self): return self.value
        def GetSelection(self): return self.selection
        def GetInsertionPoint(self): return self.selection[0]
        def GetRange(self, start, end):
            return self.value.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')
        def SetFocus(self): self.focused = True
        def ChangeValue(self, value):
            self.value = value.replace('\r\n', '\n') if normalize else value
            self.selection = (0, 0)
    editor = Editor(original)
    start = original.index('wurld')
    displayed_start = editor.value.index('wurld')
    editor.selection = (editor.at(displayed_start), editor.at(displayed_start + 5))
    state = SimpleNamespace(parent=SimpleNamespace(library=SimpleNamespace(root=root), _closing=False),
        document=ReadingDocument(original), editor=editor, busy=False, source_loaded=True,
        authorized=True, save_failure=False, saved=[], messages=[], stops=[], reviews=[],
        selected=selected, capabilities=0, driver=lambda **kwargs: None)
    state.live = SimpleNamespace(core=SimpleNamespace(busy=False), stop=lambda: state.stops.append(True),
        capabilities=lambda: setattr(state, 'capabilities', state.capabilities + 1))
    class Review:
        def __init__(self, callbacks): self.callbacks, self.alive, self.destroyed = callbacks, True, False
        def ShowModal(self): state.driver(**self.callbacks)
        def __bool__(self): return not self.destroyed
        def finish_close(self): self.alive = False
        def Destroy(self): self.destroyed = True
    def create(parent, **kwargs):
        result = Review(kwargs); state.reviews.append(result); return result
    monkeypatch.setattr(ui, 'create_spellcheck_dialog', create)
    return state, original, start


def proposal(state, capture, library_root):
    snapshot = capture()
    start, end = snapshot.selection
    review = review_spelling(snapshot.text[start:end], state.selected, library_root=library_root)
    return ui.preview_spelling(snapshot, review, (start, end), 0, 'world')


@pytest.mark.parametrize('reader', [False, True], indirect=True, ids=['retained-crlf', 'native-lf'])
def test_real_reader_hook_preserves_native_unicode_scope_source_history_and_recovery(reader):
    state, original, start = reader
    def exercise(*, capture, apply, undo, library_root, **kwargs):
        assert capture().selection == (start, start + 5)
        assert state.editor.at(1) != 1
        preview = proposal(state, capture, library_root)
        applied, warning = apply(preview)
        assert warning == '' and state.document.text == original.replace('wurld', 'world')
        assert state.document.source_text == original and state.document.revision == 1
        assert undo(applied) == '' and state.document.text == original
    state.driver = exercise
    reader_hook(state)()
    assert not state.messages and len(state.saved) == len(state.stops) == 2
    assert state.reviews[0].destroyed and state.editor.focused and state.capabilities == 1


@pytest.mark.parametrize('changed', ['library', 'root', 'closing', 'source', 'selection'])
def test_real_hook_reauthorizes_source_library_and_exact_selection_before_apply(reader, tmp_path, changed):
    state, original, _ = reader
    def exercise(*, capture, apply, library_root, **kwargs):
        preview = proposal(state, capture, library_root)
        if changed == 'library': state.parent.library = SimpleNamespace(root=library_root)
        elif changed == 'root':
            other = tmp_path / 'other'; other.mkdir(); state.parent.library.root = other
        elif changed == 'closing': state.parent._closing = True
        elif changed == 'source': state.authorized = False
        else: state.editor.selection = (0, 0)
        before = state.document.to_dict()
        with pytest.raises(ValueError): apply(preview)
        assert state.document.to_dict() == before
    state.driver = exercise
    reader_hook(state)()
    assert state.document.source_text == original and not state.saved and not state.messages


def test_actual_stop_callback_cannot_revoke_source_then_allow_a_spelling_edit(reader):
    state, _, _ = reader
    def exercise(*, capture, apply, library_root, **kwargs):
        preview = proposal(state, capture, library_root)
        state.live.stop = lambda: setattr(state, 'authorized', False)
        before = state.document.to_dict()
        with pytest.raises(ValueError): apply(preview)
        assert state.document.to_dict() == before
    state.driver = exercise
    reader_hook(state)()
    assert not state.saved and not state.messages


def test_real_hook_returns_optional_recovery_warning_after_edit_and_keeps_undo(reader):
    state, original, _ = reader
    state.save_failure = True
    def exercise(*, capture, apply, undo, library_root, **kwargs):
        preview = proposal(state, capture, library_root)
        applied, warning = apply(preview)
        assert 'in memory' in warning and 'Save As' in warning
        assert state.document.text != original
        assert 'recovery save failed' in undo(applied)
        assert state.document.text == original
    state.driver = exercise
    reader_hook(state)()
    assert not state.saved and not state.messages


@pytest.mark.parametrize('busy,loaded', [(True, True), (False, False)])
def test_spelling_hook_refuses_busy_or_unloaded_reader(reader, busy, loaded):
    state, _, _ = reader
    state.busy, state.source_loaded = busy, loaded
    reader_hook(state)()
    assert not state.reviews and 'Wait for the reader operation' in state.messages[-1]


def test_reader_registers_accessible_spelling_action():
    source = ast.parse(Path(reader_ui.__file__).read_text(encoding='utf-8'))
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'button'
        and len(node.args) == 3 and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == 'Review &spelling' and isinstance(node.args[1], ast.Name)
        and node.args[1].id == 'spelling' for node in ast.walk(source))
