"""Execute the real reader callbacks over Unicode text and durable recovery."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.speech.navigation import native_insertion_offset
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore
from carr_library.ui import speech_dialog as implementation


class NativeEditor:
    def __init__(self, text):
        self.value, self.selection, self.cursor = text, (0, 0), 0
        self.focused = False

    def GetValue(self): return self.value
    def GetSelection(self): return self.selection
    def GetInsertionPoint(self): return self.cursor
    def GetLastPosition(self): return len(self.value.encode('utf-16-le')) // 2
    def GetRange(self, start, end):
        return self.value.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')
    def GetStringSelection(self): return self.GetRange(*self.selection)
    def SetSelection(self, start, end): self.selection, self.cursor = (start, end), end
    def ShowPosition(self, value): pass
    def SetFocus(self): self.focused = True
    def place(self, value):
        position = len(self.value[:value].encode('utf-16-le')) // 2
        self.SetSelection(position, position)


def callbacks(state):
    tree = ast.parse(Path(implementation.__file__).read_text(encoding='utf-8'))
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'show_speech_reader')
    names = {'navigate_unit', 'selected_text', 'save_recovery', 'check_current'}
    methods = [node for node in reader.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == names
    template = ast.parse("""
def factory(state):
    parent = state.parent
    library = parent.library
    library_root = Path(library.root)
    information = regular(local_path(library_root, exists=True), directory=True)
    library_identity = information.st_dev, information.st_ino
    alive = True
    busy = state.busy
    source_loaded = state.source_loaded
    editor, document = state.editor, state.document
    navigation_unit = SimpleNamespace(GetSelection=lambda: state.unit)
    draft_store = state.store
    last_audio = None
    last_recovery_signature = None
    voices = ()
    voice_choice = SimpleNamespace(GetSelection=lambda: -1)
    rate = SimpleNamespace(GetValue=lambda: 0)
    volume = SimpleNamespace(GetValue=lambda: 80)
    player = SimpleNamespace(position_seconds=0)
    def source_guard():
        state.guards += 1
        if not state.authorized:
            raise ValueError('Source permission was revoked.')
    def sync_document():
        document.set_text(editor.GetValue())
    def report(message, **kwargs):
        state.messages.append(message)
    def speak():
        state.spoken.append(editor.GetStringSelection())
    def placeholder():
        pass
    return navigate_unit, selected_text, save_recovery
""")
    body = template.body[0].body
    at = next(i for i, node in enumerate(body) if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')
    body[at:at + 1] = methods
    namespace = dict(implementation.__dict__, SimpleNamespace=SimpleNamespace)
    exec(compile(ast.fix_missing_locations(template), implementation.__file__, 'exec'), namespace)
    return namespace['factory'](state)


@pytest.fixture
def state(tmp_path):
    text = '😀 Prefix.\r\nДругий рядок. Last sentence.'
    return SimpleNamespace(parent=SimpleNamespace(library=SimpleNamespace(root=tmp_path), _closing=False),
        document=ReadingDocument(text), editor=NativeEditor(text),
        store=ReaderDraftStore(tmp_path), unit=0, busy=False, source_loaded=True,
        authorized=True, guards=0, spoken=[], messages=[])


def test_current_previous_next_select_without_speech_or_source_edits(state):
    state.editor.place(state.document.text.index('рядок'))
    navigate, _, _ = callbacks(state)
    before = state.document.to_dict()
    navigate()
    assert state.editor.GetStringSelection() == 'рядок' and state.editor.focused
    navigate(direction=-1)
    assert state.editor.GetStringSelection() == 'Другий'
    navigate(direction=1)
    assert state.editor.GetStringSelection() == 'рядок'
    assert not state.spoken and state.guards == 3 and state.document.to_dict() == before


def test_read_current_unit_is_an_explicit_separate_action(state):
    state.editor.place(state.document.text.index('Другий'))
    navigate, _, _ = callbacks(state)
    navigate(read_aloud=True)
    assert state.spoken == ['Другий'] and state.editor.GetStringSelection() == 'Другий'


@pytest.mark.parametrize('blocked', ['busy', 'source_loaded', 'authorized'])
def test_navigation_refuses_busy_unloaded_or_revoked_source_without_speaking(state, blocked):
    if blocked == 'busy':
        state.busy = True
    else:
        setattr(state, blocked, False)
    state.editor.place(3)
    before = state.editor.selection
    navigate, _, _ = callbacks(state)
    navigate(read_aloud=True)
    assert state.editor.selection == before and not state.spoken and state.messages


def test_invalid_unit_keeps_selection_and_produces_no_audio(state):
    state.unit = -1
    navigate, _, _ = callbacks(state)
    navigate(read_aloud=True)
    assert not state.spoken and 'Choose word' in state.messages[-1]


def test_from_cursor_and_autosave_use_observed_unicode_offsets(state):
    offset = state.document.text.index('Другий')
    state.editor.place(offset)
    _, selected, save = callbacks(state)
    assert state.editor.GetInsertionPoint() != offset
    assert selected(True) == state.document.text[offset:]
    assert save(force=True)
    recovered = state.store.load()
    assert recovered.cursor == offset
    assert recovered.document.source_text == state.document.source_text
    assert native_insertion_offset(state.editor, recovered.document.text) == offset


def test_a_finished_navigation_does_not_wrap_or_start_full_document_speech(state):
    state.editor.place(state.document.text.rindex('sentence'))
    navigate, _, _ = callbacks(state)
    navigate()
    before = state.editor.selection
    navigate(direction=1, read_aloud=True)
    assert state.editor.selection == before and not state.spoken
    assert 'No further word' in state.messages[-1]


@pytest.mark.parametrize('changed', ['library', 'root', 'closed'])
def test_navigation_refuses_a_replaced_reader_scope_before_selection(state, tmp_path, changed):
    state.editor.place(3)
    before = state.editor.selection
    navigate, _, _ = callbacks(state)
    if changed == 'library':
        state.parent.library = SimpleNamespace(root=state.parent.library.root)
    elif changed == 'root':
        replacement = tmp_path / 'replacement'; replacement.mkdir()
        state.parent.library.root = replacement
    else:
        state.parent._closing = True
    navigate(read_aloud=True)
    assert state.editor.selection == before and not state.spoken and state.messages
