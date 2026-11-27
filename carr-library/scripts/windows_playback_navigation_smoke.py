"""Installed native W21 UI/protocol probe with an explicit synthetic transport.

The real reader, voice-registry PCM publication, navigation host, wx timers and
native keyboard are exercised. The clocked UI transport emits no sound. A
separate actual WinMM inventory optionally exercises a real selected MCI route
with synthetic silence when the runner has an identifiable output. No-device
runners report routing NOT_RUN. Listening, voice quality and NVDA remain open.
"""
from hashlib import sha256
import faulthandler
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
import traceback
import threading
import wave


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.',
                          'native_ui': 'NOT_RUN', 'mci': 'NOT_RUN', 'audio_device': 'NOT_RUN',
                          'nvda': 'NOT_RUN'}))
        return 77
    # Bound activation and real driver calls as well as the later GUI phase.
    # A blocked WinMM/MCI call must still produce a traceback and terminate.
    native_handle = None
    hard_watchdog = threading.Timer(120, lambda: os._exit(124))
    hard_watchdog.daemon = True
    faulthandler.dump_traceback_later(105, repeat=False)
    try:
        hard_watchdog.start()
        from carr_library.native_runtime import activate_bundled_runtime
        native_handle = activate_bundled_runtime()
        return _run_native()
    finally:
        try:
            if native_handle is not None:
                native_handle.close()
        finally:
            hard_watchdog.cancel()
            faulthandler.cancel_dump_traceback_later()


def _run_native():
    import ctypes
    from ctypes import wintypes
    import wx
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.speech import Voice, VoiceRegistry
    import carr_library.runtime_settings as runtime_settings
    import carr_library.speech.runtime as runtime_module
    import carr_library.ui.speech_dialog as reader_module
    import carr_library.ui.playback_navigation as navigation_module
    import carr_library.speech.playback_devices as device_module
    import carr_library.speech.player as player_module

    checks, errors, observed = [], [], {}
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    origin = Path(carr_library.__file__).resolve()
    bundle = Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR') else None
    check('installed package origin', origin.is_relative_to(bundle) if bundle else 'site-packages' in origin.parts)
    checkout, identities = Path(__file__).resolve().parents[1], {}
    for module in (runtime_module, reader_module, navigation_module, device_module, player_module):
        actual = Path(module.__file__).resolve()
        expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
        digest = sha256(actual.read_bytes()).hexdigest()
        check('installed exact source: ' + module.__name__, not actual.is_relative_to(checkout)
              and digest == sha256(expected.read_bytes()).hexdigest())
        identities[module.__name__] = digest

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc()); fail(); return False
    gui = ProbeApp(False)
    gui.SetAssertMode(wx.APP_ASSERT_EXCEPTION)
    simulator, held = wx.UIActionSimulator(), [None]
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def release():
        key = held[0]
        if key is not None:
            check('native key and modifiers released', simulator.KeyUp(*key))
            held[0] = None

    def fail():
        if observed.get('aborting'):
            return
        observed['aborting'] = True
        try:
            release()
        except Exception:
            errors.append(traceback.format_exc())
        host = observed.get('host')
        view = host.view if host is not None else None
        try:
            if view is not None and view.IsModal() and view.IsShown():
                view.EndModal(wx.ID_CANCEL)
        except Exception:
            errors.append(traceback.format_exc())
        deadline = monotonic() + 5
        def exit_outer():
            loop = observed.get('outer_loop')
            try:
                if loop is not None:
                    loop.ScheduleExit(1)
            except Exception:
                errors.append(traceback.format_exc())
            gui.ExitMainLoop()
        def unwind():
            # EndModal returns before the inner ShowModal/finally unwinds on
            # MSW. The reader is still an inactive parent until view is cleared.
            if host is not None and host.view is not None:
                if monotonic() >= deadline:
                    errors.append('The owned navigation modal did not unwind during failure cleanup.')
                    exit_outer()
                else:
                    observed['abort_timer'] = wx.CallLater(25, unwind)
                return
            reader = observed.get('reader')
            try:
                if reader is not None and reader.IsModal() and reader.IsShown():
                    reader.EndModal(wx.ID_CANCEL)
            except Exception:
                errors.append(traceback.format_exc())
            observed['abort_timer'] = wx.CallLater(25, exit_outer)
        observed['abort_timer'] = wx.CallLater(25, unwind)

    def later(callback, delay=30):
        def invoke():
            if errors:
                return
            try:
                callback()
            except Exception:
                errors.append(traceback.format_exc()); fail()
        return wx.CallLater(delay, invoke)

    def until(predicate, complete, *, timeout=10):
        deadline = monotonic() + timeout
        def poll():
            if predicate(): complete()
            elif monotonic() >= deadline: raise TimeoutError('Native playback navigation stage did not finish.')
            else: later(poll)
        later(poll)

    def foreground(control):
        top, owner = wx.GetTopLevelParent(control), wintypes.DWORD()
        handle = user32.GetForegroundWindow()
        if handle: user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        ready = (owner.value == os.getpid() and int(handle or 0) == int(top.GetHandle())
                 and wx.Window.FindFocus() is control)
        if not ready:
            top.Raise(); user32.SetForegroundWindow(top.GetHandle()); control.SetFocus()
        return ready

    def key(control, code, complete, modifiers=wx.MOD_NONE):
        def down():
            check('native target owns foreground and focus: ' + control.GetName(), foreground(control))
            held[0] = (code, modifiers)
            try:
                check('native key down accepted', simulator.KeyDown(code, modifiers))
            except BaseException:
                release(); raise
            def up():
                release(); later(complete)
            later(up, 40)
        until(lambda: held[0] is None and foreground(control), down, timeout=5)

    def press(control, complete): key(control, wx.WXK_SPACE, complete)
    def children(window):
        for child in window.GetChildren():
            yield child
            yield from children(child)
    def named(window, name): return next(child for child in children(window) if child.GetName() == name)
    def reader_button(label):
        return next(child for child in children(observed['reader'])
                    if isinstance(child, wx.Button) and child.GetLabel() == label)
    def top(prefix):
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog) and window.GetTitle().startswith(prefix)), None)

    class SyntheticPCM:
        name = 'Explicit native-navigation PCM fixture; no speech engine'
        def voices(self): return (Voice('fixture:navigation', 'Synthetic silence PCM fixture', 'fixture', 'en'),)
        def render(self, text, voice, destination, settings, cancelled):
            if cancelled(): raise RuntimeError('Synthetic render cancelled.')
            with wave.open(str(destination), 'wb') as stream:
                stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM'))
                stream.writeframes(b'\0\0' * 240000)
            observed['renders'] = observed.get('renders', 0) + 1
        def close(self): observed['provider_closed'] = True

    class ClockedTransport:
        """Explicit test-only player protocol, never an audio-device fallback."""
        position_is_approximate = True
        backend = 'Synthetic clock fixture (no audio output)'
        def __init__(self):
            self.path, self.duration_seconds, self._state = None, 0.0, 'stopped'
            self._offset, self._started, self.closed = 0.0, None, False
            self.seeks = []; observed['player'] = self
            self.output_device = None
            self.outputs = (device_module.PlaybackDevice(0, 'Explicit fixture output A', 'fixture-interface-a', 1, 1, 1, 2),
                            device_module.PlaybackDevice(1, 'Explicit fixture output B', 'fixture-interface-b', 1, 2, 1, 2))
            self.output_choices = []
        def output_devices(self):
            return self.outputs
        def select_output(self, device):
            check('fixture output changes only after explicit reader Stop', self.state == 'stopped')
            if device is not None and device not in self.outputs:
                raise RuntimeError('Selected fixture output is unavailable; no fallback was used.')
            self.output_device = device
            self.output_choices.append(device)
        @property
        def state(self):
            if self._state == 'playing' and self.position_seconds >= self.duration_seconds:
                self._state, self._offset, self._started = 'stopped', self.duration_seconds, None
            return self._state
        @property
        def position_seconds(self):
            elapsed = monotonic() - self._started if self._state == 'playing' and self._started is not None else 0
            return min(self.duration_seconds, self._offset + elapsed)
        def play(self, path, *, start_seconds=0):
            with wave.open(str(path), 'rb') as stream:
                check('fixture receives actual published nonempty PCM', stream.getcomptype() == 'NONE'
                      and stream.getnframes() > 0)
                self.duration_seconds = stream.getnframes() / stream.getframerate()
            self.path, self._offset, self._started, self._state = Path(path), start_seconds, monotonic(), 'playing'
        def pause(self):
            if self.state != 'playing': return False
            self._offset, self._state = self.position_seconds, 'paused'; return True
        def resume(self):
            if self.state != 'paused': return False
            self._started, self._state = monotonic(), 'playing'; return True
        def seek(self, seconds):
            paused = self.state == 'paused'
            self.seeks.append(seconds); self.play(self.path, start_seconds=seconds)
            if paused: self.pause()
        def stop(self): self._state, self._offset, self._started = 'stopped', 0.0, None
        def close(self): self.stop(); self.closed = True

    with TemporaryDirectory(prefix='resonance-native-playback-navigation-') as temporary:
        # The real OS inventory is separate from the explicitly synthetic UI
        # transport below. No-device runners must not claim hardware routing.
        actual_devices = device_module.output_devices()
        observed['native_inventory_count'] = len(actual_devices)
        observed['actual_mci_route'] = 'NOT_RUN_NO_IDENTIFIED_OUTPUT'
        check('actual Windows output inventory is bounded and enumerated', len(actual_devices) <= device_module.MAX_DEVICES)
        selectable = [device for device in actual_devices if device.selectable]
        if selectable:
            route_pcm = Path(temporary) / 'actual-native-route.wav'
            with wave.open(str(route_pcm), 'wb') as stream:
                stream.setparams((1, 2, 8000, 0, 'NONE', 'PCM')); stream.writeframes(b'\0\0' * 40000)
            real_player = player_module.WavePlayer()
            try:
                real_player.select_output(selectable[0])
                check('real selected output does not start audio', real_player.state == 'stopped')
                real_player.play(route_pcm)
                check('real MCI route accepted the exact selected interface', real_player.output_device.identity == selectable[0].identity
                      and real_player.state == 'playing')
                real_player.stop()
                check('real selected MCI route stops explicitly', real_player.state == 'stopped')
                observed['actual_mci_route'] = 'EXECUTED_WITH_SYNTHETIC_SILENCE'
            finally:
                real_player.close()
        library = LibraryApp(Path(temporary) / 'library')
        frame = wx.Frame(None, title='Resonance native playback navigation UI acceptance', size=(600, 350))
        frame.library, frame.report = library, lambda message: observed.setdefault('reports', []).append(str(message))
        original_registry, original_player = runtime_settings.voice_registry, reader_module.WavePlayer
        original_host, original_message = reader_module.PlaybackNavigationHost, wx.MessageBox
        def host(*args, **kwargs):
            result = original_host(*args, **kwargs); observed['host'] = result; return result
        runtime_settings.voice_registry = lambda *args, **kwargs: VoiceRegistry((SyntheticPCM(),))
        reader_module.WavePlayer, reader_module.PlaybackNavigationHost = ClockedTransport, host
        wx.MessageBox = lambda *args, **kwargs: wx.NO

        def loaded():
            current = top('Voice reader and reading copy')
            if current is None: return False
            observed['reader'] = current
            return reader_button('Read &selection / all (F6)').IsEnabled()
        def open_panel(complete):
            press(named(observed['reader'], 'Playback navigation: skip, percentage, repeat and sleep'),
                  lambda: until(lambda: observed['host'].view is not None, complete))
        def close_panel(complete):
            key(observed['host'].view.skip, wx.WXK_ESCAPE,
                lambda: until(lambda: observed['host'].view is None, complete))

        def initial():
            check('reader does not render or start automatically', observed.get('renders', 0) == 0
                  and observed['player'].state == 'stopped')
            open_panel(initial_panel)
        def initial_panel():
            view = observed['host'].view
            check('native named controls are present', all(control.GetName() for control in
                (view.skip, view.percent, view.repeat, view.minutes, view.position, view.status)))
            check('no active recording leaves transport actions disabled', not view.buttons['seek'].IsEnabled()
                  and not view.buttons['forward'].IsEnabled() and not view.buttons['sleep'].IsEnabled())
            # Native Tab follows ordinary control navigation; it must not render.
            key(view.skip, wx.WXK_TAB, initial_tabbed)
        def initial_tabbed():
            check('native Tab moved focus without playback', wx.Window.FindFocus() is not observed['host'].view.skip
                  and observed.get('renders', 0) == 0)
            view = observed['host'].view
            check('native output controls are explicitly named', view.output.GetName()
                  and view.reload_outputs.GetName() and view.apply_output.GetName())
            press(view.reload_outputs, outputs_loaded)
        def outputs_loaded():
            view = observed['host'].view
            check('reloading fixture outputs never renders or starts audio', len(view.outputs) == 3
                  and observed.get('renders', 0) == 0 and observed['player'].state == 'stopped')
            key(view.output, wx.WXK_HOME,
                lambda: key(view.output, wx.WXK_DOWN, lambda: key(view.output, wx.WXK_DOWN, output_selected)))
        def output_selected():
            view = observed['host'].view
            check('native keys select the second output without applying it', view.output.GetSelection() == 2
                  and observed['player'].output_device is None and observed.get('renders', 0) == 0)
            press(view.apply_output, output_applied)
        def output_applied():
            player = observed['player']
            check('explicit native output action retains the selected fixture without automatic speech',
                  player.output_device == player.outputs[1] and len(player.output_choices) == 1
                  and player.state == 'stopped' and observed.get('renders', 0) == 0)
            close_panel(start_first)
        def start_first():
            key(reader_button('Read &selection / all (F6)'), wx.WXK_F6,
                lambda: until(lambda: observed['host'].controller.snapshot()['active'], first_playing))
        def first_playing():
            player = observed['player']
            observed['first_generation'] = observed['host'].controller.audio.generation
            observed['first_path'], observed['first_hash'] = player.path, sha256(player.path.read_bytes()).hexdigest()
            check('explicit F6 publishes PCM and binds the reader generation', observed['renders'] == 1
                  and player.state == 'playing')
            key(reader_button('&Pause / resume (F7)'), wx.WXK_F7, lambda: until(
                lambda: player.state == 'paused', lambda: open_panel(configure_skip)))
        def configure_skip():
            view = observed['host'].view
            check('estimated transport precision is disclosed', 'estimated position' in view.position.GetLabel())
            observed['before_skip'] = observed['player'].position_seconds
            view.skip.ChangeValue('2'); press(view.buttons['forward'], skipped)
        def skipped():
            player, view = observed['player'], observed['host'].view
            check('native configurable skip preserves pause', player.state == 'paused'
                  and abs(player.seeks[-1] - observed['before_skip'] - 2) < .001)
            view.percent.ChangeValue('25'); press(view.buttons['seek'], marked_a)
        def marked_a():
            player, view = observed['player'], observed['host'].view
            check('native percentage seek reaches reviewed quarter', player.state == 'paused'
                  and abs(player.position_seconds - player.duration_seconds / 4) < .02)
            press(view.buttons['a'], move_b)
        def move_b():
            view = observed['host'].view
            view.percent.ChangeValue('50'); press(view.buttons['seek'], lambda: press(view.buttons['b'], enable_repeat))
        def enable_repeat():
            view, controller = observed['host'].view, observed['host'].controller
            check('marks alone do not enable automatic replay', controller.a is not None
                  and controller.b > controller.a and not controller.repeat)
            press(view.repeat, repeat_enabled)
        def repeat_enabled():
            check('native checkbox explicitly enables repeat', observed['host'].controller.repeat)
            observed['paused_seeks'] = len(observed['player'].seeks)
            later(paused_repeat, 450)
        def paused_repeat():
            check('repeat timer never resumes paused playback', observed['player'].state == 'paused'
                  and len(observed['player'].seeks) == observed['paused_seeks'])
            close_panel(resume_repeat)
        def resume_repeat():
            key(reader_button('&Pause / resume (F7)'), wx.WXK_F7,
                lambda: until(lambda: len(observed['player'].seeks) > observed['paused_seeks'], repeated))
        def repeated():
            controller, player = observed['host'].controller, observed['player']
            check('native timer repeats the explicitly marked range', player.state == 'playing'
                  and controller.repeat and abs(player.seeks[-1] - controller.a) < .001)
            open_panel(configure_sleep)
        def configure_sleep():
            view = observed['host'].view
            view.minutes.ChangeValue('0.02'); press(view.buttons['sleep'], sleep_enabled)
        def sleep_enabled():
            check('native sleep action arms the bounded timer', observed['host'].controller.sleep_deadline is not None)
            close_panel(lambda: until(lambda: observed['player'].state == 'stopped', slept, timeout=5))
        def slept():
            controller = observed['host'].controller
            check('sleep remains active after controls close and clears repeat through reader Stop',
                  controller.audio is None and not controller.repeat and controller.sleep_deadline is None)
            check('navigation preserved exact published PCM bytes',
                  sha256(observed['first_path'].read_bytes()).hexdigest() == observed['first_hash'])
            key(reader_button('Read &selection / all (F6)'), wx.WXK_F6,
                lambda: until(lambda: observed.get('renders') == 2 and controller.snapshot()['active'], second_playing))
        def second_playing():
            controller = observed['host'].controller
            check('new explicit playback has a new generation with no old automation',
                  controller.audio.generation != observed['first_generation']
                  and controller.a is None and controller.sleep_deadline is None and not controller.repeat)
            open_panel(long_sleep)
        def long_sleep():
            view = observed['host'].view
            view.minutes.ChangeValue('1'); press(view.buttons['sleep'], lambda: close_panel(explicit_stop))
        def explicit_stop():
            key(reader_button('S&top (F8)'), wx.WXK_F8, stopped)
        def stopped():
            controller = observed['host'].controller
            check('native F8 revokes the timer and current binding', observed['player'].state == 'stopped'
                  and controller.audio is None and controller.sleep_deadline is None)
            open_panel(missing_output_loaded)
        def missing_output_loaded():
            view = observed['host'].view
            press(view.reload_outputs, remove_selected_output)
        def remove_selected_output():
            view, player = observed['host'].view, observed['player']
            check('reopened controls retain the explicit second fixture selection', view.output.GetSelection() == 2)
            observed['missing_output'] = player.output_device
            player.outputs = player.outputs[:1]
            press(view.apply_output, unavailable_output)
        def unavailable_output():
            view, player = observed['host'].view, observed['player']
            check('missing output remains visible without fallback or automatic rendering',
                  player.output_device == observed['missing_output'] and len(player.output_choices) == 1
                  and 'no fallback' in view.status.GetValue() and player.state == 'stopped'
                  and observed['renders'] == 2)
            press(view.reload_outputs, choose_automatic_output)
        def choose_automatic_output():
            view = observed['host'].view
            check('reload leaves a missing saved output unselected', view.output.GetSelection() == wx.NOT_FOUND)
            key(view.output, wx.WXK_HOME, lambda: press(view.apply_output, automatic_output_applied))
        def automatic_output_applied():
            player = observed['player']
            check('automatic routing requires a distinct explicit native action', player.output_device is None
                  and len(player.output_choices) == 2 and player.state == 'stopped' and observed['renders'] == 2)
            close_panel(finish_reader)
        def finish_reader():
            observed['workflow_complete'] = True
            press(reader_button('C&lose'), lambda: None)
        def launch():
            observed['outer_loop'] = wx.EventLoop.GetActive()
            frame.Show(); until(loaded, initial)
            reader_module.show_speech_reader(frame, text='Synthetic playback navigation fixture',
                recovery_slot='navigation-native')
            if not errors:
                check('complete native playback controls workflow returned', observed.get('workflow_complete') is True)
                check('reader closed its timer, controller, provider and borrowed transport',
                    observed['host'].closed and not observed['host'].timer.IsRunning()
                    and observed['host'].controller.closed and observed['player'].closed
                    and observed.get('provider_closed') is True)
                check('reader removed its generated PCM only after transport cleanup', not observed['first_path'].exists())
            frame.Destroy(); gui.ExitMainLoop()
        def timeout(): raise TimeoutError('Native playback navigation exceeded its 90-second overall budget.')
        watchdog = later(timeout, 90000)
        try:
            later(launch); gui.MainLoop()
        finally:
            try:
                release()
            finally:
                try:
                    watchdog.Stop()
                    abort_timer = observed.get('abort_timer')
                    if abort_timer is not None: abort_timer.Stop()
                finally:
                    runtime_settings.voice_registry, reader_module.WavePlayer = original_registry, original_player
                    reader_module.PlaybackNavigationHost, wx.MessageBox = original_host, original_message
                    for window in tuple(wx.GetTopLevelWindows()):
                        try: window.Destroy()
                        except RuntimeError: pass
        if errors: raise RuntimeError('\n'.join(errors))
        check('normal native MainLoop completed', observed.get('workflow_complete') is True)
        print(json.dumps({'result': 'PASS', 'checks': len(checks), 'package_origin': str(origin),
            'source_identity': identities, 'native_ui': 'PASS', 'native_keyboard_automation': 'PASS',
            'transport': 'explicit synthetic PCM clock fixture; no audio output',
            'output_controls': 'EXPLICIT_SYNTHETIC_INTERFACE_FIXTURES',
            'native_inventory': 'EXECUTED', 'native_inventory_count': observed['native_inventory_count'],
            'selected_mci_route': observed['actual_mci_route'],
            'mci': 'EXECUTED_SELECTED_ROUTE' if selectable else 'NOT_RUN',
            'audio_device': 'SYNTHETIC_SILENCE_ONLY' if selectable else 'NOT_RUN', 'voice_quality': 'NOT_RUN',
            'manual_keyboard': 'NOT_RUN', 'confirmation_dialog': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc(); raise SystemExit(1)
