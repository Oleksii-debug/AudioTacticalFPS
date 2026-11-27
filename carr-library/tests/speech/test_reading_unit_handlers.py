"""Actual reader closures, exact native offsets and existing synthesis worker."""
import ast
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import shutil
import sys
from threading import Condition, Event, get_ident
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech import FliteProvider, NativeSapiProvider, VoiceRegistry
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.runtime import Voice
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.ui import speech_dialog as ui

_spec = importlib.util.spec_from_file_location('carr_unit_cursor_fixture',
    Path(__file__).with_name('test_reader_cursor.py'))
_fixture = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_fixture)


def actual_hooks(state):
    tree = ast.parse(Path(ui.__file__).read_text(encoding='utf-8'))
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    names = {'sync_document', 'save_recovery', 'selected_text', 'navigate_text_unit', 'speak', 'run'}
    hooks = [node for node in reader.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in hooks} == names
    template = ast.parse('''
def factory(state):
    parent, editor, document = state.parent, state.editor, state.document
    unit_kind, voice_choice, voices = state.kind, state.voice_choice, state.voices
    rate, volume, source_status = state.rate, state.volume, state.status
    registry, player, executor, wx = state.registry, state.player, state.executor, state.wx
    busy, alive, source_loaded = False, True, True
    recovery_notice, last_recovery_signature = '', None
    draft_store, recovered, last_audio = state.store, None, None
    reload_button, progress, navigation_host = state.button, None, None
    playback_generation, cancel = 0, state.cancel
    audio_files, dialog = SimpleNamespace(name=str(state.root)), object()
    def source_guard():
        state.native()
        if not state.allowed:
            raise ValueError('Reading source authority was revoked')
    def capabilities(): state.native()
    def report(message, **kwargs):
        state.native(); state.messages.append(str(message))
    def start_playback(action): action()
    def placeholder(): pass
    original_save = save_recovery
    def save_recovery(*args, **kwargs):
        result = original_save(*args, **kwargs)
        state.after_save()
        return result
    def close():
        nonlocal alive
        alive = False
        cancel.set()
    def replace_document(value):
        nonlocal document
        document = value
        editor.ChangeValue(value.text)
    return SimpleNamespace(navigate=navigate_text_unit, speak=speak, close=close,
        document=lambda: document, replace_document=replace_document, busy=lambda: busy)
''')
    body = template.body[0].body
    index = next(i for i, node in enumerate(body) if isinstance(node, ast.FunctionDef) and node.name == 'placeholder')
    body[index:index + 1] = hooks
    namespace = dict(ui.__dict__, SimpleNamespace=SimpleNamespace)
    exec(compile(ast.fix_missing_locations(template), ui.__file__, 'exec'), namespace)
    return namespace['factory'](state)


@pytest.fixture
def reader(tmp_path):
    owner, condition, pending, futures = get_ident(), Condition(), deque(), deque()
    root = tmp_path / 'library'; root.mkdir()
    text = '😀 First sentence.\nOnly this sentence.\nLast sentence.'
    state = SimpleNamespace(root=tmp_path, parent=SimpleNamespace(library=SimpleNamespace(root=root), _closing=False),
        document=ReadingDocument(text), messages=[], selected=[], source=text, allowed=True, calls=[],
        kind=SimpleNamespace(selection=1), voice_choice=SimpleNamespace(selection=0),
        rate=SimpleNamespace(value=0), volume=SimpleNamespace(value=100), cancel=Event(),
        store=ReaderDraftStore(root), after_save=lambda: None)
    state.native = lambda: (_ for _ in ()).throw(AssertionError('Native control read on worker')) if get_ident() != owner else None
    for field in (state.kind, state.voice_choice):
        field.GetSelection = lambda value=field: (state.native(), value.selection)[1]
    for field in (state.rate, state.volume):
        field.GetValue = lambda value=field: (state.native(), value.value)[1]
    class Editor(_fixture.NativeEditor):
        def GetValue(self): state.native(); return super().GetValue()
        def GetInsertionPoint(self): state.native(); return super().GetInsertionPoint()
        def GetLastPosition(self): state.native(); return super().GetLastPosition()
        def GetRange(self, *args): state.native(); return super().GetRange(*args)
        def GetSelection(self): state.native(); return self.selection
        def SetSelection(self, first, last):
            state.native(); self.selection = (first, last); self.cursor = last
            state.selected.append((first, last))
        def SetFocus(self): state.native()
        def ShowPosition(self, value): state.native()
    state.editor = Editor(text)
    offset = text.index('Only') + 2
    state.editor.cursor = state.editor.at(offset)
    state.editor.selection = (state.editor.cursor, state.editor.cursor)
    state.status = SimpleNamespace(ChangeValue=lambda value: state.native())
    state.button = SimpleNamespace(Enable=lambda: state.native(), Disable=lambda: state.native())
    state.voices = (Voice('fixture:first', 'First', 'Synthetic control fixture', 'en-US'),
                    Voice('fixture:second', 'Second', 'Synthetic control fixture', 'uk-UA'))
    def synthesize(content, identity, output, **kwargs):
        assert get_ident() != owner, 'Synthesis did not use the existing background worker'
        state.calls.append((content, identity))
        with wave.open(str(output), 'wb') as stream:
            stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
            stream.writeframes(b'\0\0' * 800)
        return SimpleNamespace(path=output, duration_seconds=.1, sample_rate=8000, channels=1)
    state.registry = SimpleNamespace(synthesize=synthesize)
    state.player = SimpleNamespace(stop=lambda: state.native(), play=lambda path: state.native(),
                                   position_seconds=0)
    pool = ThreadPoolExecutor(max_workers=1)
    def submit(work):
        future = pool.submit(work)
        futures.append(future)
        return future
    def dispatch(callback, *args):
        with condition:
            pending.append((callback, args)); condition.notify_all()
    state.executor = SimpleNamespace(submit=submit)
    state.wx = SimpleNamespace(CallAfter=dispatch)
    state.handlers = actual_hooks(state)
    def finish():
        future = futures.popleft()
        try: future.result(timeout=30)
        except Exception: pass  # the actual production finish closure reports it
        with condition:
            assert condition.wait_for(lambda: bool(pending), timeout=5), 'Reader completion was not dispatched'
            callback, args = pending.popleft()
        callback(*args)
    def flush():
        for _ in range(5):
            if not futures: return
            finish()
        raise AssertionError('A unit action submitted an unexpected extra worker')
    state.finish, state.flush, state.futures = finish, flush, futures
    yield state
    state.handlers.close()
    pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize(('mode', 'expected'), [(0, 'Only'), (1, 'Only this sentence.'), (2, 'Only this sentence.')])
def test_actual_navigation_changes_only_exact_selection_and_recovery_cursor(reader, mode, expected):
    reader.kind.selection = mode
    document = reader.document
    before = document.to_dict()
    reader.handlers.navigate()
    assert not reader.selected and not reader.calls
    reader.flush()
    assert reader.editor.GetStringSelection() == expected
    assert document.to_dict() == before and document.source_text == reader.source
    recovered = reader.store.load()
    assert recovered.document.to_dict() == before
    assert recovered.cursor == reader.source.index(expected) + len(expected)
    assert not reader.calls and not reader.handlers.busy()


def test_repeated_next_previous_reuses_exact_selected_unit_and_does_not_speak(reader):
    reader.handlers.navigate(); reader.flush()
    assert reader.editor.GetStringSelection() == 'Only this sentence.'
    reader.handlers.navigate(step=1); reader.flush()
    assert reader.editor.GetStringSelection() == 'Last sentence.'
    previous = reader.editor.GetSelection()
    reader.handlers.navigate(step=1); reader.flush()
    assert reader.editor.GetSelection() == previous and 'does not wrap' in reader.messages[-1]
    reader.handlers.navigate(step=-1); reader.flush()
    assert reader.editor.GetStringSelection() == 'Only this sentence.' and not reader.calls


@pytest.mark.parametrize('change', ['text', 'cursor', 'selection', 'kind', 'voice', 'settings',
                                   'document', 'library', 'directory', 'source', 'cancel', 'close'])
def test_async_plan_cannot_extend_authority_over_changed_reader_scope(reader, change):
    reader.handlers.navigate(read_unit=True)
    if change == 'text':
        reader.editor.ChangeValue('Changed reading copy')
    elif change == 'cursor':
        reader.editor.cursor = 0; reader.editor.selection = (0, 0)
    elif change == 'selection':
        reader.editor.selection = (0, reader.editor.at(5))
    elif change == 'kind':
        reader.kind.selection = 0
    elif change == 'voice':
        reader.voice_choice.selection = 1
    elif change == 'settings':
        reader.rate.value = 1
    elif change == 'document':
        reader.handlers.replace_document(ReadingDocument(reader.source))
    elif change == 'library':
        reader.parent.library = SimpleNamespace(root=reader.parent.library.root)
    elif change == 'directory':
        path = reader.parent.library.root
        path.rename(path.with_name('old-library')); path.mkdir()
    elif change == 'source':
        reader.allowed = False
    elif change == 'cancel':
        reader.cancel.set()
    else:
        reader.handlers.close()
    reader.flush()
    assert not reader.calls and not reader.selected
    if change != 'close':
        assert reader.messages


@pytest.mark.parametrize('change', ['selection', 'text', 'voice'])
def test_late_scope_loss_before_synthesis_never_falls_back_to_all_text(reader, change):
    def after_save():
        if change == 'selection':
            reader.editor.selection = (reader.editor.cursor, reader.editor.cursor)
        elif change == 'text':
            reader.editor.ChangeValue('Changed before speech')
        else:
            reader.voice_choice.selection = 1
    reader.after_save = after_save
    reader.handlers.navigate(read_unit=True); reader.flush()
    assert not reader.calls and 'no wider text was read' in reader.messages[-1]


def test_missing_voice_or_running_work_does_not_move_selection(reader):
    reader.voice_choice.selection = -1
    before = reader.editor.GetSelection()
    reader.handlers.navigate(read_unit=True)
    assert not reader.futures and reader.editor.GetSelection() == before
    assert 'installed local voice' in reader.messages[-1]
    reader.voice_choice.selection = 0
    reader.handlers.navigate()
    reader.handlers.navigate(step=1)
    reader.flush()
    assert reader.editor.GetStringSelection() == 'Only this sentence.'
    assert any('current reader operation' in message for message in reader.messages)


def test_utf16_surrogate_boundary_is_rejected_before_any_plan(reader):
    reader.editor.cursor = 1; reader.editor.selection = (1, 1)
    reader.handlers.navigate()
    assert not reader.futures and not reader.selected and reader.messages


def test_failed_native_selection_restores_the_prior_range_without_speech(reader, monkeypatch):
    original = reader.editor.SetSelection
    prior = reader.editor.GetSelection()
    def incorrect(first, last):
        original(first, last - 1 if last > first else last)
    monkeypatch.setattr(reader.editor, 'SetSelection', incorrect)
    reader.handlers.navigate(read_unit=True); reader.flush()
    assert reader.editor.GetSelection() == prior and not reader.calls
    assert 'could not be selected exactly' in reader.messages[-1]


def test_retained_source_lineage_survives_unit_navigation_and_recovery(reader):
    from carr_library.application import LibraryApp
    from carr_library.reader.reading_desk import ReadingDesk
    from carr_library.speech.source import read_source_range

    app = LibraryApp(reader.parent.library.root)
    workspace = app.create_workspace('Exact navigation source')
    imported = app.add_text(workspace, 'Retained source', reader.source)
    source = read_source_range(ReadingDesk(app, workspace, imported['edition']), 1, 1)
    document = source.reading_document()
    reader.handlers.replace_document(document)
    offset = reader.editor.GetValue().index('Only') + 1
    reader.editor.cursor = reader.editor.at(offset)
    reader.editor.selection = (reader.editor.cursor, reader.editor.cursor)
    before = document.to_dict()
    reader.handlers.navigate(); reader.flush()
    recovered = reader.store.load().document
    assert document.to_dict() == before and recovered.to_dict() == before
    assert recovered.source_anchor['quotation_authority'] is False
    assert app.assets.resolve(imported['asset']).read_bytes() == reader.source.encode('utf-8')


def test_navigation_preserves_existing_undo_history_and_retained_source(reader):
    reader.editor.ChangeValue(reader.source.replace('Only', 'Edited'))
    reader.document.set_text(reader.editor.GetValue())
    before = reader.document.to_dict()
    offset = reader.editor.GetValue().index('Edited') + 1
    reader.editor.cursor = reader.editor.at(offset)
    reader.editor.selection = (reader.editor.cursor, reader.editor.cursor)
    reader.handlers.navigate(); reader.flush()
    assert reader.editor.GetStringSelection() == 'Edited this sentence.'
    assert reader.document.to_dict() == before and reader.document.source_text == reader.source
    assert reader.document.undo() and reader.document.text == reader.source


@pytest.mark.parametrize(('mode', 'expected'), [(0, 'Only'), (1, 'Only this sentence.'), (2, 'Only this sentence.')])
def test_actual_existing_sapi_or_flite_worker_speaks_only_the_bound_unit(reader, mode, expected):
    providers = [NativeSapiProvider()] if sys.platform == 'win32' else [FliteProvider(shutil.which('ffmpeg'))]
    voices = VoiceRegistry(providers)
    try:
        available = voices.voices()
        assert available, 'Actual installed SAPI/Flite is required: ' + '; '.join(voices.notices)
        selected = next((voice for voice in available if voice.id == 'flite:awb'), available[0])
        reader.voices = (selected,)
        actual = voices.synthesize
        def synthesize(text, identity, output, **kwargs):
            reader.calls.append((text, identity))
            result = actual(text, identity, output, **kwargs)
            with wave.open(str(result.path), 'rb') as audio:
                assert audio.getnframes() > 0 and audio.getsampwidth() == 2
            return result
        reader.registry = SimpleNamespace(synthesize=synthesize)
        reader.kind.selection = mode
        reader.handlers = actual_hooks(reader)
        before = reader.document.to_dict()
        reader.handlers.navigate(read_unit=True)
        reader.flush()
        assert reader.calls == [(expected, selected.id)]
        assert reader.editor.GetStringSelection() == expected and reader.document.to_dict() == before
        assert any(message.startswith('Playing ') for message in reader.messages)
    finally:
        voices.close()


def test_every_new_unit_action_has_a_native_accessible_name():
    source = Path(ui.__file__).read_text(encoding='utf-8')
    for name in ('Text unit: word, sentence or paragraph', 'Select previous text unit',
                 'Select current text unit', 'Select next text unit', 'Read current text unit'):
        assert name in source

