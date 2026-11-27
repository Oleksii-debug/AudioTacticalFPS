"""Actual modal callbacks and stores, with explicit native controls and voice doubles."""
from itertools import count
import json
from queue import Empty, Queue
import sys
from threading import get_ident
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.application import LibraryApp
from carr_library.speech import Voice, VoiceRegistry
from carr_library.speech.session import ReaderDraftStore
from carr_library.studio.render import ProductionService
from carr_library.ui import speech_dialog, studio_dialog
from carr_library.ui.save_all import SaveCoordinator, SaveResult


@pytest.fixture
def native_view(tmp_path, monkeypatch):
    owner = get_ident()
    callbacks, identifiers = Queue(), count(10000)
    state = SimpleNamespace(controls=[], drive=lambda dialog: None)
    class Control:
        def __init__(self, parent=None, *args, **kwargs):
            self.parent = parent
            self.label = kwargs.get('label', next((a for a in args if isinstance(a, str)), ''))
            self.value = kwargs.get('value', kwargs.get('initial', ''))
            self.items = list(kwargs.get('choices', ()))
            self.name, self.selection, self.cursor = '', -1, 0
            self.enabled, self.destroyed, self.running = True, False, False
            self.handlers = {}
            state.controls.append(self)
        def native(self):
            assert get_ident() == owner, 'A control was used from a worker.'
            current = self
            while current is not None:
                assert not getattr(current, 'destroyed', False), 'A late save touched a destroyed view.'
                current = getattr(current, 'parent', None)
        def SetName(self, value): self.native(); self.name = value
        def SetValue(self, value): self.native(); self.value = value
        def ChangeValue(self, value): self.SetValue(value)
        def GetValue(self): self.native(); return self.value
        def SetLabel(self, value): self.native(); self.label = value
        def Set(self, values): self.native(); self.items = list(values)
        def SetItems(self, values): self.Set(values)
        def SetSelection(self, value): self.native(); self.selection = value
        def GetSelection(self): self.native(); return self.selection
        def GetStringSelection(self): self.native(); return ''
        def SetInsertionPoint(self, value): self.native(); self.cursor = value
        def GetInsertionPoint(self): self.native(); return self.cursor
        def GetParent(self): self.native(); return self.parent
        def Enable(self, value=True): self.native(); self.enabled = value
        def Disable(self): self.Enable(False)
        def SetFocus(self): self.native(); assert self.enabled
        def Add(self, *args): self.native()
        def SetSizer(self, *args): self.native()
        def SetEscapeId(self, *args): self.native()
        def SetAcceleratorTable(self, *args): self.native()
        def SetRange(self, value): self.native(); self.range = value
        def Bind(self, event, callback, *args, **kwargs): self.native(); self.handlers[event] = callback
        def Pulse(self): self.native()
        def Start(self, *args): self.native(); self.running = True
        def Stop(self): self.native(); self.running = False
        def IsRunning(self): self.native(); return self.running
        def IsActive(self): self.native(); return True
        def EndModal(self, result): self.native()
        def Destroy(self): self.native(); self.destroyed = True
    class Dialog(Control):
        def ShowModal(self):
            self.native()
            state.dialog = self
            state.drive(self)
            return 5101
    wx = SimpleNamespace(Dialog=Dialog, TextCtrl=Control, StaticText=Control, Choice=Control,
        SpinCtrl=Control, Button=Control, BoxSizer=Control, WrapSizer=Control, Timer=Control, Gauge=Control, CheckBox=Control,
        CallAfter=lambda callback, *args: callbacks.put((callback, args)),
        NewIdRef=lambda: next(identifiers), AcceleratorTable=lambda entries: entries,
        MessageBox=lambda *args, **kwargs: 4, ID_OK=5100, ID_CANCEL=5101, ID_CLOSE=5102,
        NOT_FOUND=-1, YES=2, NO=4, CANCEL=8, EVT_BUTTON='button', EVT_MENU='menu',
        EVT_CHOICE='choice', EVT_TEXT='text', EVT_CLOSE='close', EVT_TIMER='timer')
    for name in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL', 'LEFT', 'TOP',
            'RIGHT', 'EXPAND', 'ALL', 'ALIGN_CENTER_VERTICAL', 'ALIGN_RIGHT', 'TE_READONLY',
            'TE_MULTILINE', 'TE_DONTWRAP', 'YES_NO', 'ICON_QUESTION', 'ACCEL_CTRL', 'ACCEL_SHIFT',
            'WXK_F6', 'WXK_F7', 'WXK_F8', 'GA_HORIZONTAL'):
        setattr(wx, name, 0)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    class Provider:
        name = 'Synthetic enumeration; no audio is rendered'
        def voices(self): return (Voice('fixture:local', 'Fixture voice', 'fixture', 'en'),)
    monkeypatch.setattr('carr_library.runtime_settings.voice_registry',
                        lambda *args, **kwargs: VoiceRegistry((Provider(),)))
    state.app = LibraryApp(tmp_path / 'library')
    state.coordinator = SaveCoordinator()
    state.parent = SimpleNamespace(library=state.app, _save_coordinator=state.coordinator, report=Mock())
    registry = VoiceRegistry((Provider(),))
    state.service = ProductionService(state.app.root / 'state/studio', voices=registry)
    state.controller = studio_dialog.StudioController(state.app, service=state.service)
    monkeypatch.setattr(studio_dialog, 'StudioController', lambda library: state.controller)
    def named(prefix):
        return next(control for control in state.controls if control.name.startswith(prefix))
    def button(label):
        target = next(control for control in state.controls if control.label == label)
        assert target.enabled
        return target.handlers['button'](None)
    def pump(predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline, 'The native callback queue did not finish.'
            try:
                callback, args = callbacks.get(timeout=.02)
            except Empty:
                continue
            callback(*args)
    def ready(kind):
        choice = named('Voice (installed') if kind == 'reader' else named('Default voice')
        pump(lambda: bool(choice.items) and choice.enabled)
    def open_view(kind):
        if kind == 'reader':
            speech_dialog.show_speech_reader(state.parent, text='Immutable original reading text.')
        else:
            studio_dialog.show_studio(state.parent)
    state.named, state.button, state.pump = named, button, pump
    state.ready, state.open = ready, open_view
    yield state
    registry.close()


@pytest.mark.parametrize('clear_text', [False, True])
def test_reader_save_all_persists_actual_mapped_draft_including_deleted_text(native_view, clear_text):
    state = native_view
    reading_text = '' if clear_text else 'An explicitly edited narration copy.'
    def drive(dialog):
        state.ready('reader')
        state.named('Reading copy (editable').SetValue(reading_text)
        assert state.button('Save all open drafts')
        restored = ReaderDraftStore(state.app.root).load()
        assert restored.document.text == reading_text
        assert restored.document.source_text == 'Immutable original reading text.'
        assert state.named('Speech and save status').GetValue() == 'All 1 open drafts saved.'
    state.drive = drive
    state.open('reader')
    assert not state.coordinator.participants and not state.coordinator.busy


@pytest.mark.parametrize('clear_after_save', [False, True])
def test_studio_save_all_commits_recovered_draft_and_retains_invalid_newer_edit(native_view, monkeypatch, clear_after_save):
    state = native_view
    stored = Mock(wraps=state.service.projects.save)
    monkeypatch.setattr(state.service.projects, 'save', stored)
    script = 'A real stored project produced from this synthetic script.'
    def drive(dialog):
        state.ready('studio')
        editor = state.named('Script —')
        editor.SetValue(script)
        editor.handlers['text'](SimpleNamespace(Skip=lambda: None))
        dialog.handlers['timer'](None)
        assert json.loads(state.controller.draft.read_bytes())['text'] == script
        assert not state.service.projects.list()
        assert state.button('Save all open drafts')
        saved = state.service.projects.load(state.controller.project.id)
        assert saved.original_script == script and stored.call_count == 1
        assert state.named('Production and save status').GetValue() == 'All 1 open drafts saved.'
        if clear_after_save:
            editor.SetValue('')
            editor.handlers['text'](SimpleNamespace(Skip=lambda: None))
        assert state.button('Save all open drafts')
        status = state.named('Production and save status').GetValue()
        assert ('Unsaved changes remain' in status) if clear_after_save else status.startswith('No unsaved drafts.')
        assert stored.call_count == 1
        assert state.service.projects.load(saved.id).to_dict() == saved.to_dict()
        if clear_after_save:
            assert json.loads(state.controller.draft.read_bytes())['text'] == ''
    state.drive = drive
    state.open('studio')
    assert not state.coordinator.participants and not state.coordinator.busy


@pytest.mark.parametrize('kind', ['reader', 'studio'])
def test_pending_save_all_completion_never_touches_closed_modal(native_view, kind):
    state = native_view
    replies, dirty = [], [True]
    state.coordinator.register('pending-note', 'Research note', dirty=lambda: dirty[0], save=replies.append)
    def drive(dialog):
        state.ready(kind)
        assert state.button('Save all open drafts')
        assert len(replies) == 1 and state.coordinator.busy
        state.button('C&lose' if kind == 'reader' else '&Close')
    state.drive = drive
    state.open(kind)
    assert tuple(state.coordinator.participants) == ('pending-note',)
    dirty[0] = False
    replies[0](SaveResult(True))
    assert not state.coordinator.busy
