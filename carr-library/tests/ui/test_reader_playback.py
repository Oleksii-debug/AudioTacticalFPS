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
        self.window_id = 500 + len(self.widgets)
        self.handlers = {}
        self.widgets.append(self)

    def native(self):
        assert get_ident() == self.owner
        assert not self.destroyed

    def GetId(self): self.native(); return self.window_id
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
    def Bind(self, event, handler, *args, **kwargs): self.native(); self.handlers[event] = handler
    def SetAcceleratorTable(self, value): self.native(); self.accelerators = value
    def EndModal(self, result): self.native(); self.ended = result
    def Destroy(self):
        self.native()
        self.destroyed = True
        callback = self.handlers.get('destroy')
        if callback is not None:
            callback(SimpleNamespace(GetId=lambda: self.window_id, Skip=lambda: None))
    def Enable(self, value): self.native(); self.enabled = value


@pytest.fixture
def dialog(monkeypatch):
    widgets, deferred, subscriptions, actions = [], [], [], []
    Widget.widgets = widgets
    fixture = SimpleNamespace(driver=lambda _dialog: None, prepare_listening=None,
                              allow_queue=False, review_playlist=None, save_queue=None,
                              review_saved=None, saved_pending=lambda: False,
                              queue_changed=lambda: None, review_outputs=None,
                              guard=lambda: None, active=lambda: True)
    class Dialog(Widget):
        def ShowModal(self):
            self.native()
            fixture.driver(self)
            return 2
    class StaticText(Widget):
        pass
    wx = SimpleNamespace(Dialog=Dialog, StaticText=StaticText, TextCtrl=Widget,
        Choice=Widget, ListBox=Widget, CheckBox=Widget, ScrolledWindow=Widget,
        Button=Widget, BoxSizer=Widget, GridSizer=Widget, ID_CANCEL=2,
        EVT_BUTTON='button', EVT_MENU='menu', EVT_CLOSE='close', EVT_WINDOW_DESTROY='destroy', ACCEL_CTRL=1,
        NewIdRef=lambda: 71, AcceleratorTable=lambda entries: entries,
        YES_NO=2, NO_DEFAULT=4, ICON_WARNING=8, YES=1, NO=0,
        MessageBox=lambda *args, **kwargs: 0)
    for key in ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'VERTICAL', 'EXPAND', 'ALL',
                'TE_READONLY', 'TE_MULTILINE', 'ALIGN_RIGHT', 'TAB_TRAVERSAL', 'VSCROLL', 'HORIZONTAL'):
        setattr(wx, key, 0)
    monkeypatch.setitem(sys.modules, 'wx', wx)
    player = SimpleNamespace(ab_loop=None, repeat_enabled=False, duration_seconds=3, skip_intervals=(10, 30),
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
    def repeat(enabled=True):
        player.repeat_enabled = enabled
        actions.append(('repeat', enabled))
    player.enable_repeat = repeat
    player.play_current = lambda: actions.append(('play',))
    player.skip_backward = lambda: actions.append(('backward',))
    player.skip_forward = lambda: actions.append(('forward',))
    player.clear_ab_loop = lambda: actions.append(('clear-ab',))
    fixture.player, fixture.actions, fixture.deferred = player, actions, deferred
    fixture.widgets, fixture.subscriptions = widgets, subscriptions
    fixture.subscribe = subscriptions.append
    def execute(action, *, audible=False):
        if audible:
            deferred.append(action)
        else:
            action()
    def show():
        return show_reader_playback(object(), player, execute=execute,
            stop=lambda: actions.append(('stop',)), pause=lambda: actions.append(('pause',)),
            subscribe=fixture.subscribe, prepare_listening=fixture.prepare_listening,
            allow_queue=fixture.allow_queue, review_playlist=fixture.review_playlist,
            save_queue=fixture.save_queue, review_saved=fixture.review_saved,
            saved_pending=fixture.saved_pending, queue_changed=fixture.queue_changed,
            review_outputs=fixture.review_outputs, guard=fixture.guard, active=fixture.active)
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
        dialog.press('Set A–B points')
        assert dialog.actions == [] and len(dialog.deferred) == 1
        dialog.find('Repeat A in seconds').ChangeValue('2')
        dialog.find('Repeat B in seconds').ChangeValue('2.5')
        dialog.deferred.pop(0)()
        assert dialog.actions == [('ab', .25, 1.25)]
    dialog.driver = drive
    dialog.show()


@pytest.mark.parametrize('name', ['Play current recording', 'Set A–B points', 'Skip forward'])
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
        dialog.press('Set A–B points')
        assert 'Choose A and B' in dialog.find('Current reader playback status').GetValue()
    dialog.driver = drive
    dialog.show()
    assert dialog.actions == [] and dialog.deferred == []


@pytest.fixture
def output_dialog(dialog):
    from carr_library.speech.output_devices import OutputDevice
    dialog.player.output_device = None
    dialog.outputs = (OutputDevice(2, 'Навушники 😀', 'opaque-a'),
                      OutputDevice(4, 'Навушники 😀', 'opaque-b'))
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
    def review(completed, failed, *, active):
        try:
            completed(dialog.player.output_devices(cancelled=lambda: not active()))
        except Exception as exc:
            failed(exc)
        return lambda: None
    dialog.review_outputs = review
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
    with pytest.raises(ValueError, match='native device identity'):
        replace(view.outputs[0], interface='')
    assert view.actions == [] and view.player.output_device is None


@pytest.fixture
def listening_dialog(dialog):
    from carr_library.speech.listening import ListeningSettings
    from threading import Event
    dialog.player.listening_settings = ListeningSettings()
    dialog.player.listening_duration_seconds = 3
    dialog.requests, dialog.cancellations = [], []
    def request(speed, pitch, *, position_seconds):
        return SimpleNamespace(settings=ListeningSettings(speed, pitch),
                               position_seconds=position_seconds, token=object())
    dialog.player.listening_request = request
    def prepare(request, completed, failed, *, active):
        cancelled = Event()
        dialog.requests.append((request, completed, failed, active, cancelled))
        def stop():
            dialog.cancellations.append(request.token)
            cancelled.set()
        return stop
    dialog.prepare_listening = prepare
    return dialog


def test_listening_fields_are_frozen_for_worker_and_apply_does_not_play(listening_dialog):
    from threading import Thread
    dialog = listening_dialog
    values = []
    def drive(_window):
        speed = dialog.find('Listening speed, 0.25 to 4; synthesis rate is unchanged')
        pitch = dialog.find('Listening pitch, minus 12 to 12 semitones; speed is independent')
        speed.ChangeValue('2')
        pitch.ChangeValue('7')
        dialog.player.position_seconds = .75
        dialog.press('Stop and prepare listening speed and pitch without autoplay')
        assert dialog.actions == [('stop',)] and len(dialog.requests) == 1
        request, completed, failed, active, cancelled = dialog.requests[0]
        assert not speed.enabled and not pitch.enabled and active() and not cancelled.is_set()
        speed.ChangeValue('.5')
        pitch.ChangeValue('-12')
        worker = Thread(target=lambda: values.append((
            request.settings.speed, request.settings.pitch_semitones, request.position_seconds)))
        worker.start()
        worker.join(2)
        assert not worker.is_alive() and values == [(2, 7, .75)]
        # The parent owns actual conversion/adoption; this callback only updates
        # native controls after that owner reports completion on the UI thread.
        dialog.player.listening_settings = request.settings
        dialog.player.listening_duration_seconds = 1.5
        completed()
        assert speed.enabled and pitch.enabled
        assert 'Start audio explicitly' in dialog.find('Current reader playback status').value
        assert dialog.actions == [('stop',)] and not dialog.deferred
    dialog.driver = drive
    dialog.show()
    assert not dialog.cancellations


def test_closed_listening_controls_cancel_owner_and_ignore_all_late_ui_callbacks(listening_dialog):
    dialog = listening_dialog
    def drive(_window):
        dialog.press('Stop and prepare listening speed and pitch without autoplay')
    dialog.driver = drive
    dialog.show()
    request, completed, failed, active, cancelled = dialog.requests[0]
    assert not active() and cancelled.is_set() and dialog.cancellations == [request.token]
    for widget in dialog.widgets:
        widget.destroyed = True
    completed()
    failed(RuntimeError('Late worker result'))
    assert dialog.actions == [('stop',)]


def test_explicit_listening_cancel_uses_same_owner_and_remains_stopped(listening_dialog):
    dialog = listening_dialog
    def drive(_window):
        dialog.press('Stop and prepare listening speed and pitch without autoplay')
        request, completed, failed, active, cancelled = dialog.requests[0]
        dialog.press('Cancel private listening-copy preparation')
        assert cancelled.is_set() and active()
        assert dialog.actions == [('stop',), ('stop',)]
        failed(InterruptedError('Explicit preparation cancelled'))
        assert dialog.find('Stop and prepare listening speed and pitch without autoplay').enabled
        assert 'cancelled' in dialog.find('Current reader playback status').value
    dialog.driver = drive
    dialog.show()
    assert len(dialog.cancellations) == 1


@pytest.mark.parametrize('value', ['nan', 'inf', '0', '4.1', 'invalid'])
def test_invalid_listening_speed_is_rejected_before_stop_or_worker_submission(listening_dialog, value):
    dialog = listening_dialog
    def drive(_window):
        dialog.find('Listening speed, 0.25 to 4; synthesis rate is unchanged').ChangeValue(value)
        dialog.press('Stop and prepare listening speed and pitch without autoplay')
        assert dialog.find('Current reader playback status').value
    dialog.driver = drive
    dialog.show()
    assert not dialog.actions and not dialog.requests



@pytest.mark.parametrize('boundary', ['cancel', 'unsubscribe'])
def test_listening_close_destroys_native_dialog_when_cleanup_callback_raises(listening_dialog, boundary):
    view = listening_dialog
    prepare = view.prepare_listening
    subscribed = view.subscribe

    def prepare_with_failure(request, completed, failed, *, active):
        cancel = prepare(request, completed, failed, active=active)
        def cancelled():
            cancel()
            if boundary == 'cancel':
                raise RuntimeError('Cancellation callback failed after signalling.')
        return cancelled

    def subscribe(callback):
        subscribed(callback)
        if callback is None and boundary == 'unsubscribe':
            raise RuntimeError('Unsubscribe callback failed after detaching.')

    view.prepare_listening, view.subscribe = prepare_with_failure, subscribe
    view.driver = lambda _window: view.press('Stop and prepare listening speed and pitch without autoplay')
    with pytest.raises(RuntimeError, match='callback failed'):
        view.show()
    request, completed, failed, active, cancelled = view.requests[0]
    assert cancelled.is_set() and not active()
    assert view.cancellations == [request.token]
    assert callable(view.subscriptions[0]) and view.subscriptions[-1] is None
    native_dialogs = [widget for widget in view.widgets if hasattr(widget, 'ShowModal')]
    assert len(native_dialogs) == 1 and native_dialogs[0].destroyed
    for widget in view.widgets:
        widget.destroyed = True
    view.subscriptions[0]()
    completed()
    failed(RuntimeError('Late completion after cleanup failure.'))
    assert view.actions == [('stop',)] and not view.deferred


@pytest.fixture
def levels_dialog(dialog):
    from carr_library.speech._wave_output import PlaybackLevels
    dialog.player.levels = PlaybackLevels(70)
    dialog.player.live_mix_available = True
    def set_levels(volume, *, muted=False, balance=0):
        dialog.actions.append(('levels', volume, muted, balance))
        dialog.player.levels = PlaybackLevels(volume, muted, balance)
    dialog.player.set_levels = set_levels
    return dialog


def test_live_levels_capture_only_reviewed_values_before_quiet_wait(levels_dialog):
    dialog = levels_dialog
    def drive(window):
        dialog.find('This player volume, 0 to 100').ChangeValue('35')
        dialog.find('This player stereo balance, minus 100 left to 100 right').ChangeValue('-25')
        dialog.find('Mute this player only').SetValue(True)
        dialog.press('Apply player volume, mute and balance')
        assert dialog.actions == [] and len(dialog.deferred) == 1
        dialog.find('This player volume, 0 to 100').ChangeValue('100')
        dialog.find('Mute this player only').SetValue(False)
        dialog.deferred.pop(0)()
        assert dialog.actions == [('levels', 35, True, -.25)]
    dialog.driver = drive
    dialog.show()


@pytest.mark.parametrize('volume,balance', [('nan', '0'), ('101', '0'), ('50', 'inf'), ('50', '-101')])
def test_invalid_live_levels_do_not_queue_or_mutate(levels_dialog, volume, balance):
    dialog = levels_dialog
    def drive(window):
        dialog.find('This player volume, 0 to 100').ChangeValue(volume)
        dialog.find('This player stereo balance, minus 100 left to 100 right').ChangeValue(balance)
        dialog.press('Apply player volume, mute and balance')
        assert 'Choose volume' in dialog.find('Current reader playback status').GetValue()
    dialog.driver = drive
    dialog.show()
    assert not dialog.actions and not dialog.deferred


def test_closed_controls_cannot_apply_a_waiting_volume_change(levels_dialog):
    dialog = levels_dialog
    dialog.driver = lambda window: dialog.press('Apply player volume, mute and balance')
    dialog.show()
    for widget in dialog.widgets:
        widget.destroyed = True
    dialog.deferred.pop(0)()
    assert not dialog.actions


def test_unavailable_backend_has_no_enabled_live_mixer(levels_dialog):
    dialog = levels_dialog
    dialog.player.live_mix_available = False
    def drive(window):
        assert not dialog.find('Apply player volume, mute and balance').enabled
        assert not dialog.find('Mute this player only').enabled
    dialog.driver = drive
    dialog.show()
    assert not dialog.actions


@pytest.fixture
def queue_dialog(dialog, monkeypatch):
    from pathlib import Path
    wx = sys.modules['wx']
    class FileDialog(Widget):
        def __enter__(self): return self
        def __exit__(self, *args): self.Destroy()
        def ShowModal(self): return wx.ID_OK
        def GetPaths(self): return ['one.wav', 'two.wav']
    wx.FileDialog, wx.ID_OK = FileDialog, 1
    wx.FD_OPEN, wx.FD_FILE_MUST_EXIST, wx.FD_MULTIPLE = 1, 2, 4
    dialog.allow_queue = True
    items = tuple(SimpleNamespace(path=Path(name), duration_seconds=1)
                  for name in ('one.wav', 'two.wav'))
    dialog.player.playlist = items
    dialog.player.playlist_index = None
    dialog.player.queue_active = False
    dialog.reviews, dialog.review_cancelled = [], []
    def set_playlist(values):
        dialog.actions.append(('queue', tuple(values)))
        dialog.player.playlist = tuple(values)
        dialog.player.playlist_index = None
    dialog.player.set_playlist = set_playlist
    dialog.player.play_queue = lambda index: dialog.actions.append(('play-queue', index))
    dialog.player.select_queue = lambda index: dialog.actions.append(('select-queue', index))
    def review(paths, completed, failed, *, active):
        dialog.reviews.append((paths, completed, failed, active))
        return lambda: dialog.review_cancelled.append(True)
    dialog.review_playlist = review
    dialog.items = items
    return dialog


def test_document_bound_reader_never_exposes_playlist_controls(queue_dialog):
    dialog = queue_dialog
    dialog.allow_queue = False
    def drive(window):
        assert not any(widget.name == 'Play queue from selected item' for widget in dialog.widgets)
    dialog.driver = drive
    dialog.show()
    assert not dialog.actions


def test_playlist_review_stops_then_waits_for_exact_worker_result_without_playing(queue_dialog):
    dialog = queue_dialog
    def drive(window):
        dialog.press('Replace session playlist...')
        assert dialog.actions == [('stop',)]
        paths, completed, failed, active = dialog.reviews[0]
        assert paths == ('one.wav', 'two.wav') and active()
        assert not dialog.find('Play queue from selected item').enabled
        completed(tuple(reversed(dialog.items)))
        assert dialog.actions[-1] == ('queue', tuple(reversed(dialog.items)))
        assert dialog.find('Play queue from selected item').enabled
        assert not any(action[0] in ('play', 'play-queue') for action in dialog.actions)
    dialog.driver = drive
    dialog.show()


def test_late_review_after_modal_close_is_cancelled_and_cannot_replace_queue(queue_dialog):
    dialog = queue_dialog
    dialog.driver = lambda window: dialog.press('Replace session playlist...')
    dialog.show()
    assert dialog.review_cancelled == [True]
    for widget in dialog.widgets:
        widget.destroyed = True
    _, completed, failed, active = dialog.reviews[0]
    assert not active()
    completed(dialog.items)
    failed(ValueError('late'))
    assert dialog.actions == [('stop',)]


def test_queue_item_is_frozen_before_deferred_quiet_admission(queue_dialog):
    dialog = queue_dialog
    def drive(window):
        queue = dialog.find('Reviewed session playlist; choose an item explicitly')
        queue.SetSelection(1)
        dialog.press('Play queue from selected item')
        queue.SetSelection(0)
        assert not dialog.actions
        dialog.deferred.pop(0)()
        assert dialog.actions == [('play-queue', 1)]
    dialog.driver = drive
    dialog.show()


def test_changed_playlist_cannot_retarget_a_deferred_item_action(queue_dialog):
    dialog = queue_dialog
    def drive(window):
        dialog.press('Play queue from selected item')
        dialog.player.playlist = tuple(reversed(dialog.items))
        with pytest.raises(ValueError, match='playlist changed'):
            dialog.deferred.pop(0)()
        assert not dialog.actions
    dialog.driver = drive
    dialog.show()


def test_reordering_and_removal_are_silent_owned_actions(queue_dialog):
    dialog = queue_dialog
    def drive(window):
        queue = dialog.find('Reviewed session playlist; choose an item explicitly')
        queue.SetSelection(0)
        dialog.press('Stop and move item down')
        assert dialog.player.playlist == tuple(reversed(dialog.items))
        queue.SetSelection(1)
        dialog.press('Stop and remove item')
        assert dialog.player.playlist == (dialog.items[1],)
        assert not dialog.deferred
    dialog.driver = drive
    dialog.show()
    assert all(action[0] == 'queue' for action in dialog.actions)


@pytest.fixture
def saved_queue_dialog(queue_dialog):
    dialog = queue_dialog
    dialog.saved_calls, dialog.restore_calls, dialog.changed = [], [], []
    dialog.save_queue = lambda: (dialog.saved_calls.append(True), 'Durably saved exact queue.')[1]
    dialog.pending_recovery = True
    dialog.saved_pending = lambda: dialog.pending_recovery
    dialog.queue_changed = lambda: dialog.changed.append(True)
    def review(completed, failed, *, active, use_current_controls):
        dialog.restore_calls.append((completed, failed, active, use_current_controls))
        return lambda: dialog.review_cancelled.append(True)
    dialog.review_saved = review
    return dialog


def test_ctrl_s_calls_real_owned_save_callback_and_displays_its_completed_result(saved_queue_dialog):
    dialog = saved_queue_dialog
    def drive(window):
        assert window.accelerators == [(1, ord('S'), 71)]
        window.handlers['menu'](None)
        assert dialog.saved_calls == [True]
        assert 'Durably saved exact queue.' in dialog.find('Current reader playback status').GetValue()
        assert dialog.actions == [] and not dialog.deferred
    dialog.driver = drive
    dialog.show()


def test_saved_queue_review_freezes_explicit_controls_choice_and_never_calls_play(saved_queue_dialog):
    dialog = saved_queue_dialog
    def drive(window):
        choice = dialog.find('Explicitly replace saved playback settings with current controls')
        choice.SetValue(True)
        dialog.press('Review saved queue without playing')
        assert dialog.actions == [('stop',)]
        completed, failed, active, current = dialog.restore_calls[0]
        assert current is True and active()
        choice.SetValue(False)
        dialog.pending_recovery = False
        completed()
        assert not dialog.find('Review saved queue without playing').enabled
        assert not any(item[0] in ('play', 'play-queue') for item in dialog.actions)
    dialog.driver = drive
    dialog.show()


def test_failed_queue_save_vetoes_close_by_default_and_preserves_controls(saved_queue_dialog):
    dialog = saved_queue_dialog
    def fail():
        raise OSError('synthetic storage failure')
    dialog.save_queue = fail
    def drive(window):
        dialog.press('Close playback controls')
        assert not hasattr(window, 'ended')
        assert 'storage failure' in dialog.find('Current reader playback status').GetValue()
        assert not window.destroyed
    dialog.driver = drive
    dialog.show()


def test_confirmed_close_without_saving_is_explicit(saved_queue_dialog):
    dialog = saved_queue_dialog
    messages = []
    def answer(*args, **kwargs):
        messages.append((args, kwargs))
        return 1
    sys.modules['wx'].MessageBox = answer
    dialog.save_queue = lambda: (_ for _ in ()).throw(OSError('synthetic storage failure'))
    def drive(window):
        dialog.press('Close playback controls')
        assert window.ended == 2
        assert messages[0][0][2] & sys.modules['wx'].NO_DEFAULT
    dialog.driver = drive
    dialog.show()


def test_queue_mutation_notifies_only_after_actual_order_is_changed(saved_queue_dialog):
    dialog = saved_queue_dialog
    def drive(window):
        dialog.press('Stop and move item down')
        assert dialog.player.playlist == tuple(reversed(dialog.items))
        assert dialog.changed == [True]
        dialog.press('Stop and clear playlist')
        assert dialog.player.playlist == () and dialog.changed == [True, True]
    dialog.driver = drive
    dialog.show()


def test_late_saved_review_completion_cannot_touch_destroyed_controls(saved_queue_dialog):
    dialog = saved_queue_dialog
    dialog.driver = lambda window: dialog.press('Review saved queue without playing')
    dialog.show()
    assert dialog.review_cancelled == [True]
    completed, failed, active, _ = dialog.restore_calls[0]
    assert not active()
    for widget in dialog.widgets:
        widget.destroyed = True
    completed()
    failed(RuntimeError('late failure'))
    assert dialog.actions == [('stop',)]


def test_points_and_enabling_repeat_are_separate_explicit_actions(dialog):
    def drive(window):
        dialog.find('Repeat A in seconds').ChangeValue('.2')
        dialog.find('Repeat B in seconds').ChangeValue('1')
        dialog.press('Set A–B points')
        dialog.deferred.pop(0)()
        assert dialog.player.ab_loop == (.2, 1) and not dialog.player.repeat_enabled
        assert dialog.actions == [('ab', .2, 1)]
        dialog.press('Enable A–B repeat')
        assert not dialog.player.repeat_enabled
        dialog.deferred.pop(0)()
        assert dialog.actions[-1] == ('repeat', True)
        dialog.press('Disable A–B repeat')
        dialog.deferred.pop(0)()
        assert dialog.actions[-1] == ('repeat', False)
    dialog.driver = drive
    dialog.show()


def test_actual_owner_destroy_revokes_queued_commands_without_dying_wrapper_calls(dialog):
    retained = []
    def drive(window):
        dialog.press('Play current recording')
        retained.extend(dialog.deferred)
        window.Destroy()
        assert not dialog.actions
        for widget in dialog.widgets:
            widget.destroyed = True
        retained[0]()
        assert not dialog.actions
    dialog.driver = drive
    dialog.show()
    assert dialog.subscriptions[-1] is None


def test_child_destroy_does_not_revoke_owner_controls(dialog):
    def drive(window):
        window.handlers['destroy'](SimpleNamespace(GetId=lambda: window.window_id + 1, Skip=lambda: None))
        dialog.press('Apply skip intervals')
        assert dialog.actions == [('skips', 10, 30)]
    dialog.driver = drive
    dialog.show()


@pytest.mark.parametrize('answer', [0, 1], ids=['decline', 'approve'])
def test_forced_destroy_during_unsaved_close_confirmation_never_touches_controls(saved_queue_dialog, answer):
    dialog = saved_queue_dialog
    dialog.save_queue = lambda: (_ for _ in ()).throw(OSError('Storage unavailable'))
    def drive(window):
        def prompt(*args, **kwargs):
            window.Destroy()
            for widget in dialog.widgets:
                widget.destroyed = True
            return answer
        sys.modules['wx'].MessageBox = prompt
        dialog.press('Close playback controls')
        assert not hasattr(window, 'ended')
    dialog.driver = drive
    dialog.show()


def test_source_revocation_during_quiet_wait_never_executes_a_play_command(dialog):
    valid = [True]
    def guard():
        if not valid[0]:
            raise ValueError('The source scope changed.')
    dialog.guard = guard
    def drive(window):
        dialog.press('Play current recording')
        valid[0] = False
        with pytest.raises(ValueError, match='scope'):
            dialog.deferred.pop(0)()
        assert not dialog.actions
    dialog.driver = drive
    dialog.show()


def test_late_output_review_after_forced_close_never_uses_destroyed_widgets(output_dialog):
    dialog, callbacks = output_dialog, []
    dialog.review_outputs = lambda done, failed, **kwargs: (
        callbacks.append((done, failed, kwargs['active'])), lambda: callbacks.append('cancelled'))[1]
    def drive(window):
        dialog.reload()
        window.Destroy()
        for widget in dialog.widgets:
            widget.destroyed = True
        done, failed, active = callbacks[0]
        assert not active()
        done(dialog.outputs)
        failed(RuntimeError('late'))
    dialog.driver = drive
    dialog.show()
    assert callbacks[-1] == 'cancelled'


# Seek runs the installed reader's actual nested callbacks with a real PCM file
# and WavePlayer. Only native widgets/device calls are modeled here; the native
# navigation probe separately drives the same dialog on the reviewed endpoint.
@pytest.fixture
def reader_seek(tmp_path, monkeypatch):
    import ast
    from hashlib import sha256
    import math
    from pathlib import Path
    import threading
    import wave

    from carr_library.speech import ReadingDocument
    from carr_library.speech.focus import ReadingAudio
    from carr_library.speech import player as player_module
    from carr_library.speech.player import WavePlayer
    from carr_library.speech.progress_speech import play_when_quiet
    from carr_library.ui import speech_dialog

    path = tmp_path / 'reviewed seek PCM.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'uncompressed'))
        stream.writeframes(b'\0\0' * 16000)
    original = path.read_bytes()
    reading = ReadingDocument('Original words remain unchanged.', title='Seek fixture')
    signature = reading._signature()
    result = SimpleNamespace(path=path, frames=16000, sample_rate=8000,
        text_sha256=sha256(reading.text.encode('utf-8')).hexdigest())
    binding = ReadingAudio.from_render(reading, signature, 0, len(reading.text), reading.text, result)

    clock = SimpleNamespace(value=100.0)
    native = SimpleNamespace(commands=[], mode='stopped', offset=0.0, started=100.0, end=2.0)
    def send(command):
        native.commands.append(command)
        words = command.split()
        operation = words[0]
        if operation == 'play':
            native.offset = int(words[words.index('from') + 1]) / 1000
            native.end = int(words[words.index('to') + 1]) / 1000 if 'to' in words else 2.0
            native.started, native.mode = clock.value, 'playing'
        elif operation == 'status':
            if native.mode == 'playing':
                position = min(native.end, native.offset + clock.value - native.started)
                if position == native.end:
                    native.offset, native.mode = position, 'stopped'
            else:
                position = native.offset
            return native.mode if words[-1] == 'mode' else str(round(position * 1000))
        elif operation == 'pause':
            native.offset += clock.value - native.started
            native.mode = 'paused'
        elif operation == 'resume':
            native.started, native.mode = clock.value, 'playing'
        elif operation in ('stop', 'close'):
            native.mode = 'stopped'
        return ''
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    # This fixture models MCI's protocol, independently of temporary path length.
    # Actual long-path routing remains covered by the unmodified waveOut suite.
    monkeypatch.setattr(player_module, '_mci_filename_supported', lambda value: True)
    player = WavePlayer(clock=lambda: clock.value)
    monkeypatch.setattr(player, '_mci', send)
    monkeypatch.setattr(player, '_mci_open',
        lambda value: send(f'open "{value}" type waveaudio alias {player._alias}'))
    player.play(path)
    assert player.duration_seconds == 2.0

    pending, holds, reports = [], [], []
    class Progress:
        # OperationProgress is identity-hashable; real quiet admission deduplicates it.
        ready, refreshes = True, 0
    progress = Progress()
    def hold_audio():
        class Hold:
            closed = False
            @property
            def ready(self):
                return progress.ready
            def close(self):
                self.closed = True
        hold = Hold()
        holds.append(hold)
        return hold
    def refresh():
        progress.refreshes += 1
    progress.hold_audio, progress.refresh_audio_state = hold_audio, refresh
    def schedule(milliseconds, callback):
        assert milliseconds == 25
        pending.append(callback)

    owner = SimpleNamespace(shown=True, destroyed=False, checks=0)
    def is_shown():
        assert not owner.destroyed, 'A revoked native owner was queried.'
        owner.checks += 1
        return owner.shown
    owner.IsShown = is_shown
    text = SimpleNamespace(value=reading.text, destroyed=False, reads=0)
    def get_text():
        assert not text.destroyed, 'A revoked reading editor was queried.'
        text.reads += 1
        return text.value
    text.GetValue = get_text
    status = SimpleNamespace(ChangeValue=reports.append, SetFocus=lambda: None)
    source_status = SimpleNamespace(ChangeValue=lambda message: None)
    scope = SimpleNamespace(allowed=True, checks=0)
    def source_guard():
        scope.checks += 1
        if not scope.allowed:
            raise ValueError('Retained source authority was revoked.')
    library = SimpleNamespace(root=tmp_path)
    parent = SimpleNamespace(library=library, _closing=False)
    entry = SimpleNamespace(value='50%', answer=1, opened=0, closed=0, reads=0,
                            destroyed=False, hook=lambda: None, prompt='', default='')
    class Entry:
        def __init__(self, actual_owner, prompt, title, default):
            assert actual_owner is owner and title == 'Seek playback'
            entry.prompt, entry.default = prompt, default
            entry.opened += 1
        def __enter__(self):
            return self
        def __exit__(self, *error):
            entry.closed += 1
        def ShowModal(self):
            entry.hook()
            return entry.answer
        def GetValue(self):
            assert not entry.destroyed, 'A closed modal value was queried.'
            entry.reads += 1
            return entry.value
    wx = SimpleNamespace(TextEntryDialog=Entry, ID_OK=1, CallLater=schedule)
    values = dict(parent=parent, reader_library=library, reader_root=tmp_path, source_guard=source_guard,
        alive=True, dialog_destroyed=False, close_requested=False, busy=False, cancel=threading.Event(),
        playback_generation=0, player=player, last_audio=path, last_reading_audio=binding,
        document=reading, dialog=owner, dialog_id=91, editor=text, source_status=source_status,
        recovery_notice='', progress=progress, status=status, wx=wx)
    source = Path(speech_dialog.__file__)
    window = next(node for node in ast.parse(source.read_text(encoding='utf-8')).body
                  if isinstance(node, ast.FunctionDef) and node.name == 'show_speech_reader')
    names = {'report', 'playback_guard', 'sync_document', 'start_playback', 'seek_audio',
             'stop_audio', 'pause_audio', 'owner_destroyed'}
    definitions = [node for node in window.body
                   if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in definitions} == names
    # Keep the real callbacks and mutable closure cells, including Stop/Destroy.
    setters = ('document', 'last_audio', 'last_reading_audio', 'playback_generation',
               'alive', 'busy', 'close_requested')
    helper = 'def change(**changes):\n    nonlocal ' + ', '.join(setters) + '\n'
    helper += '    assert set(changes) <= ' + repr(set(setters)) + '\n'
    for name in setters:
        helper += f"    if {name!r} in changes:\n        {name} = changes[{name!r}]\n"
    helper += '\ndef current():\n    return {' + ', '.join(repr(name) + ': ' + name for name in values) + '}\n'
    factory = ast.parse('def build(' + ', '.join(values) + '):\n    pass\n')
    factory.body[0].body = definitions + ast.parse(helper).body + ast.parse(
        'return SimpleNamespace(' + ', '.join(name + '=' + name for name in sorted(names))
        + ', change=change, current=current)').body
    namespace = dict(math=math, Path=Path, SimpleNamespace=SimpleNamespace, play_when_quiet=play_when_quiet)
    exec(compile(ast.fix_missing_locations(factory), str(source), 'exec'), namespace)
    callbacks = namespace['build'](**values)
    fixture = SimpleNamespace(callbacks=callbacks, player=player, path=path, original=original,
        reading=reading, binding=binding, native=native, clock=clock, entry=entry, owner=owner,
        text=text, parent=parent, library=library, scope=scope, progress=progress,
        pending=pending, holds=holds, reports=reports, mutated=False,
        starts=lambda: sum(command.startswith('play ') for command in native.commands))
    yield fixture
    callbacks.change(alive=False)
    progress.ready = True
    while pending:
        pending.pop(0)()
    player.close()
    assert all(hold.closed for hold in holds)
    if not fixture.mutated:
        assert path.read_bytes() == original


@pytest.mark.parametrize(('entered', 'position'), [
    ('0%', 0), ('25%', .5), ('50%', 1), ('100%', 2), (' 62.5 % ', 1.25),
    ('1.25', 1.25), ('0', 0),
])
def test_reader_seek_measures_percentages_and_keeps_seconds_and_pause(reader_seek, entered, position):
    value = reader_seek
    assert value.player.pause()
    initial_starts = value.starts()
    value.entry.value = entered
    value.callbacks.seek_audio()
    assert value.entry.opened == value.entry.closed == value.entry.reads == 1
    assert '0% to 100%' in value.entry.prompt and 'seconds' in value.entry.prompt
    assert value.player.position_seconds == pytest.approx(position)
    assert value.player.state == 'paused' and value.starts() == initial_starts
    assert value.player.path == value.path and value.player.duration_seconds == 2
    assert value.reading._signature() == value.binding.signature
    assert value.progress.refreshes == 1 and all(hold.closed for hold in value.holds)
    assert value.reports[-1].startswith('Playback at ')


def test_reader_seek_waits_for_existing_quiet_policy_before_same_player_restart(reader_seek):
    value = reader_seek
    value.progress.ready = False
    value.entry.value = '50%'
    value.callbacks.seek_audio()
    assert value.starts() == 1 and len(value.pending) == 1
    assert len(value.holds) == 1 and not value.holds[0].closed
    value.progress.ready = True
    value.pending.pop(0)()
    assert value.starts() == 2 and value.player.position_seconds == 1
    assert value.player.state == 'playing' and value.holds[0].closed
    assert any(command.endswith('from 1000') for command in value.native.commands)


@pytest.mark.parametrize('entered', [
    '-1%', '100.001%', 'nan%', 'inf%', '1e309%', '%', '50%%',
    '-.001', '2.001', 'nan', 'inf', '', '1' * 65,
])
def test_reader_seek_rejects_invalid_or_unbounded_input_before_admission(reader_seek, entered):
    value = reader_seek
    before = (value.player.session_generation, value.player.position_seconds, value.starts())
    value.entry.value = entered
    value.callbacks.seek_audio()
    assert (value.player.session_generation, value.player.position_seconds, value.starts()) == before
    assert not value.holds and not value.pending
    assert value.entry.opened == value.entry.closed == 1 and value.reports
    assert not value.reports[-1].startswith('Playback at ')


@pytest.mark.parametrize('missing', ['audio', 'binding', 'different-binding-path', 'different-duration'])
def test_reader_seek_requires_the_actual_rendered_recording_before_opening(reader_seek, missing):
    from dataclasses import replace
    value = reader_seek
    if missing == 'audio':
        value.callbacks.change(last_audio=None)
    elif missing == 'binding':
        value.callbacks.change(last_reading_audio=None)
    elif missing == 'different-binding-path':
        value.callbacks.change(last_reading_audio=replace(value.binding, path=value.path.with_name('not-read.wav')))
    else:
        value.callbacks.change(last_reading_audio=replace(value.binding, frames=value.binding.frames + 1))
    value.callbacks.seek_audio()
    assert not value.entry.opened and not value.holds and value.starts() == 1
    assert value.reports and not value.reports[-1].startswith('Playback at ')


@pytest.mark.parametrize('phase', ['modal', 'quiet'])
@pytest.mark.parametrize('mutation', [
    'stop-then-clear-cancel', 'external-stop', 'new-render', 'binding-replacement',
    'same-text-tab', 'reading-edit', 'source-revoked', 'library-changed',
    'duration-changed', 'hidden-close', 'destroy',
])
def test_reader_seek_cannot_target_a_changed_recording_or_reader(reader_seek, phase, mutation):
    from dataclasses import replace
    from carr_library.speech import ReadingDocument
    value = reader_seek
    value.player.pause()
    changed = {}
    def mutate():
        if mutation == 'stop-then-clear-cancel':
            value.callbacks.stop_audio()
            value.callbacks.current()['cancel'].clear()
        elif mutation == 'external-stop':
            value.player.stop()
        elif mutation == 'new-render':
            replacement = value.path.with_name('replacement PCM.wav')
            replacement.write_bytes(value.original)
            value.player.play(replacement, start_seconds=.25)
            value.player.pause()
            value.callbacks.change(last_audio=replacement,
                last_reading_audio=replace(value.binding, path=replacement))
        elif mutation == 'binding-replacement':
            value.callbacks.change(last_reading_audio=replace(value.binding))
        elif mutation == 'same-text-tab':
            replacement = ReadingDocument(value.reading.text, title=value.reading.title)
            assert replacement._signature() == value.reading._signature()
            value.callbacks.change(document=replacement)
        elif mutation == 'reading-edit':
            value.text.value = value.reading.text + ' A new reading-only edit.'
        elif mutation == 'source-revoked':
            value.scope.allowed = False
        elif mutation == 'library-changed':
            value.parent.library = SimpleNamespace(root=value.library.root)
        elif mutation == 'duration-changed':
            value.player.duration_seconds += .125
        elif mutation == 'hidden-close':
            value.owner.shown = False
        else:
            skipped = []
            value.callbacks.owner_destroyed(SimpleNamespace(GetId=lambda: 91, Skip=lambda: skipped.append(True)))
            assert skipped == [True]
            value.owner.destroyed = value.text.destroyed = value.entry.destroyed = True
        changed.update(path=value.player.path, position=value.player.position_seconds,
                       session=value.player.session_generation, starts=value.starts())
    if phase == 'modal':
        value.entry.hook = mutate
        value.callbacks.seek_audio()
        assert value.entry.reads == 0
    else:
        value.progress.ready = False
        value.callbacks.seek_audio()
        assert len(value.pending) == 1 and value.entry.reads == 1
        mutate()
        value.progress.ready = True
        value.pending.pop(0)()
    assert value.player.path == changed['path']
    assert value.player.position_seconds == changed['position']
    assert value.player.session_generation == changed['session']
    assert value.starts() == changed['starts']
    assert not value.pending and all(hold.closed for hold in value.holds)
    assert value.path.read_bytes() == value.original
    assert value.reading.source_text == 'Original words remain unchanged.'
    assert not any(message.startswith('Playback at ') for message in value.reports)


def test_reader_seek_preserves_existing_pcm_identity_refusal_after_quiet_wait(reader_seek):
    value = reader_seek
    value.progress.ready = False
    value.callbacks.seek_audio()
    assert len(value.pending) == 1 and value.starts() == 1
    value.path.write_bytes(value.original + b'changed-after-review')
    value.mutated = True
    value.progress.ready = True
    value.pending.pop(0)()
    assert value.starts() == 1 and value.player.state == 'stopped'
    assert value.holds[0].closed and not value.pending
    assert 'original recording changed' in value.reports[-1]
    assert value.reading.source_text == 'Original words remain unchanged.'


@pytest.mark.parametrize(('entered', 'accepted'), [('25%', True), ('75%', False)])
def test_reader_seek_retains_same_player_ab_range_policy(reader_seek, entered, accepted):
    value = reader_seek
    value.player.pause()
    value.player.set_ab_loop(.2, .8)
    value.player.enable_repeat()
    before = value.player.position_seconds
    starts = value.starts()
    value.entry.value = entered
    value.callbacks.seek_audio()
    assert value.player.ab_loop == (.2, .8) and value.player.repeat_enabled
    assert value.player.state == 'paused' and value.starts() == starts
    assert value.player.position_seconds == (.5 if accepted else before)
    if accepted:
        assert value.reports[-1].startswith('Playback at ')
    else:
        assert 'Disable A/B' in value.reports[-1]


def test_reader_seek_cancelled_modal_never_reads_input_or_schedules_audio(reader_seek):
    value = reader_seek
    value.entry.answer = 0
    value.entry.destroyed = True
    value.callbacks.seek_audio()
    assert value.entry.opened == value.entry.closed == 1 and value.entry.reads == 0
    assert not value.holds and not value.pending and value.starts() == 1


def test_reader_seek_rechecks_source_authority_before_even_opening_modal(reader_seek):
    value = reader_seek
    value.scope.allowed = False
    value.callbacks.seek_audio()
    assert not value.entry.opened and not value.holds and value.starts() == 1
    assert 'source authority' in value.reports[-1]
