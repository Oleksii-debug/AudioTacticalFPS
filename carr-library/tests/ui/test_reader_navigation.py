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
    names = {'navigate_unit', 'selected_text', 'save_recovery'}
    methods = [node for node in reader.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in methods} == names
    template = ast.parse("""
def factory(state):
    alive = True
    busy = state.busy
    source_loaded = state.source_loaded
    editor, document = state.editor, state.document
    navigation_unit = SimpleNamespace(GetSelection=lambda: state.unit)
    draft_store = state.store
    last_audio = None
    last_recovery_signature = None
    recovered, remembered_voice_id = None, ''
    reader_tabs, save_path = None, None
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
    return SimpleNamespace(document=ReadingDocument(text), editor=NativeEditor(text),
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


def _native_pronunciation_navigation(state, tmp_path, original):
    """Execute the probe's actual completion observer against durable recovery."""
    script = Path(__file__).resolve().parents[2] / 'scripts' / 'windows_pronunciation_smoke.py'
    tree = ast.parse(script.read_text(encoding='utf-8'), filename=str(script))
    method, = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and node.name == 'navigation_complete']
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    source = tmp_path / 'original-source.txt'
    source.write_bytes(original.encode('utf-8'))
    observed = {'editor': state.editor, 'navigation_initial': original}
    calls, finished, checks = [], [], []

    def check(label, value):
        assert value, label
        checks.append(label)

    def until(predicate, complete, *, timeout):
        calls.append((predicate, complete, timeout))

    namespace = dict(observed=observed, check=check, original_asset=source,
        asset_bytes=source.read_bytes(), ReaderDraftStore=ReaderDraftStore,
        library=SimpleNamespace(root=tmp_path), until=until,
        finish_after_cursor_saved=lambda: finished.append(True))
    exec(compile(module, str(script), 'exec'), namespace)
    return namespace['navigation_complete'], observed, source, calls, finished, checks


@pytest.mark.parametrize('saved', ['start', 'end', 'missing'])
def test_native_pronunciation_cursor_observer_requires_actual_windows_insertion_start(
        state, tmp_path, saved):
    original = '😀 Retained source.\r\nDr. First speaks. Dr. Second listens.'

    class WindowsEditor(NativeEditor):
        def SetSelection(self, start, end):
            # Pinned wx MSW GetInsertionPoint returns the selection's start.
            self.selection, self.cursor = (start, end), start

    state.document = ReadingDocument(original)
    state.editor = WindowsEditor(original)
    state.store = ReaderDraftStore(tmp_path, slot='pronunciation-native')
    state.editor.place(original.rindex('Second'))
    navigate, _, save = callbacks(state)
    navigate()
    assert state.editor.GetStringSelection() == 'Second'
    start, end = state.editor.GetSelection()
    assert start != original.rindex('Second')  # Native UTF-16 is not a Python offset.
    if saved != 'missing':
        state.editor.cursor = start if saved == 'start' else end
        assert save(force=True)
    state.editor.cursor = start
    complete, observed, source, calls, finished, checks = _native_pronunciation_navigation(
        state, tmp_path, original)
    complete()
    assert observed['expected_cursor'] == original.rindex('Second')
    assert len(calls) == 1 and calls[0][2] == 10
    assert calls[0][0]() is (saved == 'start')
    assert not finished and len(checks) == 2
    if saved == 'start':
        calls[0][1]()
        assert finished == [True]
        recovered = state.store.load()
        assert recovered.cursor == original.rindex('Second')
        assert recovered.document.source_text == original
    assert source.read_bytes() == original.encode('utf-8')
    assert state.editor.GetValue() == original and not state.spoken


@pytest.mark.parametrize('changed', ['insertion-end', 'prefix', 'source'])
def test_native_pronunciation_cursor_observer_refuses_unproved_native_or_source_state(
        state, tmp_path, changed):
    original = '😀 Retained source.\r\nDr. Second listens.'

    class WindowsEditor(NativeEditor):
        def SetSelection(self, start, end):
            self.selection, self.cursor = (start, end), start

    state.document = ReadingDocument(original)
    state.editor = WindowsEditor(original)
    state.editor.place(original.rindex('Second'))
    navigate, _, _ = callbacks(state)
    navigate()
    complete, _, source, calls, finished, _ = _native_pronunciation_navigation(
        state, tmp_path, original)
    start, end = state.editor.GetSelection()
    if changed == 'insertion-end':
        state.editor.cursor = end
    elif changed == 'prefix':
        actual_range = state.editor.GetRange
        state.editor.GetRange = lambda first, last: (
            'Changed prefix' if (first, last) == (0, start) else actual_range(first, last))
    else:
        source.write_bytes(b'Changed retained source')
    with pytest.raises(AssertionError):
        complete()
    assert not calls and not finished and not state.spoken
