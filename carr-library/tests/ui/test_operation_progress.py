"""Native progress lifecycle and handlers using explicit, checked wx doubles.

These tests do not constitute native Windows or screen-reader acceptance.
"""
from types import SimpleNamespace
from threading import Thread, get_ident
import sys

import pytest

from carr_library.progress import FeedbackPreferences, ProgressPreferenceStore
from carr_library.ui.operation_progress import ProgressActions, ProgressPanel, install_progress_actions


class Control:
    def __init__(self, parent=None, **kwargs):
        self.parent, self.kwargs = parent, kwargs
        self.alive = True
        self.name = ''
        self.value = None
        self.focus = self.pulses = 0
        self.calls = []
        self.bindings = []
        self.sizer = None
    def _touch(self, action):
        assert self.alive, 'A late callback accessed a deleted native control'
        self.calls.append((action, get_ident()))
    def SetName(self, name):
        self._touch('name')
        self.name = name
    def SetMinSize(self, value):
        self._touch('size')
    def SetSizer(self, sizer):
        self._touch('sizer')
        self.sizer = sizer
    def Bind(self, *args):
        self._touch('bind')
        self.bindings.append(args)
    def ChangeValue(self, value):
        self._touch('text')
        self.value = value
    def SetValue(self, value):
        self._touch('value')
        self.value = value
    def Pulse(self):
        self._touch('pulse')
        self.pulses += 1
    def SetFocus(self):
        self._touch('focus')
        self.focus += 1
    def Layout(self):
        self._touch('layout')


class Sizer:
    def __init__(self, *args):
        self.items = []
    def Add(self, item, *args):
        self.items.append(item)
    def Insert(self, index, item, *args):
        self.items.insert(index, item)
    def GetChildren(self):
        return [SimpleNamespace(GetWindow=lambda value=value: value) for value in self.items]


class Timer:
    def __init__(self, owner):
        self.running = False
    def Start(self, interval):
        self.interval, self.running = interval, True
    def Stop(self):
        self.running = False


class Menu:
    def __init__(self):
        self.items = []
    def Append(self, identity, label):
        item = SimpleNamespace(label=label)
        self.items.append(item)
        return item


class MenuBar:
    def __init__(self):
        self.menus = []
    def Append(self, menu, label):
        self.menus.append((menu, label))
    def GetMenuCount(self):
        return len(self.menus)
    def GetMenu(self, index):
        return self.menus[index][0]
    def GetMenuLabel(self, index):
        return self.menus[index][1]


def wx_double():
    return SimpleNamespace(Panel=Control, StaticText=Control, Gauge=Control, TextCtrl=Control,
        BoxSizer=Sizer, Timer=Timer, Menu=Menu, VERTICAL=1, EXPAND=2, ALL=4,
        TE_READONLY=8, TE_MULTILINE=16, EVT_TIMER='timer', EVT_WINDOW_DESTROY='destroy',
        EVT_MENU='menu', ID_ANY=-1)


class Feedback:
    def __init__(self, preferences):
        self.preferences, self.events, self.closed = preferences, [], False
        self.notice = ''
        self.media = (False, False)
        self.background = False
    def submit(self, item, *, manual=False):
        assert not self.closed
        self.events.append((item, manual))
    def configure(self, preferences):
        assert not self.closed
        self.preferences = preferences
    def set_media_state(self, *, playback, recording):
        assert not self.closed
        self.media = (playback, recording)
    def set_background(self, value):
        assert not self.closed
        self.background = value
    def close(self):
        self.closed = True


@pytest.fixture
def panel():
    value = ProgressPanel(Control(), wx_module=wx_double(), feedback_factory=Feedback)
    yield value
    value.dispose()


def test_producer_thread_never_touches_controls_and_tick_never_steals_focus(panel):
    ui_thread = get_ident()
    handle = panel.start('import', 'Import books', 'Extracting', total=20)
    failures = []
    def produce():
        try:
            for amount in range(1, 11):
                panel.update(handle, completed=amount)
        except Exception as exc:
            failures.append(exc)
    thread = Thread(target=produce)
    thread.start()
    thread.join(timeout=2)
    assert not failures and not thread.is_alive()
    panel._tick()
    assert panel.gauge.value == 50
    assert '50%; 10 of 20' in panel.text.value
    assert panel.text.focus == 0
    assert all(thread_id == ui_thread for control in (panel.window, panel.text, panel.gauge)
               for action, thread_id in control.calls)
    assert len(panel.feedback.events) == 1
    panel.query_current()
    assert panel.text.focus == 1 and panel.feedback.events[-1][1] is True


def test_unknown_uses_pulse_but_pause_cancel_and_failure_stop_it(panel):
    handle = panel.start('discover', 'Discover sources')
    panel._tick()
    count = panel.gauge.pulses
    assert count > 0 and 'Total unknown' in panel.text.value
    panel.pause(handle)
    panel._tick()
    assert 'Paused' in panel.text.value and panel.gauge.pulses == count
    panel.resume(handle)
    panel._tick()
    assert panel.gauge.pulses > count
    count = panel.gauge.pulses
    panel.finish(handle, state='cancelled')
    panel._tick()
    assert 'Cancelled' in panel.text.value and panel.gauge.pulses == count
    second = panel.start('fail', 'Second source')
    panel.finish(second, state='failed')
    panel._tick()
    assert 'Failed' in panel.text.value and panel.gauge.pulses == count


def test_final_units_remain_verification_pending_until_real_finish(panel):
    handle = panel.start('save', 'Save snapshot', 'Copying', total=7)
    panel.update(handle, completed=7)
    panel._tick()
    assert 'final verification pending' in panel.text.value
    assert panel.gauge.value != 100
    panel.finish(handle, verified=True)
    panel._tick()
    assert panel.gauge.value == 100 and 'verified complete' in panel.text.value


def test_concurrent_tasks_have_separate_text_without_fake_aggregate(panel):
    first = panel.start('first', 'First task', total=10)
    second = panel.start('second', 'Second task', total=8)
    panel.update(first, completed=1)
    panel.update(second, completed=6)
    panel._tick()
    assert panel.gauge.value == 75
    assert '2 active operations. The gauge shows Second task.' in panel.text.value
    assert '10%; 1 of 10' in panel.text.value and '75%; 6 of 8' in panel.text.value
    assert panel.text.focus == 0


def test_destroy_with_pending_updates_withdraws_timer_feedback_and_controls(panel):
    handle = panel.start('work', 'Work')
    feedback = panel.feedback
    event = SimpleNamespace(GetEventObject=lambda: panel.window, Skip=lambda: None)
    panel._destroyed(event)
    for control in (panel.window, panel.text, panel.gauge):
        control.alive = False
    assert not panel.alive and not panel.timer.running and feedback.closed
    assert panel.update(handle, completed=20) is None
    assert panel.finish(handle, verified=True) is None
    panel._tick()
    panel.query_current()
    panel.dispose()


def test_child_destroy_does_not_dispose_parent_progress(panel):
    panel._destroyed(SimpleNamespace(GetEventObject=lambda: panel.text, Skip=lambda: None))
    assert panel.alive and panel.timer.running


def test_scope_guard_drops_old_worker_even_before_ui_reload(panel):
    bound = [True]
    panel.current = lambda: bound[0]
    handle = panel.start('read', 'Reading')
    bound[0] = False
    assert panel.update(handle, completed=12) is None
    assert panel.start('new', 'New') is None
    panel._tick()
    assert not panel.feedback.events


def test_start_cannot_cross_reload_between_scope_check_and_publication(panel):
    def replace_binding_during_check():
        panel.rebind(FeedbackPreferences(), current=lambda: True)
        return True
    panel.current = replace_binding_during_check
    assert panel.start('late', 'Previous folder operation') is None
    assert panel.hub.snapshot() == ()


def make_frame(root):
    frame = SimpleNamespace(library=SimpleNamespace(root=root), _closing=False,
        host=Control(), layout=Sizer(), status=Control(), menu_bar=MenuBar(), bindings=[], active=True)
    frame.IsActive = lambda: frame.active
    frame.Bind = lambda *args: frame.bindings.append(args)
    frame.layout.Add(frame.status)
    return frame


def make_actions(frame, **kwargs):
    return ProgressActions(frame, wx_module=wx_double(),
        panel_factory=lambda parent, **settings: ProgressPanel(parent, feedback_factory=Feedback, **settings), **kwargs)


def test_data_folder_reload_invalidates_handles_audio_and_preferences(tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    ProgressPreferenceStore(tmp_path).save(FeedbackPreferences('Beep', volume=19))
    frame = make_frame(tmp_path)
    actions = make_actions(frame)
    try:
        assert actions.preferences.volume == 19
        old = actions.panel.start('import', 'Previous library')
        old_feedback = actions.panel.feedback
        frame.library = SimpleNamespace(root=other)
        actions.panel._tick()
        assert old_feedback.closed and actions.preferences.mode == 'Off'
        assert actions.panel.update(old, completed=12) is None
        assert actions.panel.hub.snapshot() == ()
        fresh = actions.panel.start('import', 'New library')
        assert fresh != old
        assert not (other / 'operation-progress.json').exists()
    finally:
        actions.panel.dispose()


def test_parent_background_and_media_state_reach_independent_audio_worker(tmp_path):
    frame = make_frame(tmp_path)
    actions = make_actions(frame)
    try:
        frame.active = False
        actions.panel._tick()
        assert actions.panel.feedback.background
        actions.panel.set_media_state(playback=True)
        assert actions.panel.feedback.media == (True, False)
        actions.panel.set_media_state(recording=True)
        assert actions.panel.feedback.media == (False, True)
    finally:
        actions.panel.dispose()


def test_foreground_child_dialog_is_not_mistaken_for_background_app(tmp_path):
    frame = make_frame(tmp_path)
    actions = make_actions(frame)
    try:
        frame.active = False
        actions.wx.GetActiveWindow = lambda: SimpleNamespace(GetParent=lambda: frame)
        actions.panel._tick()
        assert not actions.panel.feedback.background
    finally:
        actions.panel.dispose()


def test_main_window_close_withdraws_audio_without_waiting_for_destroy_event(tmp_path):
    frame = make_frame(tmp_path)
    actions = make_actions(frame)
    feedback = actions.panel.feedback
    frame._closing = True
    actions.panel._tick()
    assert feedback.closed and not actions.panel.alive and not actions.panel.timer.running


def test_preferences_dialog_cannot_save_to_a_different_data_folder(tmp_path):
    other = tmp_path / 'other'
    other.mkdir()
    frame = make_frame(tmp_path)
    class ChangedDialog:
        def __init__(self, *args, **kwargs):
            pass
        def show(self):
            frame.library = SimpleNamespace(root=other)
            return FeedbackPreferences('Speech')
    actions = make_actions(frame, dialog_factory=ChangedDialog)
    try:
        actions.configure()
        assert 'not saved' in actions.panel.text.value
        assert not (tmp_path / 'operation-progress.json').exists()
        assert not (other / 'operation-progress.json').exists()
        assert actions.panel.text.focus == 0
    finally:
        actions.panel.dispose()


def test_invalid_saved_preferences_stay_off_and_are_not_silently_rewritten(tmp_path):
    path = tmp_path / 'operation-progress.json'
    path.write_bytes(b'{"untrusted":true}')
    actions = make_actions(make_frame(tmp_path))
    try:
        assert actions.preferences == FeedbackPreferences()
        assert 'could not be read' in actions.panel.text.value
        actions.mute()
        assert path.read_bytes() == b'{"untrusted":true}'
    finally:
        actions.panel.dispose()


def test_explicit_mute_persists_and_query_requests_focus_only_on_demand(tmp_path):
    actions = make_actions(make_frame(tmp_path))
    try:
        actions.mute()
        assert ProgressPreferenceStore(tmp_path).load().muted
        assert actions.panel.text.focus == 0
        assert 'No operations' in actions.query_current()
        assert actions.panel.text.focus == 1
    finally:
        actions.panel.dispose()


def test_installer_uses_existing_options_and_status_position_without_shortcuts(tmp_path, monkeypatch):
    wx = wx_double()
    monkeypatch.setitem(sys.modules, 'wx', wx)
    frame = make_frame(tmp_path)
    old_menu = Menu()
    frame.menu_bar.Append(Menu(), '&Library')
    frame.menu_bar.Append(old_menu, '&Options')
    monkeypatch.setattr('carr_library.ui.operation_progress.ProgressActions',
        lambda value, **kwargs: make_actions(value))
    actions = install_progress_actions(frame)
    try:
        assert frame.progress_actions is actions
        assert frame.layout.items == [frame.status, actions.panel.window]
        assert frame.menu_bar.GetMenuCount() == 2 and len(old_menu.items) == 3
        assert all('\t' not in item.label for item in old_menu.items)
        query_binding = next(args for args in frame.bindings if args[2] is old_menu.items[0])
        query_binding[1]()
        assert actions.panel.text.focus == 1
    finally:
        actions.panel.dispose()
