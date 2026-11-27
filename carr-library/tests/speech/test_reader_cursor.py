"""Exact reader handlers with UTF-16/CRLF positions and real recovery records.

Native-control stand-ins establish the conversion contract, not Windows/NVDA
execution. The production helpers must verify actual GetRange prefixes.
"""
import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from carr_library.speech.reading import ReadingDocument
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui import speech_dialog as ui


class NativeEditor:
    def __init__(self, text, mode='utf16-crlf'):
        self.value = text.replace('\r\n', '\n').replace('\r', '\n')
        self.mode, self.cursor, self.selection = mode, 0, (0, 0)

    def wire(self, value):
        return value.replace('\n', '\r\n') if self.mode == 'utf16-crlf' else value

    def width(self, value):
        return len(value.encode('utf-16-le')) // 2 if self.mode.startswith('utf16') else len(value)

    def at(self, character_offset):
        return self.width(self.wire(self.value[:character_offset]))

    def GetValue(self): return self.value
    def GetInsertionPoint(self): return self.cursor
    def SetInsertionPoint(self, value): self.cursor = value
    def GetLastPosition(self): return self.width(self.wire(self.value))
    def ChangeValue(self, value):
        self.value = value.replace('\r\n', '\n').replace('\r', '\n')
        self.cursor = 0
    def GetRange(self, start, end):
        raw = self.wire(self.value)
        if self.mode.startswith('utf16'):
            raw = raw.encode('utf-16-le')[start * 2:end * 2].decode('utf-16-le')
        else:
            raw = raw[start:end]
        return raw.replace('\r\n', '\n')
    def GetStringSelection(self): return self.GetRange(*self.selection)
    def XYToPosition(self, column, row):
        lines = self.value.split('\n')
        if not 0 <= row < len(lines) or not 0 <= column <= self.width(lines[row]):
            return -1
        prefix = ''.join(line + '\n' for line in lines[:row])
        return self.width(self.wire(prefix)) + column


def reader_handlers(state):
    """Run the installed module's real closures without creating another UI."""
    tree = ast.parse(Path(ui.__file__).read_text(encoding='utf-8'))
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == 'show_speech_reader')
    names = {'sync_document', 'save_recovery', 'selected_text'}
    hooks = [node for node in reader.body if isinstance(node, ast.FunctionDef) and node.name in names]
    reload = next(node for node in reader.body if isinstance(node, ast.FunctionDef) and node.name == 'reload')
    hooks.append(next(node for node in reload.body if isinstance(node, ast.FunctionDef) and node.name == 'loaded'))
    template = ast.parse('''
def factory(state):
    document, editor, draft_store = state.document, state.editor, state.store
    alive, source_loaded, last_audio = True, True, None
    last_recovery_signature, recovered, registry = None, state.recovered, None
    voices, recovery_notice = (), ''
    source_status = SimpleNamespace(ChangeValue=lambda value: state.messages.append(value))
    voice_choice = SimpleNamespace(GetSelection=lambda: -1, Set=lambda values: None, SetSelection=lambda value: None)
    rate, volume = SimpleNamespace(GetValue=lambda: 0), SimpleNamespace(GetValue=lambda: 80)
    player = SimpleNamespace(position_seconds=0)
    def report(value, **kwargs): state.messages.append(value)
    def source_guard(): state.guards.append(True)
    def placeholder(): pass
    return SimpleNamespace(save=save_recovery, selected=selected_text, loaded=loaded)
''')
    body = template.body[0].body
    index = next(i for i, node in enumerate(body) if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')
    body[index:index + 1] = hooks
    namespace = dict(ui.__dict__, SimpleNamespace=SimpleNamespace)
    exec(compile(ast.fix_missing_locations(template), ui.__file__, 'exec'), namespace)
    return namespace['factory'](state)


def state_for(tmp_path, text, mode='utf16-crlf', *, document=None, recovered=None):
    return SimpleNamespace(document=document or ReadingDocument(text), editor=NativeEditor(text, mode),
        store=ReaderDraftStore(tmp_path), recovered=recovered, messages=[], guards=[])


@pytest.mark.parametrize('mode', ['utf16-crlf', 'utf16-lf', 'codepoint-lf'])
@pytest.mark.parametrize(('original', 'prefix', 'expected'), [
    ('😀A', '😀', 'A'),
    ('😀 first\r\nSecond 😀line.', '😀 first\nSecond 😀', 'line.'),
    ('One\rTwo\r\n😀Three', 'One\nTwo\n', '😀Three'),
])
def test_from_cursor_and_selection_use_exact_native_ranges(tmp_path, mode, original, prefix, expected):
    state = state_for(tmp_path, original, mode)
    state.editor.cursor = state.editor.at(len(prefix))
    handlers = reader_handlers(state)
    assert handlers.selected(True) == expected
    state.editor.selection = (state.editor.at(len(prefix)), state.editor.GetLastPosition())
    assert handlers.selected() == expected
    state.editor.selection = (0, 0)
    assert handlers.selected() == state.editor.GetValue()
    assert state.document.source_text == original


@pytest.mark.parametrize('mode', ['utf16-crlf', 'utf16-lf', 'codepoint-lf'])
def test_real_recovery_stores_character_offset_and_restores_without_a_voice(tmp_path, mode):
    original = '😀 First.\r\nSecond 😀line.'
    state = state_for(tmp_path, original, mode)
    offset = len('😀 First.\nSecond 😀')
    state.editor.cursor = state.editor.at(offset)
    handlers = reader_handlers(state)
    assert handlers.save(force=True)
    raw = state.store.path.read_bytes()
    record = json.loads(raw)
    assert record['schema'] == 'carr.reader-session.v1' and record['cursor'] == offset
    recovered = ReaderDraftStore(tmp_path).load()
    restarted = state_for(tmp_path, recovered.document.text, mode, document=recovered.document, recovered=recovered)
    actual = reader_handlers(restarted)
    actual.loaded((SimpleNamespace(voices=lambda: (), notices=()), None))
    assert restarted.editor.cursor == restarted.editor.at(offset)
    assert actual.selected(True) == 'line.'
    assert restarted.document.source_text == original
    assert state.store.path.read_bytes() == raw, 'Loading/restoring never rewrites the saved record.'


@pytest.mark.parametrize('original', ['A😀B\r\nC😀D', 'A😀B\rC😀D', 'A😀B\nC😀D'])
def test_existing_v1_cursor_and_retained_line_endings_remain_readable(tmp_path, original):
    document = ReadingDocument(original)
    offset = original.index('D')
    store = ReaderDraftStore(tmp_path)
    store.save(ReaderResume(document, cursor=offset))
    before = store.path.read_bytes()
    recovered = store.load()
    state = state_for(tmp_path, recovered.document.text, document=recovered.document, recovered=recovered)
    reader_handlers(state).loaded((SimpleNamespace(voices=lambda: (), notices=()), None))
    assert state.editor.GetRange(state.editor.cursor, state.editor.GetLastPosition()) == 'D'
    assert recovered.document.text == recovered.document.source_text == original
    assert store.path.read_bytes() == before


def test_native_cursor_roundtrip_preserves_authenticated_source_anchor(tmp_path):
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.source import read_source_range
    app = LibraryApp(tmp_path / 'library')
    workspace = app.create_workspace('Cursor source')
    original = '😀 First.\r\nSecond 😀line.'
    imported = app.add_text(workspace, 'Exact source', original)
    source = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 1)
    document = source.reading_document()
    state = state_for(app.root, document.text, document=document)
    offset = state.editor.value.index('line.')
    state.editor.cursor = state.editor.at(offset)
    assert reader_handlers(state).save(force=True)
    recovered = ReaderDraftStore(app.root).load()
    assert recovered.document.source_anchor == source.reading_document().source_anchor
    assert recovered.document.source_anchor['quotation_authority'] is False
    assert recovered.document.source_text == source.text
    assert app.assets.resolve(imported['asset']).read_bytes() == original.encode('utf-8')
    restored = state_for(app.root, recovered.document.text, document=recovered.document, recovered=recovered)
    handlers = reader_handlers(restored)
    handlers.loaded((SimpleNamespace(voices=lambda: (), notices=()), source))
    assert restored.guards == [True] and handlers.selected(True) == 'line.'


def test_changed_display_or_invalid_native_boundary_never_publishes_recovery(tmp_path):
    state = state_for(tmp_path, '😀A')
    state.store.save(ReaderResume(state.document))
    before = state.store.path.read_bytes()
    state.editor.cursor = 1  # Inside the UTF-16 surrogate pair, not a valid caret.
    with pytest.raises(UnicodeError):
        reader_handlers(state).save(force=True)
    assert state.store.path.read_bytes() == before
    state.editor.ChangeValue('Different')
    with pytest.raises(ValueError, match='differs'):
        ui._restore_character_cursor(state.editor, '😀A', 1)
    assert state.editor.cursor == 0 and state.store.path.read_bytes() == before
