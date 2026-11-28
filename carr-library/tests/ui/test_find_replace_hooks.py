"""Exercise the actual find/replace reader closure with native UTF-16 positions."""
import ast
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.speech.reading import ReadingDocument, PronunciationRule
from carr_library.ui import find_replace_dialog as review_ui
from carr_library.ui import speech_dialog as reader_ui


def reader_hook(state):
    tree = ast.parse(Path(reader_ui.__file__).read_text(encoding='utf-8'))
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'show_speech_reader')
    names = {'find_replace', 'check_current'}
    hooks = [node for node in reader.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in hooks} == names
    template = ast.parse("""
def factory(state):
    parent = state.parent
    library = getattr(parent, 'library', None)
    library_root = Path(library.root) if library is not None else None
    library_identity = None
    if library_root is not None:
        information = regular(local_path(library_root, exists=True), directory=True)
        library_identity = information.st_dev, information.st_ino
    alive = True
    busy = state.busy
    source_loaded = state.source_loaded
    document = state.document
    editor = state.editor
    dialog = object()
    voices = state.voices
    voice_choice = state.voice_choice
    player = state.player
    progress = state.progress
    cancel = Event()
    last_audio = object()
    playback_generation = 10
    def source_guard():
        if not state.authorized:
            raise ValueError('The source scope is no longer available.')
    def sync_document():
        document.set_text(editor.GetValue())
    def save_recovery(*args, **kwargs):
        if state.save_failure:
            raise OSError('Synthetic recovery storage unavailable')
        state.saved.append(document.to_dict())
    def report(message, **kwargs):
        state.messages.append(message)
    def capabilities():
        state.capability_updates += 1
    def run(*args, **kwargs):
        raise AssertionError('The hook fixture cannot create another executor.')
    def placeholder():
        pass
    def playback():
        return last_audio, playback_generation
    return find_replace, playback
""")
    body = template.body[0].body
    index = next(i for i, node in enumerate(body) if isinstance(node, ast.FunctionDef)
                 and node.name == 'placeholder')
    body[index:index + 1] = hooks
    namespace = dict(reader_ui.__dict__, Event=Event)
    exec(compile(ast.fix_missing_locations(template), reader_ui.__file__, 'exec'), namespace)
    return namespace['factory'](state)


@pytest.fixture
def reader(tmp_path, monkeypatch):
    root = tmp_path / 'library'; root.mkdir()
    original = '😀 retained prefix\r\nDr. First. Dr. Second.'
    document = ReadingDocument(original)
    python_start = original.rindex('Dr.')
    native_start = len(original[:python_start].encode('utf-16-le')) // 2
    class NativeEditor:
        def __init__(self):
            self.value = original
            self.selection = (native_start, native_start + 3)
            self.focused = False
        def GetValue(self): return self.value
        def GetSelection(self): return self.selection
        def GetLastPosition(self): return len(self.value.encode('utf-16-le')) // 2
        def SetSelection(self, start, end): self.selection = start, end
        def ShowPosition(self, position): self.shown = position
        def GetRange(self, start, end):
            return self.value.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')
        def ChangeValue(self, value):
            self.value, self.selection = value, (0, 0)
        def SetFocus(self): self.focused = True
    choices = SimpleNamespace(index=0)
    choices.GetSelection = lambda: choices.index
    state = SimpleNamespace(parent=SimpleNamespace(library=SimpleNamespace(root=root), _closing=False),
        busy=False, source_loaded=True, document=document, editor=NativeEditor(),
        voices=(SimpleNamespace(id='voice:one', language='en'), SimpleNamespace(id='voice:two', language='uk')),
        voice_choice=choices, authorized=True, save_failure=False, saved=[], messages=[],
        capability_updates=0, stops=[], refreshed=[], reviews=[], driver=lambda **kwargs: None)
    state.player = SimpleNamespace(stop=lambda: state.stops.append(True))
    state.progress = SimpleNamespace(refresh_audio_state=lambda: state.refreshed.append(True))
    class Review:
        def __init__(self, callbacks):
            self.callbacks, self.alive, self.destroyed = callbacks, True, False
        def on_load(self): pass
        def ShowModal(self):
            state.driver(**self.callbacks)
        def on_close(self):
            self.alive = False
        def Destroy(self):
            self.destroyed = True
    def make_review(parent, **kwargs):
        instance = Review(kwargs)
        state.reviews.append(instance)
        return instance
    monkeypatch.setattr(review_ui, 'create_find_dialog', make_review)
    return state, original, python_start, native_start


def test_reader_find_hook_converts_unicode_matches_and_keeps_one_undoable_source_copy(reader):
    state, original, python_start, native_start = reader
    assert native_start != python_start
    def exercise(*, capture, select, apply, undo, guard, **kwargs):
        snapshot = capture()
        assert snapshot.selection == (python_start, python_start + 3)
        proposal = review_ui.preview_find(snapshot, PronunciationRule('Dr.', 'Doctor'),
            selection_only=True)
        highlighted = select(proposal, 0)
        assert highlighted.selection == (python_start, python_start + 3)
        assert state.editor.selection == (native_start, native_start + 3)
        assert state.editor.shown == native_start + 3 and not state.editor.focused
        proposal = replace(proposal, snapshot=highlighted)
        applied, warning = apply(proposal, 0)
        assert warning == ''
        assert state.document.text == original[:python_start] + 'Doctor' + original[python_start + 3:]
        assert state.document.revision == 1 and state.document.source_text == original
        assert state.saved[-1]['source_text'] == original
        assert undo(applied) == ''
        assert state.document.text == original
    state.driver = exercise
    command, playback = reader_hook(state)
    command()
    assert state.editor.GetValue() == original and state.editor.focused
    assert len(state.stops) == len(state.refreshed) == len(state.saved) == 2
    assert playback() == (None, 12)
    assert not state.messages and state.reviews[0].destroyed


def test_recovery_write_failure_keeps_applied_revision_undoable_and_reports_it(reader):
    state, original, _, _ = reader
    state.save_failure = True
    def exercise(*, capture, apply, undo, **kwargs):
        proposal = review_ui.preview_find(capture(), PronunciationRule('Dr.', 'Doctor'),
            selection_only=True)
        applied, warning = apply(proposal, None)
        assert 'in memory' in warning and 'Save As' in warning
        assert state.document.text != original and state.document.revision == 1
        warning = undo(applied)
        assert 'recovery save failed' in warning and state.document.text == original
    state.driver = exercise
    command, _ = reader_hook(state); command()
    assert not state.messages and not state.saved


@pytest.mark.parametrize('changed', ['library', 'root', 'closed', 'source'])
def test_deferred_reader_scope_rejects_changed_authority_before_apply(reader, tmp_path, changed):
    state, original, _, _ = reader
    def exercise(*, capture, apply, guard, **kwargs):
        proposal = review_ui.preview_find(capture(), PronunciationRule('Dr.', 'Doctor'),
            selection_only=True)
        if changed == 'library':
            state.parent.library = SimpleNamespace(root=state.parent.library.root)
        elif changed == 'root':
            moved = tmp_path / 'different'; moved.mkdir()
            state.parent.library.root = moved
        elif changed == 'closed':
            state.parent._closing = True
        else:
            state.authorized = False
        with pytest.raises(ValueError):
            apply(proposal, None)
    state.driver = exercise
    command, _ = reader_hook(state); command()
    assert state.document.text == original and not state.saved and not state.stops
    assert not state.editor.focused and state.capability_updates == 0


@pytest.mark.parametrize('busy,loaded', [(True, True), (False, False)])
def test_reader_operation_or_unloaded_source_cannot_open_review(reader, busy, loaded):
    state, original, _, _ = reader
    state.busy, state.source_loaded = busy, loaded
    command, _ = reader_hook(state); command()
    assert not state.reviews and state.document.text == original
    assert 'Wait for the current reader operation' in state.messages[-1]

def test_find_hook_refuses_a_changed_selection_before_apply_or_undo(reader):
    state, original, python_start, native_start = reader
    def exercise(*, capture, apply, undo, **kwargs):
        proposal = review_ui.preview_find(capture(), PronunciationRule('Dr.', 'Doctor'),
                                         selection_only=True)
        state.editor.SetSelection(0, 0)
        with pytest.raises(ValueError, match='changed'):
            apply(proposal, None)
        assert state.document.text == original and not state.stops
        state.editor.SetSelection(native_start, native_start + 3)
        applied, warning = apply(proposal, None)
        changed = state.document.text
        state.editor.SetSelection(2, 2)
        with pytest.raises(ValueError, match='changed'):
            undo(applied)
        assert state.document.text == changed and len(state.saved) == 1
    state.driver = exercise
    command, _ = reader_hook(state); command()
    assert not state.messages and state.reviews[0].destroyed


def test_find_hook_manual_reader_uses_existing_copy_without_a_library(reader):
    state, original, _, _ = reader
    state.parent = SimpleNamespace(_closing=False)
    def exercise(*, capture, apply, **kwargs):
        preview = review_ui.preview_find(capture(), PronunciationRule('Dr.', 'Doctor'))
        result, warning = apply(preview, None)
        assert not warning and result.text == original.replace('Dr.', 'Doctor')
    state.driver = exercise
    command, _ = reader_hook(state); command()
    assert state.document.source_text == original and state.document.revision == 1
    assert not state.messages and state.reviews[0].destroyed


@pytest.mark.parametrize('changed', ['library', 'root', 'closed'])
def test_find_does_not_open_after_its_original_reader_scope_changes(reader, tmp_path, changed):
    state, original, _, _ = reader
    command, _ = reader_hook(state)
    if changed == 'library':
        state.parent.library = SimpleNamespace(root=state.parent.library.root)
    elif changed == 'root':
        replacement = tmp_path / 'replacement'; replacement.mkdir()
        state.parent.library.root = replacement
    else:
        state.parent._closing = True
    command()
    assert not state.reviews and state.document.text == original and not state.stops
    assert state.messages and ('changed' in state.messages[-1] or 'closing' in state.messages[-1])
