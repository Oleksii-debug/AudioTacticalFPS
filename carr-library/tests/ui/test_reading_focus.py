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
    def check(self): assert get_ident() == self.owner and not self.dead
    def SetName(self, value): self.check(); self.name = value
    def Wrap(self, value): self.check()
    def Add(self, *args, **kwargs): self.check()
    def SetSizer(self, value): self.check()
    def SetFocus(self): self.check(); self.focused += 1
    def Bind(self, event, handler, *args, **kwargs): self.check(); self.handlers[event] = handler
    def GetValue(self): self.check(); return self.value
    def ChangeValue(self, value): self.check(); self.value = value
    def GetFont(self): self.check(); return SimpleNamespace(SetPointSize=Mock())
    def SetFont(self, value): self.check()
    def GetSelection(self): self.check(); return self.selection
    def SetSelection(self, *value): self.check(); self.selection = value[0] if len(value) == 1 else value
    def GetLastPosition(self): self.check(); return len(self.value)
    def GetRange(self, first, last): self.check(); return self.value[first:last]
    def ShowPosition(self, value): self.check()
    def SetAcceleratorTable(self, value): self.check()
    def EndModal(self, result): self.check()
    def Start(self, interval): self.check(); self.running = True
    def Stop(self): self.check(); self.running = False
    def Destroy(self): self.check(); self.dead = True


@pytest.fixture
def view():
    wx = SimpleNamespace(Dialog=Widget, TextCtrl=Widget, StaticText=Widget, Choice=Widget,
        SpinCtrl=Widget, BoxSizer=Widget, Button=Widget, Timer=Widget, ID_CANCEL=2,
        EVT_BUTTON='press', EVT_CLOSE='close', EVT_CHOICE='choice', EVT_TIMER='tick',
        EVT_MENU='menu', WXK_F7=7, WXK_F8=8, NewIdRef=lambda: 12, AcceleratorTable=lambda rows: rows)
    for key in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'HORIZONTAL',
                'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE', 'TE_RICH2', 'TE_NOHIDESEL'):
        setattr(wx, key, 0)
    doc = ReadingDocument('One two three four five.')
    plan = FocusPlan(doc)
    player = SimpleNamespace(path=Path('recording.wav'), position_seconds=0.0,
        state='stopped', play=Mock(), resume=Mock())
    result = SimpleNamespace(path=player.path, frames=1000, sample_rate=100,
        text_sha256=sha256(doc.text.encode('utf-8')).hexdigest())
    binding = ReadingAudio.from_render(doc, doc._text_signature(), 0, len(doc.text), doc.text, result)
    state = SimpleNamespace(doc=doc, plan=plan, now=10.0, player=player, guard=Mock(),
        authorize=Mock(), pause_audio=Mock(), stop_audio=Mock(), goto=Mock())
    state.dialog = create_reading_focus(object(), plan=plan, cursor=0, player=player, audio=binding,
        guard=state.guard, authorize=state.authorize, pause_audio=state.pause_audio,
        stop_audio=state.stop_audio, goto=state.goto, wx_module=wx, clock=lambda: state.now)
    state.press = lambda name: state.dialog.buttons[name].handlers['press'](None)
    yield state
    state.dialog.alive = False
    state.dialog.timer.Stop()
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
