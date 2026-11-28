"""Actual LiveReader handlers with disclosed native widgets and PCM output.

No physical output, native key focus, timing quality or NVDA claim is made here.
The same source/core/executor remains responsible for every audible operation.
"""
from pathlib import Path
import runpy
from types import SimpleNamespace as N

import pytest

from carr_library.reader.live_reading import SourceSequence
from carr_library.ui import live_range_controls as ui
from carr_library.ui.live_reading import LiveReadingControls, native_cursor_position

_root = Path(__file__).resolve().parents[1]
_live = runpy.run_path(str(_root / 'reader' / 'test_live_reading.py'))
_controls = runpy.run_path(str(_root / 'ui' / 'test_live_reading_controls.py'))
_widgets = runpy.run_path(str(_root / 'speech' / 'test_pronunciation_handlers.py'))
source, make_reader = _live['source'], _live['make_reader']
SyntheticPlayer, VOICE, until = _live['SyntheticPlayer'], _live['VOICE'], _live['until']
BaseWidget, Sizer = _widgets['Widget'], _widgets['Sizer']


class Widget(BaseWidget):
    def SetEscapeId(self, value):
        self.native()
        self.escape_id = value

    def _delete_native(self):
        self.native()
        for child in tuple(self.children):
            Widget._delete_native(child)
        for callback in tuple(self.handlers.values()):
            if getattr(callback, '__name__', '') in ('on_destroy', 'parent_destroyed'):
                callback(N(GetId=lambda: id(self), Skip=lambda: None))
        if self.parent is not None:
            self.parent.children.remove(self)
        self.destroyed = True

    def Destroy(self):
        if self.destroyed:
            return False
        Widget._delete_native(self)
        return True


def native():
    names = ('DEFAULT_DIALOG_STYLE', 'RESIZE_BORDER', 'TAB_TRAVERSAL', 'VSCROLL',
        'VERTICAL', 'HORIZONTAL', 'ALL', 'EXPAND', 'TE_READONLY', 'TE_MULTILINE',
        'ALIGN_RIGHT', 'EVT_BUTTON', 'EVT_CLOSE', 'EVT_WINDOW_DESTROY', 'ID_CANCEL')
    return N(**{name: index + 1 for index, name in enumerate(names)},
        NOT_FOUND=-1, Dialog=Widget, ScrolledWindow=Widget, StaticText=Widget,
        TextCtrl=Widget, Button=Widget, BoxSizer=Sizer, WrapSizer=Sizer)


@pytest.fixture
def opened(source, make_reader, monkeypatch):
    app, workspace, book, desk, pages = source
    def authorized():
        # The existing source reader's guard is a fresh current workspace read.
        with app.read(workspace):
            pass
    core, renderer, player = make_reader(SourceSequence(desk, authorized),
        player=SyntheticPlayer(automatic=False))
    owner = _controls['controls']()
    owner.core, owner.source = core, core.source
    owner._media_indicator.core = core
    owner.dialog, owner.wx = Widget(), native()
    owner._range_listeners = set()
    owner._voices = (VOICE,)
    owner.voice.SetSelection(0)
    owner.current_calls = 0
    state = N(ordinal=1, raw=pages[0], on_current=None)
    def current():
        owner.current_calls += 1
        if state.on_current is not None:
            state.on_current()
        return state.ordinal, state.raw
    owner.current = current
    owner.text = _controls['NativeText'](state.raw)
    first, last = state.raw.index('🚂'), len(state.raw) - 4
    owner.text.selection = (native_cursor_position(owner.text, state.raw, first),
                            native_cursor_position(owner.text, state.raw, last))
    views, captured = [], []
    def show(parent, **options):
        # Only the outer native modal loop is controlled. Construct and invoke
        # the actual new view and its handlers against this real current core.
        captured.append(options)
        views.append(ui.create_live_range_controls(parent, **options))
    monkeypatch.setattr(ui, 'show_live_range_controls', show)
    owner.repeat_and_sleep()
    assert len(views) == 1, owner.status.value
    view = views[0]
    try:
        yield N(app=app, workspace=workspace, book=book, desk=desk, pages=pages,
            core=core, renderer=renderer, player=player, owner=owner, state=state,
            view=view, views=views, captured=captured, first=first, last=last)
    finally:
        owner.close()
        if core._future is not None:
            core._future.result(5)
        for item in views:
            if not item.destroyed:
                item.Destroy()
        if not owner.dialog.destroyed:
            owner.dialog.Destroy()
        assert app.close_search()


def press(view, name):
    button = view.buttons[name]
    assert button.enabled, name
    callback = next(callback for callback in button.handlers.values())
    callback()


def started(opened):
    press(opened.view, 'repeat')
    until(lambda: opened.core.status().state == 'playing')
    opened.owner.tick()
    assert opened.core.navigation_status()['repeat_range'] == (1, opened.first, opened.last)


def test_open_is_passive_and_exact_native_unicode_selection_repeats_across_real_chunks(opened):
    before = opened.desk.page(1)
    assert opened.view.a.value == str(opened.first)
    assert opened.view.b.value == str(opened.last)
    assert not opened.core.busy and opened.player.play_count == 0 and not opened.renderer.requests
    assert opened.view.buttons['repeat'].enabled and not opened.view.buttons['sleep'].enabled
    started(opened)
    while opened.core.navigation_status()['repeat_pass'] < 2:
        count = opened.player.play_count
        opened.player.advance(.2)
        until(lambda: opened.player.play_count != count or
              opened.core.navigation_status()['repeat_pass'] >= 2)
    until(lambda: opened.core.status().state == 'playing')
    opened.owner.tick()
    assert opened.player.play_count >= 3
    assert 'pass 2' in opened.view.status.value
    first_pass = ''.join(request.text for request in opened.renderer.requests[:-1])
    assert first_pass == opened.state.raw[opened.first:opened.last]
    press(opened.view, 'stop')
    opened.core._future.result(5)
    assert opened.core.status().state == 'stopped' and opened.core.quiescent
    assert opened.player.handle is None and all(not path.exists() for path in opened.player.paths)
    assert opened.desk.page(1) == before


def test_current_selected_range_is_read_from_real_unicode_control_only_when_explicit(opened):
    first = opened.state.raw.index('Literal')
    last = first + len('Literal')
    opened.owner.text.selection = (
        native_cursor_position(opened.owner.text, opened.state.raw, first),
        native_cursor_position(opened.owner.text, opened.state.raw, last))
    assert opened.view.a.value == str(opened.first)
    press(opened.view, 'selection')
    assert (opened.view.a.value, opened.view.b.value) == (str(first), str(last))
    assert not opened.renderer.requests and not opened.core.busy


@pytest.mark.parametrize('change', ['text', 'ordinal', 'voice', 'rate', 'volume', 'archived'])
def test_changed_source_or_voice_never_arms_an_old_repeat_view(opened, change):
    if change == 'text':
        opened.state.raw += ' an explicit changed reading copy'
    elif change == 'ordinal':
        opened.state.ordinal = 2
    elif change == 'voice':
        opened.owner.voice.SetSelection(-1)
    elif change == 'rate':
        opened.owner.rate.SetValue(25)
    elif change == 'volume':
        opened.owner.volume.SetValue(50)
    else:
        opened.app.archive_workspace(opened.workspace)
    press(opened.view, 'repeat')
    assert not opened.core.busy and not opened.renderer.requests and not opened.player.paths
    assert 'changed' in opened.view.status.value.lower() or 'explicitly' in opened.view.status.value.lower() or 'archived' in opened.view.status.value.lower()


@pytest.mark.parametrize('boundary', ['current', 'quiet'])
def test_stop_during_the_same_real_start_callback_revokes_its_generation(opened, boundary):
    if boundary == 'current':
        opened.state.on_current = opened.owner.stop
    else:
        actual = opened.owner._quiet_begin
        def interrupted():
            actual()
            opened.owner.stop()
        opened.owner._quiet_begin = interrupted
    press(opened.view, 'repeat')
    assert not opened.core.busy and not opened.renderer.requests and not opened.player.paths
    assert 'cancelled' in opened.view.status.value


@pytest.mark.parametrize('a,b', [('', '1'), ('-1', '1'), ('0', '0'), ('2', '1'),
    ('0', '9999999999999'), ('０', '2'), ('1.0', '2'), ('1', 'not a number')])
def test_invalid_character_boundaries_never_start_the_owned_core(opened, a, b):
    opened.view.a.ChangeValue(a); opened.view.b.ChangeValue(b)
    press(opened.view, 'repeat')
    assert not opened.core.busy and not opened.renderer.requests


@pytest.mark.parametrize('seconds', ['nan', 'inf', '0', '.5', '86401', '9' * 65])
def test_bad_sleep_input_cannot_stop_or_rearm_the_real_playing_chunk(opened, seconds):
    started(opened)
    opened.view.seconds.ChangeValue(seconds)
    press(opened.view, 'sleep')
    assert opened.core.navigation_status()['sleep_remaining'] is None
    assert opened.core.busy and opened.player.play_count == 1


def test_existing_timer_delivery_updates_sleep_and_real_paused_deadline_stops_same_owner(opened):
    started(opened)
    opened.core.toggle_pause()
    until(lambda: opened.core.status().state == 'paused')
    opened.view.seconds.ChangeValue('1')
    press(opened.view, 'sleep')
    opened.owner.tick()
    assert 'Sleep in at most' in opened.view.status.value
    assert opened.view.buttons['clear_sleep'].enabled
    opened.core._future.result(5)
    opened.owner.tick()
    assert opened.core.status().state == 'stopped' and opened.core.quiescent
    assert opened.player.handle is None and opened.player.resume_count == 0
    assert opened.player.play_count == 1 and len(opened.renderer.requests) == 1
    assert opened.core.navigation_status()['repeat_range'] is None


def test_cancel_sleep_keeps_actual_playing_owner_without_restart(opened):
    started(opened)
    opened.view.seconds.ChangeValue('60')
    press(opened.view, 'sleep')
    press(opened.view, 'clear_sleep')
    assert opened.core.navigation_status()['sleep_remaining'] is None
    assert opened.player.play_count == 1 and opened.core.busy


def test_opening_controls_while_reading_never_reprepares_or_replaces_the_source(opened):
    started(opened)
    opened.owner.current = lambda: pytest.fail('Active reading must use the current immutable source section.')
    opened.owner.repeat_and_sleep()
    assert len(opened.views) == 2
    assert not opened.views[-1].buttons['repeat'].enabled
    assert opened.views[-1].buttons['sleep'].enabled
    assert opened.player.play_count == 1 and opened.core.busy


def test_closing_only_the_controls_leaves_owned_reading_active_and_unsubscribes(opened):
    started(opened)
    retained = opened.view.refresh
    opened.view.close()
    assert not opened.view.alive and not opened.owner._range_listeners
    opened.view.Destroy()
    retained()  # Queued pre-close delivery may not touch a deleted native peer.
    opened.owner.tick()
    assert opened.core.busy and opened.player.play_count == 1 and opened.player.handle is not None


@pytest.mark.parametrize('kind', ['direct', 'parent', 'rewrapped', 'foreign-child'])
def test_native_owner_identity_releases_only_its_observer_and_never_touches_dead_controls(opened, kind):
    started(opened)
    retained = opened.view.refresh
    if kind == 'direct':
        opened.view.Destroy()
        assert opened.view.Destroy() is False
    elif kind == 'parent':
        opened.owner.dialog.Destroy()
    elif kind == 'rewrapped':
        opened.view.on_destroy(N(GetId=lambda: opened.view._native_id, Skip=lambda: None,
            GetEventObject=lambda: pytest.fail('Use cached native identity.')))
        Widget._delete_native(opened.view)
    else:
        opened.view.on_destroy(N(GetId=lambda: opened.view._native_id + 99, Skip=lambda: None))
        assert opened.view.alive and opened.view.refresh in opened.owner._range_listeners
        return
    assert not opened.view.alive and not opened.owner._range_listeners
    retained()
    assert opened.core.busy and opened.player.play_count == 1
    # Actual parent owner closure, not the child view, cancels the reader.
    opened.owner.close()
    opened.core._future.result(5)
    assert opened.core.quiescent and opened.player.handle is None


def test_actual_host_close_cancels_repeat_and_sleep_before_any_late_view_event(opened):
    started(opened)
    opened.view.seconds.ChangeValue('60')
    press(opened.view, 'sleep')
    retained = opened.view.refresh
    opened.owner.close()
    opened.core._future.result(5)
    opened.owner.dialog.Destroy()
    retained()
    assert opened.core.quiescent and opened.player.handle is None
    assert not opened.owner._range_listeners and not opened.view.alive
    assert opened.core.navigation_status()['sleep_remaining'] is None
