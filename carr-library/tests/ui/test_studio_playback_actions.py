"""Execute actual Studio preview closures with the existing borrowed transport.

The player is a controlled protocol fixture. Actual ffplay and native Windows
UI are qualified separately; this suite proves ownership and quiet-start order.
"""
from hashlib import sha256
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import wave

import pytest


_shared = runpy.run_path(str(Path(__file__).with_name('test_progress_voice_actions.py')))


@pytest.fixture
def studio(tmp_path):
    path = tmp_path / 'master.wav'
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
        stream.writeframes(b'\0\0' * 80000)

    class Player:
        position_is_approximate = False
        def __init__(self):
            self.path, self.duration_seconds = None, 0
            self.state, self.position_seconds = 'stopped', 0
            self.calls, self.close_result = [], True
            self.output_device = None
        def select_output(self, selected):
            self.calls.append(('select_output', selected)); self.output_device = selected
        def play(self, selected):
            self.calls.append(('play', selected))
            self.path, self.duration_seconds = selected, 10
            self.state, self.position_seconds = 'playing', 0
        def pause(self):
            self.calls.append(('pause',)); self.state = 'paused'; return True
        def resume(self):
            self.calls.append(('resume',)); self.state = 'playing'; return True
        def seek(self, value):
            self.calls.append(('seek', value)); self.position_seconds = value
        def stop(self):
            self.calls.append(('stop',)); self.state, self.position_seconds = 'stopped', 0
        def close(self):
            self.calls.append(('close',))
            if self.close_result:
                self.state = 'stopped'
            return self.close_result

    class Owner:
        def __init__(self):
            self._audio_player, self._playback_navigation = Player(), None
            self._playback_generation, self.handlers, self.destroyed = 0, {}, False
        def GetId(self): return 9201
        def Bind(self, kind, callback, *args): self.handlers[kind] = callback
        def EndModal(self, value): pass
        def __bool__(self): return not self.destroyed

    class Timer:
        def __init__(self, owner): self.running = False
        def Start(self, milliseconds): self.running = True
        def Stop(self): self.running = False

    scheduler = _shared['Scheduler']()
    local, parent = _shared['Progress']('studio'), _shared['Progress']('main')
    dialog, reports = Owner(), []
    frame = SimpleNamespace(report=reports.append)
    values = dict(current_job='complete-fixture', service=SimpleNamespace(
        get=lambda identity: {'destination': str(tmp_path)}, close=Mock(return_value=True)),
        dialog=dialog, progress=local, parent_progress=parent, report=lambda value, focus=False: reports.append(value),
        wx=SimpleNamespace(CallLater=scheduler, CallAfter=lambda action: action(), Timer=Timer,
                           EVT_TIMER='timer', EVT_WINDOW_DESTROY='destroy', ID_CLOSE=3),
        controls=Mock(), alive=True, busy=False, autosave=lambda: None,
        cancel=threading.Event(), timer=Mock(), executor=Mock(), cleanup_started=False, frame=frame)
    handlers = _shared['callbacks']('studio_dialog.py', 'show_studio',
        ['preview', 'pause_preview', 'stop_preview', 'playback_navigation', 'choose_playback_output',
         'cleanup', 'release_service', 'dispose', 'close'], values)
    fixture = SimpleNamespace(handlers=handlers, player=dialog._audio_player, dialog=dialog,
        local=local, parent=parent, scheduler=scheduler, reports=reports, path=path, values=values)
    yield fixture
    handlers.close()


def start(studio):
    studio.local.ready = studio.parent.ready = True
    studio.handlers.preview()
    assert studio.player.state == 'playing'
    return studio.dialog._playback_navigation


def test_preview_waits_for_both_real_quiet_holds_then_binds_exact_master(studio):
    studio.handlers.preview()
    assert studio.player.calls == []
    studio.local.ready = True; studio.scheduler.tick()
    assert studio.player.calls == []
    studio.parent.ready = True; studio.scheduler.tick()
    assert studio.player.calls == [('play', studio.path)]
    host = studio.dialog._playback_navigation
    assert host.controller.audio.path == studio.path
    assert host.controller.audio.generation == studio.dialog._playback_generation
    assert all(hold.closed for owner in (studio.local, studio.parent) for hold in owner.holds)


def test_actual_studio_pause_seek_resume_and_sleep_revoke_owned_preview_generation(studio):
    host = start(studio)
    original = sha256(studio.path.read_bytes()).hexdigest()
    studio.handlers.pause_preview()
    assert studio.player.state == 'paused'
    host.controller.seek_percent(25)
    assert studio.player.position_seconds == 2.5 and studio.player.state == 'paused'
    studio.local.ready = studio.parent.ready = False
    studio.handlers.pause_preview()
    assert studio.player.state == 'paused'
    studio.local.ready = studio.parent.ready = True; studio.scheduler.tick()
    assert studio.player.state == 'playing'
    clock = [100.0]
    host.controller.clock = lambda: clock[0]
    host.controller.set_sleep(1)
    generation = studio.dialog._playback_generation
    clock[0] += 1; host._tick()
    assert studio.player.state == 'stopped' and host.controller.audio is None
    assert studio.dialog._playback_generation > generation
    assert sha256(studio.path.read_bytes()).hexdigest() == original


def test_explicit_stop_cancels_waiting_resume_before_native_transport(studio):
    host = start(studio)
    studio.handlers.pause_preview()
    studio.parent.ready = False
    studio.handlers.pause_preview()
    studio.handlers.stop_preview()
    before = list(studio.player.calls)
    studio.parent.ready = True; studio.scheduler.tick()
    assert studio.player.calls == before and studio.player.state == 'stopped'
    assert host.controller.audio is None and host.controller.sleep_deadline is None


@pytest.mark.parametrize('confirmed', [True, False])
def test_close_invalidates_timer_and_retains_uncertain_media_in_both_existing_observers(studio, confirmed):
    host = start(studio)
    studio.player.close_result = confirmed
    studio.handlers.close()
    assert host.closed and host.controller.closed and not host.timer.running
    assert ('unregister', 'studio', confirmed) in studio.local.events
    assert ('unregister', 'main', confirmed) in studio.parent.events
    assert any('not confirmed' in notice for notice in studio.reports) is (not confirmed)
    assert studio.path.exists()


@pytest.mark.parametrize('confirmed', [True, False])
def test_explicit_output_change_requires_previous_native_close_confirmation(studio, monkeypatch, confirmed):
    from carr_library.audio.output_devices import OutputDevice
    from carr_library.ui import output_devices
    selected = OutputDevice(1, 'reviewed-endpoint', 'Headphones')
    host = start(studio)
    studio.handlers.stop_preview()
    studio.player.close_result = confirmed
    def choose(parent, *, current, guard):
        guard()
        return output_devices.OutputChoice(selected)
    monkeypatch.setattr(output_devices, 'choose_output_device', choose)
    studio.handlers.choose_playback_output()
    assert studio.player.output_device is (selected if confirmed else None)
    assert getattr(studio.dialog, '_output_device', None) is (selected if confirmed else None)
    assert host.controller.audio is None
    if not confirmed:
        assert any('not confirmed' in message for message in studio.reports)
    else:
        assert studio.player.calls[-2:] == [('close',), ('select_output', selected)]


@pytest.mark.parametrize('boundary', ['generation', 'player', 'native-owner', 'playback'])
def test_modal_output_selection_is_bound_to_unchanged_studio_and_player(studio, monkeypatch, boundary):
    from carr_library.audio.output_devices import OutputDevice
    from carr_library.ui import output_devices
    selected = OutputDevice(0, 'reviewed-endpoint', 'Speakers')
    def choose(parent, *, current, guard):
        guard()
        if boundary == 'generation':
            studio.dialog._playback_generation += 1
        elif boundary == 'player':
            studio.dialog._audio_player = SimpleNamespace()
        elif boundary == 'native-owner':
            studio.dialog.destroyed = True
        else:
            studio.player.state = 'playing'
        return output_devices.OutputChoice(selected)
    monkeypatch.setattr(output_devices, 'choose_output_device', choose)
    studio.handlers.choose_playback_output()
    assert studio.player.output_device is None
    assert not hasattr(studio.dialog, '_output_device')
    assert not any(call[0] == 'select_output' for call in studio.player.calls)
    # Restore the fixture's real cleanup owner before its ordinary finalizer.
    studio.dialog._audio_player = studio.player
    studio.dialog.destroyed = False
    studio.player.state = 'stopped'


def test_output_change_revokes_already_queued_quiet_start(studio, monkeypatch):
    from carr_library.audio.output_devices import OutputDevice
    from carr_library.ui import output_devices
    studio.handlers.preview()
    assert not studio.player.calls
    selected = OutputDevice(0, 'explicit-endpoint', 'Speakers')
    monkeypatch.setattr(output_devices, 'choose_output_device',
        lambda *args, **kwargs: output_devices.OutputChoice(selected))
    studio.handlers.choose_playback_output()
    before = list(studio.player.calls)
    studio.local.ready = studio.parent.ready = True
    studio.scheduler.tick()
    assert studio.player.calls == before
    assert not any(call[0] == 'play' for call in studio.player.calls)
