"""Actual playback-dialog handlers with native-control lifetime assertions."""
from threading import get_ident
from types import SimpleNamespace
import sys

import pytest

from carr_library.ui.reader_playback import show_reader_playback


class Widget:
    def __init__(self, parent=None, *args, **kwargs):
        self.owner, self.parent = get_ident(), parent
        self.name = kwargs.get('label', next((value for value in args if isinstance(value, str)), ''))
        self.value = kwargs.get('value', '')
        self.choices, self.selection = list(kwargs.get('choices', ())), -1
        self.destroyed = False
        self.handlers = {}
        self.widgets.append(self)

    def native(self):
        assert get_ident() == self.owner
        assert not self.destroyed

    def SetName(self, value): self.native(); self.name = value
    def GetValue(self): self.native(); return self.value
    def ChangeValue(self, value): self.native(); self.value = value
    def SetValue(self, value): self.ChangeValue(value)
    def SetFocus(self): self.native()
    def Add(self, *args, **kwargs): self.native()
    def SetSizer(self, *args): self.native()
    def SetEscapeId(self, *args): self.native()
    def SetScrollRate(self, *args): self.native()
    def FitInside(self): self.native()
    def GetSelection(self): self.native(); return self.selection
    def SetSelection(self, value): self.native(); self.selection = value
    def Set(self, values): self.native(); self.choices = list(values); self.selection = -1
    def Bind(self, event, handler, *args): self.native(); self.handlers[event] = handler
    def EndModal(self, result): self.native()
    def Destroy(self): self.native(); self.destroyed = True


@pytest.fixture
def dialog(monkeypatch):
    widgets, deferred, subscriptions, actions = [], [], [], []
    Widget.widgets = widgets
    fixture = SimpleNamespace(driver=lambda _dialog: None)
    class Dialog(Widget):
        def ShowModal(self):
            self.native()
            fixture.driver(self)
            return 2
    class StaticText(Widget):
        pass
    wx = SimpleNamespace(Dialog=Dialog, StaticText=StaticText, TextCtrl=Widget,
        Choice=Widget, ScrolledWindow=Widget,
        Button=Widget, BoxSizer=Widget, GridSizer=Widget, ID_CANCEL=2,
        EVT_BUTTON='button')
    for key in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'EXPAND', 'ALL',
                'TE_READONLY', 'TE_MULTILINE', 'ALIGN_RIGHT', 'TAB_TRAVERSAL', 'VSCROLL', 'HORIZONTAL'):
        setattr(wx, key, 0)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    player = SimpleNamespace(ab_loop=None, duration_seconds=3, skip_intervals=(10, 30),
        sleep_remaining_seconds=None, state='stopped', position_seconds=0,
        position_is_approximate=False)
    def set_ab(a, b):
        if not 0 <= a < b <= 3:
            raise ValueError('Invalid A–B range')
        actions.append(('ab', a, b))
        player.ab_loop = a, b
    def set_skips(backward, forward):
        actions.append(('skips', backward, forward))
        player.skip_intervals = backward, forward
    def sleep(seconds=None):
        actions.append(('sleep', seconds))
        player.sleep_remaining_seconds = seconds
    player.set_ab_loop, player.set_skip_intervals, player.set_sleep_timer = set_ab, set_skips, sleep
    player.play_current = lambda: actions.append(('play',))
    player.skip_backward = lambda: actions.append(('backward',))
    player.skip_forward = lambda: actions.append(('forward',))
    player.clear_ab_loop = lambda: actions.append(('clear-ab',))
    fixture.player, fixture.actions, fixture.deferred = player, actions, deferred
    fixture.widgets, fixture.subscriptions = widgets, subscriptions
    def execute(action, *, audible=False):
        if audible:
            deferred.append(action)
        else:
            action()
    def show():
        return show_reader_playback(object(), player, execute=execute,
            stop=lambda: actions.append(('stop',)), pause=lambda: actions.append(('pause',)),
            subscribe=subscriptions.append)
    fixture.show = show
    def find(name):
        # A StaticText label and its explicitly named TextCtrl are distinct
        # native widgets. Change the editable control, never its caption.
        matches = [widget for widget in widgets
                   if not isinstance(widget, StaticText) and widget.name == name]
        assert len(matches) == 1, (name, matches)
        return matches[0]
    fixture.find = find
    fixture.press = lambda name: fixture.find(name).handlers['button'](None)
    return fixture


def test_open_and_close_only_subscribe_to_owned_status_without_starting_audio(dialog):
    dialog.show()
    assert dialog.actions == [] and dialog.deferred == []
    assert callable(dialog.subscriptions[0]) and dialog.subscriptions[-1] is None
    # A status callback retained by an old timer must not touch destroyed wx.
    for widget in dialog.widgets:
        widget.destroyed = True
    dialog.subscriptions[0]()


def test_deferred_ab_uses_exact_numeric_values_reviewed_before_queueing(dialog):
    def drive(_window):
        dialog.find('Repeat A in seconds').ChangeValue('.25')
        dialog.find('Repeat B in seconds').ChangeValue('1.25')
        dialog.press('Apply A–B repeat')
        assert dialog.actions == [] and len(dialog.deferred) == 1
        dialog.find('Repeat A in seconds').ChangeValue('2')
        dialog.find('Repeat B in seconds').ChangeValue('2.5')
        dialog.deferred.pop(0)()
        assert dialog.actions == [('ab', .25, 1.25)]
    dialog.driver = drive
    dialog.show()


@pytest.mark.parametrize('name', ['Play current recording', 'Apply A–B repeat', 'Skip forward'])
def test_closed_modal_cannot_run_its_previously_queued_audio_action(dialog, name):
    def drive(_window):
        dialog.press(name)
        assert len(dialog.deferred) == 1 and not dialog.actions
    dialog.driver = drive
    dialog.show()
    for widget in dialog.widgets:
        widget.destroyed = True
    dialog.deferred.pop(0)()
    assert dialog.actions == []


def test_setting_skips_and_sleep_is_explicit_and_does_not_play(dialog):
    def drive(_window):
        dialog.find('Backward skip in seconds, 0.1 to 600').ChangeValue('2.5')
        dialog.find('Forward skip in seconds, 0.1 to 600').ChangeValue('7.5')
        dialog.press('Apply skip intervals')
        dialog.find('Sleep timer in minutes, greater than zero and at most 10080').ChangeValue('.25')
        dialog.press('Set sleep timer')
        dialog.press('Clear sleep timer')
    dialog.driver = drive
    dialog.show()
    assert dialog.actions == [('skips', 2.5, 7.5), ('sleep', 15), ('sleep', None)]
    assert not dialog.deferred


@pytest.mark.parametrize('value', ['nan', 'inf', '-1', '0', '10081', 'not a number'])
def test_invalid_sleep_is_visible_and_does_not_change_playback(dialog, value):
    def drive(_window):
        dialog.find('Sleep timer in minutes, greater than zero and at most 10080').ChangeValue(value)
        dialog.press('Set sleep timer')
        assert dialog.find('Current reader playback status').GetValue()
    dialog.driver = drive
    dialog.show()
    assert dialog.actions == [] and dialog.deferred == []


@pytest.mark.parametrize('a,b', [('1', '.5'), ('0', 'nan'), ('.1', '.12')])
def test_invalid_ab_is_rejected_before_any_deferred_playback_action(dialog, a, b):
    def drive(_window):
        dialog.find('Repeat A in seconds').ChangeValue(a)
        dialog.find('Repeat B in seconds').ChangeValue(b)
        dialog.press('Apply A–B repeat')
        assert 'Choose A and B' in dialog.find('Current reader playback status').GetValue()
    dialog.driver = drive
    dialog.show()
    assert dialog.actions == [] and dialog.deferred == []


@pytest.fixture
def output_dialog(dialog):
    from carr_library.speech.playback_devices import PlaybackDevice
    dialog.player.output_device = None
    dialog.outputs = (PlaybackDevice(2, 'Навушники 😀', 'opaque-a', 1, 2, 3, 2),
                      PlaybackDevice(4, 'Навушники 😀', 'opaque-b', 1, 2, 3, 2))
    dialog.inventories = []
    def inventory(*, cancelled):
        assert not cancelled()
        dialog.inventories.append(True)
        return dialog.outputs
    def selected(device):
        assert dialog.actions[-1] == ('stop',)
        if device is not None and device not in dialog.outputs:
            raise RuntimeError('Selected output is unavailable; no fallback was used.')
        dialog.player.output_device = device
        dialog.actions.append(('output', device))
    dialog.player.output_devices = inventory
    dialog.player.select_output = selected
    dialog.output = lambda: dialog.find('Requested playback output; apply explicitly after selection')
    dialog.reload = lambda: dialog.press('Reload available playback outputs without playing audio')
    dialog.apply = lambda: dialog.press('Stop audio and use selected playback output')
    return dialog


def test_output_inventory_and_selection_are_explicit_and_never_autoplay(output_dialog):
    view = output_dialog
    def drive(_window):
        assert view.inventories == [] and view.actions == []
        view.reload()
        assert view.inventories == [True] and view.actions == []
        assert view.output().choices[1] != view.output().choices[2]
        view.output().SetSelection(2)
        assert view.player.output_device is None and view.actions == []
        view.apply()
        assert view.actions == [('stop',), ('output', view.outputs[1])]
        assert view.player.output_device == view.outputs[1] and view.deferred == []
        assert 'Start audio explicitly' in view.find('Current reader playback status').GetValue()
    view.driver = drive
    view.show()


def test_missing_selected_output_is_not_silently_reset_to_default(output_dialog):
    view = output_dialog
    view.player.output_device = view.outputs[1]
    def drive(_window):
        assert view.output().GetSelection() == -1
        view.reload()
        assert view.output().GetSelection() == 2
        old = view.player.output_device
        view.outputs = view.outputs[:1]
        view.apply()
        assert view.player.output_device == old and view.actions == [('stop',)]
        view.reload()
        assert view.output().GetSelection() == -1 and view.player.output_device == old
        view.apply()
        assert view.actions == [('stop',)]
        view.output().SetSelection(0)
        view.apply()
        assert view.actions == [('stop',), ('stop',), ('output', None)]
    view.driver = drive
    view.show()


def test_failed_reload_discards_stale_rows_but_preserves_selected_identity(output_dialog):
    view = output_dialog
    view.player.output_device = view.outputs[0]
    def drive(_window):
        view.reload()
        def unavailable(**options): raise RuntimeError('Device inventory unavailable')
        view.player.output_devices = unavailable
        view.reload()
        assert view.output().GetSelection() == -1 and len(view.output().choices) == 1
        view.apply()
        assert view.player.output_device == view.outputs[0] and view.actions == []
        view.subscriptions[0]()
        assert 'Device inventory unavailable' in view.find('Current reader playback status').GetValue()
    view.driver = drive
    view.show()


def test_closed_dialog_ignores_queued_output_handlers(output_dialog):
    view = output_dialog
    retained = []
    def drive(_window):
        view.reload()
        view.output().SetSelection(1)
        retained.extend(view.find(name).handlers['button'] for name in (
            'Reload available playback outputs without playing audio',
            'Stop audio and use selected playback output'))
    view.driver = drive
    view.show()
    for widget in view.widgets: widget.destroyed = True
    for handler in retained: handler(None)
    assert view.actions == [] and view.inventories == [True]


def test_output_without_stable_interface_is_unavailable_before_stop(output_dialog):
    from dataclasses import replace
    view = output_dialog
    view.outputs = (replace(view.outputs[0], interface=''),)
    def drive(_window):
        view.reload()
        view.output().SetSelection(1)
        view.apply()
        assert view.actions == [] and view.player.output_device is None
        assert 'no stable interface identity' in view.find('Current reader playback status').GetValue()
    view.driver = drive
    view.show()
