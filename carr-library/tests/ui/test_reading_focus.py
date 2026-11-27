"""Actual native focus handlers with deterministic time and no automatic audio."""
from hashlib import sha256
from pathlib import Path
from threading import get_ident
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from carr_library.speech.focus import FocusPlan, ReadingAudio
from carr_library.speech.reading import ReadingDocument
from carr_library.ui.reading_focus import create_reading_focus


class Widget:
    def __init__(self, parent=None, *args, **kwargs):
        self.owner, self.dead, self.handlers = get_ident(), False, {}
        self.value, self.selection = kwargs.get('value', kwargs.get('initial', '')), -1
        self.name, self.focused, self.running = '', 0, False
    def _widget_check(self): assert get_ident() == self.owner and not self.dead
    def GetId(self): self._widget_check(); return id(self)
    def SetName(self, value): self._widget_check(); self.name = value
    def Wrap(self, value): self._widget_check()
    def Add(self, *args, **kwargs): self._widget_check()
    def SetSizer(self, value): self._widget_check()
    def SetFocus(self): self._widget_check(); self.focused += 1
    def Bind(self, event, handler, *args, **kwargs): self._widget_check(); self.handlers[event] = handler
    def GetValue(self): self._widget_check(); return self.value
    def ChangeValue(self, value): self._widget_check(); self.value = value
    def GetFont(self): self._widget_check(); return SimpleNamespace(SetPointSize=Mock())
    def SetFont(self, value): self._widget_check()
    def GetSelection(self): self._widget_check(); return self.selection
    def SetSelection(self, *value): self._widget_check(); self.selection = value[0] if len(value) == 1 else value
    def GetLastPosition(self): self._widget_check(); return len(self.value)
    def XYToPosition(self, column, line):
        self._widget_check()
        lines = self.value.splitlines(keepends=True)
        return sum(len(part) for part in lines[:line]) + column
    def GetRange(self, first, last): self._widget_check(); return self.value[first:last]
    def ShowPosition(self, value): self._widget_check()
    def SetAcceleratorTable(self, value): self._widget_check()
    def EndModal(self, result): self._widget_check()
    def Start(self, interval): self._widget_check(); self.running = True
    def Stop(self): self._widget_check(); self.running = False
    def Destroy(self): self._widget_check(); self.dead = True


@pytest.fixture
def view():
    wx = SimpleNamespace(Dialog=Widget, TextCtrl=Widget, StaticText=Widget, Choice=Widget,
        SpinCtrl=Widget, BoxSizer=Widget, Button=Widget, Timer=Widget, ID_CANCEL=2,
        EVT_BUTTON='press', EVT_CLOSE='close', EVT_CHOICE='choice', EVT_TIMER='tick',
        EVT_MENU='menu', EVT_WINDOW_DESTROY='destroy', WXK_F7=7, WXK_F8=8, NewIdRef=lambda: 12, AcceleratorTable=lambda rows: rows)
    for key in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
                'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE', 'TE_RICH2', 'TE_NOHIDESEL'):
        setattr(wx, key, 0)
    doc = ReadingDocument('One two three four five.')
    plan = FocusPlan(doc)
    player = SimpleNamespace(path=Path('recording.wav'), position_seconds=0.0,
        state='stopped', play=Mock(), resume=Mock())
    result = SimpleNamespace(path=player.path, frames=1000, sample_rate=100,
        text_sha256=sha256(doc.text.encode('utf-8')).hexdigest())
    binding = ReadingAudio.from_render(doc, doc._signature(), 0, len(doc.text), doc.text, result)
    state = SimpleNamespace(doc=doc, plan=plan, now=10.0, player=player, guard=Mock(),
        authorize=Mock(), pause_audio=Mock(), stop_audio=Mock(), goto=Mock(), wx=wx)
    state.dialog = create_reading_focus(object(), plan=plan, cursor=0, player=player, audio=binding,
        guard=state.guard, authorize=state.authorize, pause_audio=state.pause_audio,
        stop_audio=state.stop_audio, goto=state.goto, wx_module=wx, clock=lambda: state.now)
    state.press = lambda name: state.dialog.buttons[name].handlers['press'](None)
    yield state
    state.dialog.close_resources()
    if not state.dialog.dead:
        state.dialog.Destroy()
    player.play.assert_not_called()
    player.resume.assert_not_called()


def test_open_is_paused_and_timer_does_not_steal_focus_or_change_text(view):
    before = view.doc.to_dict()
    assert not view.dialog.pacer.running and not view.dialog.following
    assert view.dialog.word.GetValue() == 'One'
    view.now = 1000
    view.dialog.on_tick()
    assert view.dialog.word.GetValue() == 'One' and view.doc.to_dict() == before
    assert not view.dialog.preview.focused and not view.dialog.word.focused
    view.goto.assert_not_called()
    view.pause_audio.assert_not_called()


def test_explicit_visual_start_pause_and_next_share_the_reading_snapshot(view):
    view.press('start')
    view.now += .25
    view.dialog.on_tick()
    assert view.dialog.word.GetValue() == 'two' and view.dialog.pacer.running
    view.press('pause')
    view.now += 20
    view.dialog.on_tick()
    assert view.dialog.word.GetValue() == 'two' and not view.dialog.pacer.running
    view.press('next')
    assert view.dialog.word.GetValue() == 'three'
    view.authorize.assert_called()


def test_audio_follow_is_explicit_approximate_and_does_not_start_transport(view):
    view.dialog.mode.SetSelection(1)
    view.dialog.on_mode()
    assert not view.dialog.following
    view.press('start')
    assert view.dialog.following
    view.player.position_seconds, view.player.state = 6, 'playing'
    view.dialog.on_tick()
    assert 'Approximate audio follow' in view.dialog.status.GetValue()
    assert 'no engine word timings' in view.dialog.status.GetValue()
    assert view.dialog.word.GetValue() == 'four'
    assert not view.dialog.preview.focused
    view.press('audio_pause')
    view.pause_audio.assert_called_once()
    view.press('audio_stop')
    view.stop_audio.assert_called_once()


@pytest.mark.parametrize('change', ['text', 'source', 'audio'])
def test_changed_scope_halts_visual_work_and_retains_text(view, change):
    if change == 'audio':
        view.dialog.mode.SetSelection(1)
    view.press('start')
    if change == 'text': view.doc.set_text('A changed reading copy.')
    elif change == 'source': view.guard.side_effect = ValueError('The reader scope changed.')
    else: view.player.path = Path('other.wav')
    view.dialog.on_tick()
    assert not view.dialog.pacer.running and not view.dialog.following
    assert 'changed' in view.dialog.status.GetValue()
    view.goto.assert_not_called()


def test_goto_is_the_only_action_that_requests_main_editor_cursor_change(view):
    view.press('next')
    view.goto.assert_not_called()
    view.press('goto')
    view.goto.assert_called_once_with(view.plan.spans[1][0])
    assert not view.dialog.alive and not view.dialog.timer.running


def test_closed_focus_rejects_late_timer_and_button_callbacks(view):
    view.press('close')
    before = view.doc.to_dict()
    calls = view.authorize.call_count
    for identity in view.dialog.buttons:
        view.press(identity)
    view.dialog.on_tick()
    assert view.doc.to_dict() == before and view.authorize.call_count == calls
    assert not view.dialog.timer.running


def test_explicit_audio_stop_remains_reachable_after_source_revocation(view):
    view.press('start')
    view.guard.side_effect = ValueError('The source was revoked.')
    view.press('audio_stop')
    view.stop_audio.assert_called_once()
    assert not view.dialog.pacer.running and not view.dialog.following
    view.pause_audio.assert_not_called()
    view.goto.assert_not_called()


def test_destroyed_focus_owner_invalidates_late_callbacks_without_dying_wrapper_calls(view):
    dialog = view.dialog
    handler = dialog.handlers['destroy']
    child_id = dialog.preview.GetId()
    handler(SimpleNamespace(GetId=lambda: child_id, Skip=lambda: None))
    assert dialog.alive
    owner_id = dialog.GetId()
    dialog.dead = True  # Model SIP invalidation before EVT_WINDOW_DESTROY.
    handler(SimpleNamespace(GetId=lambda: owner_id, Skip=lambda: None))
    assert dialog.destroyed and not dialog.alive and not dialog.timer.running
    for identity in dialog.buttons:
        view.press(identity)
    dialog.on_tick()
    dialog.close_resources()
    view.goto.assert_not_called()
    view.pause_audio.assert_not_called()


def test_focus_reuses_existing_reader_keyboard_adapter_without_private_binding_store(view, monkeypatch):
    from carr_library.ui import reading_focus
    records = []
    class Borrowed:
        def __init__(self, dialog, parent, controls, **options):
            records.append((dialog, parent, controls, options))
            self.closed, self.manager = False, None
        def close(self):
            self.closed = True
    monkeypatch.setattr(reading_focus, 'VoiceReaderKeyboardAdapter', Borrowed)
    shared_host = object()
    dialog = reading_focus.create_reading_focus(object(), plan=view.plan, cursor=0,
        guard=view.guard, authorize=view.authorize, player=view.player,
        pause_audio=view.pause_audio, stop_audio=view.stop_audio,
        keyboard_parent=shared_host, wx_module=view.wx, clock=lambda: view.now)
    try:
        assert len(records) == 1 and records[0][:2] == (dialog, shared_host)
        assert set(records[0][2]) == {'voice_reader.pause', 'voice_reader.stop'}
        assert records[0][2]['voice_reader.pause'][0] is dialog.buttons['audio_pause']
        view.guard.side_effect = ValueError('The source was revoked.')
        records[0][2]['voice_reader.stop'][1]()
        view.stop_audio.assert_called_once()
        assert not dialog.pacer.running and not dialog.following
    finally:
        dialog.close_resources()
        assert dialog.keyboard.closed
        dialog.Destroy()
