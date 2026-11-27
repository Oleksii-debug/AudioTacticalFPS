"""Exercise the actual reader pronunciation closure with native UTF-16 positions."""
import ast
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from carr_library.speech.reading import ReadingDocument, PronunciationRule
from carr_library.ui import pronunciation_dialog as review_ui
from carr_library.ui import speech_dialog as reader_ui


def reader_hook(state):
    tree = ast.parse(Path(reader_ui.__file__).read_text(encoding='utf-8'))
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'show_speech_reader')
    hook = next(node for node in reader.body if isinstance(node, ast.FunctionDef)
                and node.name == 'pronunciations')
    template = ast.parse("""
def factory(state):
    parent = state.parent
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
    navigation_host = None
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
    return pronunciations, playback
""")
    body = template.body[0].body
    index = next(i for i, node in enumerate(body) if isinstance(node, ast.FunctionDef)
                 and node.name == 'placeholder')
    body[index] = hook
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
        def finish_close(self):
            self.alive = False
        def Destroy(self):
            self.destroyed = True
    def make_review(parent, **kwargs):
        instance = Review(kwargs)
        state.reviews.append(instance)
        return instance
    monkeypatch.setattr(review_ui, 'create_pronunciation_dialog', make_review)
    return state, original, python_start, native_start


def test_reader_hook_converts_unicode_native_range_and_keeps_one_undoable_source_copy(reader):
    state, original, python_start, native_start = reader
    assert native_start != python_start
    def exercise(*, capture, apply, undo, guard, **kwargs):
        snapshot = capture()
        assert snapshot.selection == (python_start, python_start + 3)
        proposal = review_ui.preview_pronunciations(snapshot, (PronunciationRule('Dr.', 'Doctor'),),
            selection_only=True, language='en')
        applied, warning = apply(proposal)
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
        proposal = review_ui.preview_pronunciations(capture(), (PronunciationRule('Dr.', 'Doctor'),),
            selection_only=True, language='en')
        applied, warning = apply(proposal)
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
        proposal = review_ui.preview_pronunciations(capture(), (PronunciationRule('Dr.', 'Doctor'),),
            selection_only=True, language='en')
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
            apply(proposal)
    state.driver = exercise
    command, _ = reader_hook(state); command()
    assert state.document.text == original and not state.saved and not state.stops


@pytest.mark.parametrize('busy,loaded', [(True, True), (False, False)])
def test_reader_operation_or_unloaded_source_cannot_open_review(reader, busy, loaded):
    state, original, _, _ = reader
    state.busy, state.source_loaded = busy, loaded
    command, _ = reader_hook(state); command()
    assert not state.reviews and state.document.text == original
    assert 'Wait for the reader operation' in state.messages[-1]
