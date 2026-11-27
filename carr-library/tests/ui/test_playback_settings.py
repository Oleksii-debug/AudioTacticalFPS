"""Bound native playback-control closures with explicit synthetic widgets.

Actual PCM preparation/ownership is also exercised through the current
NavigationHost. No physical sound device or accessibility qualification is claimed.
"""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import wave

import pytest

from carr_library.speech.listening import ListeningSettings
from carr_library.speech._wave_output import PlaybackLevels
from carr_library.speech import player as player_module
from carr_library.ui.playback_settings import create_playback_settings_dialog, show_playback_settings
from carr_library.ui.playback_navigation import PlaybackNavigationHost

_spec = importlib.util.spec_from_file_location(
    '_carr_playback_settings_widgets', Path(__file__).with_name('test_playback_navigation.py'))
_widgets = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _widgets
_spec.loader.exec_module(_widgets)
Widget = _widgets.Widget


class Control(Widget):
    def SetFocus(self): self.native()
    def SetScrollRate(self, *args): self.native()
    def FitInside(self): self.native()


@pytest.fixture
def wx():
    return SimpleNamespace(Dialog=Control, ScrolledWindow=Control, StaticText=Control,
        TextCtrl=Control, Button=Control, CheckBox=Control,
        Timer=_widgets.Timer, BoxSizer=_widgets.Sizer,
        CallLater=lambda delay, callback: callback(),
        DEFAULT_DIALOG_STYLE=1, RESIZE_BORDER=2, VERTICAL=1, HORIZONTAL=2, ALL=4, EXPAND=8,
        ALIGN_RIGHT=16, TE_READONLY=32, TE_MULTILINE=64, VSCROLL=128, TAB_TRAVERSAL=256,
        ID_CANCEL=99, EVT_BUTTON='button', EVT_TIMER='timer',
        EVT_WINDOW_DESTROY='destroy', EVT_CLOSE='close')


def make(wx, *, prepare=None, mix=True, **options):
    owner, calls = Control(), []
    def immediate(settings, completed, failed, *, active):
        calls.append(('prepare', settings))
        completed()
    view = create_playback_settings_dialog(owner, settings=ListeningSettings(), levels=PlaybackLevels(),
        prepare=prepare or immediate, apply_levels=lambda value: calls.append(('levels', value)),
        mix_available=mix, play=lambda: calls.append(('play',)),
        stop=lambda: calls.append(('stop',)), wx_module=wx, **options)
    return owner, view, calls


def test_explicit_independent_settings_and_reset_never_start_or_change_synthesis(wx):
    owner, view, calls = make(wx)
    assert calls == [] and not view.pending
    assert all(field.GetName() for field in view.fields.values())
    view.fields['speed'].ChangeValue('1.5')
    view.fields['pitch'].ChangeValue('-7')
    view.prepare()
    assert calls == [('prepare', ListeningSettings(1.5, -7))]
    view.reset()
    assert view.fields['speed'].GetValue() == '1' and view.fields['pitch'].GetValue() == '0'
    assert len(calls) == 1
    view.prepare()
    assert calls[-1] == ('prepare', ListeningSettings())
    view.play()
    assert calls[-1] == ('play',)
    view.Destroy()


@pytest.mark.parametrize('field,value', [('speed', 'nan'), ('speed', '0'), ('pitch', '13')])
def test_invalid_values_never_reach_existing_worker_or_player(wx, field, value):
    owner, view, calls = make(wx)
    view.fields[field].ChangeValue(value)
    view.prepare()
    assert calls == [] and not view.pending
    assert view.status.GetValue() and owner.forbidden == []
    view.Destroy()


def test_owned_levels_capture_exact_values_without_starting_audio(wx):
    owner, view, calls = make(wx)
    view.fields['volume'].ChangeValue('34')
    view.fields['balance'].ChangeValue('-50')
    view.muted.SetValue(True)
    view.levels()
    assert calls == [('levels', PlaybackLevels(34, True, -.5))]
    view.muted.SetValue(False)
    view.levels()
    assert calls[-1] == ('levels', PlaybackLevels(34, False, -.5))
    assert not any(row[0] == 'play' for row in calls)
    view.Destroy()


def test_unsupported_backend_disables_only_native_levels_not_listening_controls(wx):
    owner, view, calls = make(wx, mix=False)
    assert not view.buttons['levels'].enabled and not view.muted.enabled
    assert view.buttons['prepare'].enabled
    view.levels()
    assert calls == []
    view.prepare()
    assert calls == [('prepare', ListeningSettings())]
    view.Destroy()


@pytest.mark.parametrize('direct', [False, True])
@pytest.mark.parametrize('completed_before_death', [False, True])
def test_real_worker_completion_and_queued_actions_stay_inert_after_native_deletion(
        wx, direct, completed_before_death):
    release, cancelled = threading.Event(), threading.Event()
    pending, callbacks = [], []
    executor = ThreadPoolExecutor(max_workers=1)
    def prepare(settings, completed, failed, *, active):
        def work():
            assert release.wait(5)
            return settings
        future = executor.submit(work)
        future.add_done_callback(lambda result: callbacks.append(
            lambda: completed('Real fixture worker has completed.')))
        pending.append(future)
        return cancelled.set
    owner, view, calls = make(wx, prepare=prepare)
    try:
        view.prepare()
        assert view.pending and not view.buttons['prepare'].enabled
        if completed_before_death:
            release.set()
            pending[0].result(5)
            # Future callbacks run immediately after its result becomes visible.
            executor.shutdown(wait=True)
        cached = view._native_id
        child = view.fields['speed']
        view._destroyed(SimpleNamespace(GetId=lambda: child.native_id, Skip=lambda: None))
        assert view.alive
        if direct:
            view.Destroy()
        else:
            Widget.Destroy(owner)
        assert not view and not view.alive and cancelled.is_set()
        assert view._native_id == cached
        release.set()
        pending[0].result(5)
        executor.shutdown(wait=True)
        assert len(callbacks) == 1
        callbacks[0]()
        for action in (view.prepare, view.levels, view.reset, view.play, view.stop, view.cancel):
            action()
        assert view.Destroy() is False
        assert calls == [] and owner.forbidden == []
    finally:
        release.set()
        executor.shutdown(wait=True)
        if view:
            view.Destroy()


@pytest.mark.parametrize('kind', ['false', 'raise', 'interrupt'])
def test_admission_failure_does_not_leave_phantom_preparation(wx, kind):
    def rejected(settings, completed, failed, *, active):
        if kind == 'raise': raise RuntimeError('Existing worker rejected admission.')
        if kind == 'interrupt': raise KeyboardInterrupt('Existing worker interrupted admission.')
        return False
    owner, view, calls = make(wx, prepare=rejected)
    view.prepare()
    assert not view.pending and view.buttons['prepare'].enabled and view._cancel_preparation is None
    assert calls == [] and owner.forbidden == []
    view.Destroy()


def test_nested_modal_return_after_parent_deletion_has_no_double_native_destroy(wx, monkeypatch):
    owner = Control()
    captured = []
    def modal(view):
        captured.append(view)
        Widget.Destroy(owner)
        return 0
    monkeypatch.setattr(Control, 'ShowModal', modal)
    show_playback_settings(owner, settings=ListeningSettings(), levels=PlaybackLevels(),
        prepare=lambda *a, **k: None, apply_levels=lambda *a: pytest.fail('Unexpected level change.'),
        mix_available=True, wx_module=wx)
    assert len(captured) == 1 and not captured[0] and not captured[0].alive
    assert owner.forbidden == []


@pytest.mark.parametrize('late', [False, True])
def test_navigation_preparation_adopts_only_current_owned_pcm_and_never_autoplays(tmp_path, wx, monkeypatch, late):
    source = tmp_path / 'original.wav'
    with wave.open(str(source), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\x01\x00' * 800)
    original = source.read_bytes()
    private = tmp_path / 'listening'
    private.mkdir()
    player = player_module.WavePlayer()
    monkeypatch.setattr(player_module, 'os', SimpleNamespace(name='nt'))
    commands = []
    player._mci = lambda command: commands.append(command) or ''
    player.open(source)
    owner = Control()
    generation, pending, result = [0], [], []
    executor = ThreadPoolExecutor(max_workers=1)
    def stop():
        generation[0] += 1
        player.stop()
    def prepare(request, completed, failed, *, cancelled):
        future = executor.submit(lambda: player.prepare_listening(request, private,
            cancelled=cancelled, runtime_factory=lambda *a, **k: pytest.fail('Default reset called an engine.')))
        pending.append((future, completed))
        return True
    host = PlaybackNavigationHost(owner, player, generation=lambda: generation[0],
        alive=lambda: bool(owner), stop_current=stop, prepare=prepare, wx_module=wx)
    try:
        cancel = host._prepare_settings(ListeningSettings(), result.append,
            lambda error: result.append(error), active=lambda: bool(owner))
        value = pending[0][0].result(10)
        assert value.path.exists() and not any(command.startswith('play ') for command in commands)
        if late:
            Widget.Destroy(owner)
        pending[0][1](value)
        if late:
            assert value.closed and not value.path.exists() and not result
        else:
            assert value._owner is player and not value.closed
            assert len(result) == 1 and player.state == 'stopped'
            assert not any(command.startswith('play ') for command in commands)
        assert source.read_bytes() == original and owner.forbidden == []
    finally:
        executor.shutdown(wait=True)
        player.close()
        host.close()
    assert not list(private.iterdir())
