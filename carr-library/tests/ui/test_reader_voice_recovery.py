"""Execute reader recovery callbacks against real drafts without a voice engine."""
import ast
from importlib.util import find_spec
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.speech.navigation import native_cursor_position, native_insertion_offset
from carr_library.speech.reading import ReadingDocument
from carr_library.speech.runtime import Voice, VoiceRegistry
from carr_library.speech.session import ReaderDraftStore, ReaderResume
from carr_library.speech.tabs import ReadingTab, ReadingTabsStore
from carr_library.ui.reader_tabs import NativeReaderTabs, TabState


class Editor:
    def __init__(self, text):
        self.text, self.position = text, 0
        self.focused = False

    def GetValue(self): return self.text
    def GetRange(self, first, last): return self.text[first:last]
    def GetInsertionPoint(self): return self.position
    def GetLastPosition(self): return len(self.text)
    def SetInsertionPoint(self, value):
        assert 0 <= value <= len(self.text)
        self.position = value
    def ChangeValue(self, value): self.text = value
    def SetFocus(self): self.focused = True


class Choice:
    def __init__(self): self.items, self.selection = [], -1
    def GetSelection(self): return self.selection
    def SetSelection(self, value):
        assert value == -1 or 0 <= value < len(self.items)
        self.selection = value
    def Set(self, items):
        self.items, self.selection = list(items), -1


class Number:
    def __init__(self, value): self.value = value
    def GetValue(self): return self.value
    def SetValue(self, value): self.value = value


class Inventory:
    """Discovery supplies metadata only; synthesis must never be requested."""
    notices = ()

    def __init__(self, voices=()):
        self.listed = tuple(voices)
        self.close = Mock()
        self.synthesize = Mock(side_effect=AssertionError('Recovery must not synthesize.'))

    def voices(self): return self.listed


class WorkQueue:
    def __init__(self): self.pending = []
    def __call__(self, work, done, **options): self.pending.append((work, done))
    def finish(self):
        work, done = self.pending.pop(0)
        done(work())


def _callbacks(values):
    # This follows the imported package, including the sealed installed origin.
    path = Path(find_spec('carr_library.ui.speech_dialog').origin)
    module = ast.parse(path.read_text(encoding='utf-8'))
    reader = next(node for node in module.body
                  if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    names = {'reload', 'save_recovery', 'capture_reading_tab',
             'activate_reading_tab', 'sync_document'}
    callbacks = [node for node in reader.body
                 if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in callbacks} == names
    result = ', '.join(repr(name) + ': ' + name for name in sorted(names))
    factory = ast.parse('def build(' + ', '.join(values) + '):\n    return {' + result + '}\n')
    factory.body[0].body[:0] = callbacks
    namespace = dict(Path=Path, VoiceRegistry=VoiceRegistry, ReaderResume=ReaderResume,
        native_cursor_position=native_cursor_position, native_insertion_offset=native_insertion_offset,
        __package__='carr_library.ui')
    exec(compile(ast.fix_missing_locations(factory), str(path), 'exec'), namespace)
    return SimpleNamespace(**namespace['build'](**values))


@pytest.fixture
def reader(tmp_path, monkeypatch):
    from carr_library import runtime_settings

    created = []
    def create(*, tabs=False, recovered=True):
        root = tmp_path / str(len(created))
        root.mkdir()
        document = ReadingDocument('Original exact text.\nДругий рядок.', title='Independent copy')
        initial = ReaderResume(document, 'fixture:temporarily-unavailable', 2, 73, 7, 2.25)
        store = ReaderDraftStore(root)
        if recovered:
            store.save(initial)
        editor, choice, queue = Editor(document.text), Choice(), WorkQueue()
        context = SimpleNamespace(next_inventory=Inventory(), inventories=[], discoveries=[])
        current_tabs = None
        if tabs:
            current_tabs = NativeReaderTabs.__new__(NativeReaderTabs)
            current_tabs.store = ReadingTabsStore(root)
            entry = ReadingTab('primary', 'First copy')
            saved, token = store.load_with_token()
            state = TabState(entry, saved or initial, store, token, editor=editor)
            current_tabs.states, current_tabs.active, current_tabs.token = [state], 0, None
            current_tabs.alive = current_tabs.bound = True
            current_tabs.guard = Mock()
            current_tabs.busy = lambda: False
            initial = state.resume

        def discover(state_root, *, cancelled=lambda: False):
            assert Path(state_root) == root / 'state' and not cancelled()
            context.discoveries.append(Path(state_root))
            context.inventories.append(context.next_inventory)
            return context.next_inventory

        monkeypatch.setattr(runtime_settings, 'voice_registry', discover)
        saved_resume = initial if recovered or tabs else None
        player, report = Mock(), Mock()
        values = dict(parent=SimpleNamespace(library=SimpleNamespace(root=root)),
            alive=True, source_loaded=True, source_loader=None, source_guard=Mock(),
            document=document, editor=editor, source_status=Mock(),
            draft_store=store, recovered=saved_resume, recovery_notice='',
            remembered_voice_id=saved_resume.voice_id if saved_resume else '',
            last_recovery_signature=None, reader_tabs=current_tabs, save_path=None,
            last_audio=None, player=player, rate=Number(2), volume=Number(73),
            voices=(), voice_choice=choice, registry=None, owned_registries=set(),
            cancel=Event(), run=queue, report=report, capabilities=Mock(),
            presentation=SimpleNamespace(control=editor, current=None, apply=Mock()))
        handlers = _callbacks(values)
        result = SimpleNamespace(handlers=handlers, values=values, root=root, store=store,
            document=document, initial=initial, editor=editor, choice=choice, queue=queue,
            context=context, tabs=current_tabs, player=player, report=report)
        created.append(result)
        return result

    yield create
    for item in created:
        item.player.assert_not_called()
        item.player.play.assert_not_called()
        for inventory in item.context.inventories:
            inventory.synthesize.assert_not_called()


def _reload(reader, voices=()):
    reader.context.next_inventory = Inventory(voices)
    reader.handlers.reload()
    reader.queue.finish()


@pytest.mark.parametrize('tabs', [False, True])
def test_empty_inventory_restores_saved_cursor_without_writing_or_substituting(reader, tabs):
    view = reader(tabs=tabs)
    before = view.store.path.read_bytes()
    _reload(view)
    assert view.editor.position == view.initial.cursor
    assert view.choice.selection == -1 and not view.choice.items
    assert view.store.path.read_bytes() == before
    assert view.document.source_text == view.initial.document.source_text
    assert any('No local voice' in str(call) for call in view.report.call_args_list)


@pytest.mark.parametrize('tabs', [False, True])
def test_missing_voice_survives_autosave_capture_and_fresh_reopen(reader, tabs):
    view = reader(tabs=tabs)
    _reload(view)
    view.editor.SetInsertionPoint(13)
    assert view.handlers.save_recovery(force=True)
    captured, output = view.handlers.capture_reading_tab()
    reopened = ReaderDraftStore(view.root).load()
    assert output is None
    assert captured.voice_id == reopened.voice_id == view.initial.voice_id
    assert (reopened.rate, reopened.volume, reopened.cursor) == (2, 73, 13)
    assert reopened.document.source_text == view.document.source_text
    view.editor.SetInsertionPoint(0)
    _reload(view)
    assert view.editor.position == 13  # The latest successful save, not the startup cursor.


@pytest.mark.parametrize('tabs,recovered', [(False, False), (False, True), (True, True)])
def test_choice_removed_before_autosave_keeps_exact_new_requested_identity(reader, tabs, recovered):
    view = reader(tabs=tabs, recovered=recovered)
    chosen = Voice('fixture:new-choice', 'Explicit new choice', 'fixture', 'uk')
    _reload(view, (chosen,))
    view.choice.SetSelection(0)
    # No recovery write occurs between choosing the voice and removing its engine.
    _reload(view)
    assert view.choice.selection == -1
    captured, _ = view.handlers.capture_reading_tab()
    assert captured.voice_id == chosen.id
    assert view.handlers.save_recovery(force=True)
    assert ReaderDraftStore(view.root).load().voice_id == chosen.id
    replacement = Voice('fixture:other', 'Unrelated available voice', 'fixture', 'en')
    _reload(view, (replacement,))
    assert view.choice.selection == -1  # Never silently select the remaining engine.
    assert view.handlers.save_recovery(force=True)
    assert ReaderDraftStore(view.root).load().voice_id == chosen.id
    _reload(view, (replacement, chosen))
    assert view.choice.selection == 1


def test_tab_activation_restores_its_own_passive_voice_instead_of_previous_tab(reader):
    view = reader(tabs=True)
    _reload(view)
    first = view.tabs.current
    first_before = first.store.path.read_bytes()
    entry = ReadingTab.new('Second copy')
    store = view.tabs.store.draft(entry)
    second_document = ReadingDocument('Second independent original.')
    resume = ReaderResume(second_document, 'fixture:second-missing', -2, 61, 9, 1.5)
    token = store.save_with_token(resume, expected_token=None)
    second = TabState(entry, resume, store, token, editor=Editor(second_document.text))
    view.tabs.states.append(second)
    view.tabs.active = 1
    view.handlers.activate_reading_tab(second)
    captured, _ = view.handlers.capture_reading_tab()
    assert (captured.voice_id, captured.cursor) == ('fixture:second-missing', 9)
    assert view.handlers.save_recovery(force=True)
    assert store.load().voice_id == resume.voice_id
    assert first.store.path.read_bytes() == first_before
    view.tabs.active = 0
    view.handlers.activate_reading_tab(first)
    captured, _ = view.handlers.capture_reading_tab()
    assert captured.voice_id == view.initial.voice_id
    assert captured.document.source_text == view.initial.document.source_text


def test_failed_tab_cas_does_not_claim_attempted_cursor_as_saved(reader):
    view = reader(tabs=True)
    _reload(view)
    view.editor.SetInsertionPoint(15)
    other = ReaderDraftStore(view.root)
    other.save(ReaderResume(ReadingDocument('A separately saved reading copy.'), 'fixture:other'))
    retained = other.path.read_bytes()
    with pytest.raises(ValueError, match='Another window changed'):
        view.handlers.save_recovery(force=True)
    assert other.path.read_bytes() == retained
    view.editor.SetInsertionPoint(0)
    _reload(view)
    assert view.editor.position == view.initial.cursor
    assert other.path.read_bytes() == retained
