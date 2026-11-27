"""Installed native Studio navigation over an actual rendered master.

The real Studio saved-job entry, borrowed navigation host, wx timers and native
keyboard are exercised. FFmpeg creates the silence master under current policy.
The explicit clocked transport emits no sound and does not qualify Windows MCI,
an output device, a voice, live-reader chunk navigation or NVDA.
"""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from types import SimpleNamespace
import traceback
import wave


def _acceptance_bindings():
    import importlib.util
    path = Path(__file__).with_name('windows_publishing_smoke.py')
    spec = importlib.util.spec_from_file_location('_studio_navigation_bindings', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows is required.',
                          'native_ui': 'NOT_RUN', 'mci': 'NOT_RUN', 'audio_device': 'NOT_RUN',
                          'nvda': 'NOT_RUN'}))
        return 77
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audio-evidence', type=Path, required=True)
    arguments = parser.parse_args()
    from carr_library.native_runtime import activate_bundled_runtime
    native_handle = activate_bundled_runtime()
    try:
        return _run_native(arguments)
    finally:
        if native_handle is not None:
            native_handle.close()


def _run_native(arguments):
    import ctypes
    from ctypes import wintypes
    import wx
    import carr_library
    from carr_library.application import LibraryApp
    from carr_library.speech import VoiceRegistry
    from carr_library.audio import AudioBudget
    from carr_library.durable_state import read_json
    from carr_library.studio.render import ProductionService
    import carr_library.runtime_settings as runtime_settings
    import carr_library.speech.runtime as runtime_module
    import carr_library.ui.studio_dialog as studio_module
    import carr_library.studio.render as render_module
    import carr_library.speech.player as player_module
    import carr_library.ui.playback_navigation as navigation_module

    checks, errors, observed = [], [], {}
    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)

    origin = Path(carr_library.__file__).resolve()
    binding = _acceptance_bindings()
    identities = binding.verify_installed_sources(runtime_module, studio_module,
        render_module, player_module, navigation_module)
    evidence = read_json(arguments.audio_evidence)
    selected = SimpleNamespace(ffmpeg=evidence['ffmpeg']['path'], ffprobe=evidence['ffprobe']['path'])
    audio_proof = binding.verify_audio_evidence(selected, arguments.audio_evidence)
    def verify_runtime(runtime):
        check('external test audio retains exact prepared executable identity',
              binding.verify_audio_evidence(runtime, arguments.audio_evidence) == audio_proof)

    class ProbeApp(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc()); fail(); return False
    gui = ProbeApp(False)
    simulator, held = wx.UIActionSimulator(), [None]
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    def release():
        key, held[0] = held[0], None
        if key is not None:
            check('native key and modifiers released', simulator.KeyUp(*key))

    def fail():
        try:
            release()
        except Exception:
            errors.append(traceback.format_exc())
        for window in tuple(wx.GetTopLevelWindows()):
            try:
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            except RuntimeError:
                pass
        gui.ExitMainLoop()

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
    def studio_button(label):
        return next(child for child in children(observed['studio'])
                    if isinstance(child, wx.Button) and child.GetLabel() == label)
    def top(prefix):
        return next((window for window in wx.GetTopLevelWindows()
                     if isinstance(window, wx.Dialog) and window.GetTitle().startswith(prefix)), None)

    class EmptyVoices:
        name = 'Explicit empty voice inventory for navigation acceptance'
        def voices(self): return ()
        def close(self): observed['provider_closed'] = True; return True

    class ClockedTransport:
        """Explicit test-only player protocol, never an audio-device fallback."""
        position_is_approximate = True
        backend = 'Synthetic clock fixture (no audio output)'
        def __init__(self):
            self.path, self.duration_seconds, self._state = None, 0.0, 'stopped'
            self._offset, self._started, self.closed = 0.0, None, False
            self.seeks = []; observed['player'] = self
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
        root = Path(temporary)
        library = LibraryApp(root / 'library')
        library.create_workspace('Synthetic Studio navigation acceptance')
        settings = runtime_settings.load_settings(library.root / 'state')
        settings.update(ffmpeg=audio_proof['ffmpeg']['path'], ffprobe=audio_proof['ffprobe']['path'])
        runtime_settings.save_settings(library.root / 'state', settings)
        runtimes = []
        def runtime(directory, **options):
            options.setdefault('budget', AudioBudget(timeout=20, max_output_bytes=32 * 1024**2))
            value = runtime_settings.audio_runtime(directory, state_root=library.root / 'state', **options)
            verify_runtime(value); runtimes.append(value)
            return value
        registry = VoiceRegistry(())
        service = ProductionService(library.root / 'state' / 'studio', voices=registry, runtime_factory=runtime)
        output = root / 'rendered'; output.mkdir()
        try:
            project = service.create('[Pause: 8]', title='Native master navigation fixture')
            saved = service.run(service.submit(project, output, stems=False))
            check('actual current Studio pipeline publishes its completed master', saved['status'] == 'complete')
        finally:
            service.close(); registry.close()
        master = Path(saved['destination']) / 'master.wav'
        original_master = sha256(master.read_bytes()).hexdigest()
        frame = wx.Frame(None, title='Resonance native Studio navigation acceptance', size=(600, 350))
        frame.library, frame.report = library, lambda message: observed.setdefault('reports', []).append(str(message))
        original_registry, original_player = runtime_settings.voice_registry, player_module.WavePlayer
        original_host = navigation_module.PlaybackNavigationHost
        def host(*args, **kwargs):
            result = original_host(*args, **kwargs); observed['host'] = result; return result
        runtime_settings.voice_registry = lambda *args, **kwargs: VoiceRegistry((EmptyVoices(),))
        player_module.WavePlayer, navigation_module.PlaybackNavigationHost = ClockedTransport, host

        def loaded():
            current = top('Audio Studio')
            if current is None: return False
            observed['studio'] = current
            return named(current, 'Script — prose or named roles; Help explains directions').IsEnabled()
        def open_panel(complete):
            press(studio_button('Playback navigation...'),
                  lambda: until(lambda: observed['host'].view is not None, complete))
        def close_panel(complete):
            key(observed['host'].view.skip, wx.WXK_ESCAPE,
                lambda: until(lambda: observed['host'].view is None, complete))

        def initial():
            check('Studio never starts playback automatically', 'player' not in observed
                  and not studio_button('Playback navigation...').IsEnabled())
            press(studio_button('Saved &jobs'), lambda: until(lambda: top('Production jobs') is not None, choose_job))
        def choose_job():
            chooser = top('Production jobs')
            chooser.SetSelection(0)
            press(wx.FindWindowById(wx.ID_OK, chooser),
                  lambda: until(lambda: studio_button('Listen to &output').IsEnabled(), start_first))
        def start_first():
            press(studio_button('Listen to &output'),
                  lambda: until(lambda: observed.get('host') is not None
                                and observed['host'].controller.snapshot()['active'], first_playing))
        def first_playing():
            player = observed['player']
            observed['first_generation'] = observed['host'].controller.audio.generation
            observed['first_path'] = player.path
            check('explicit Listen binds the exact retained rendered master', player.path == master
                  and player.state == 'playing' and sha256(player.path.read_bytes()).hexdigest() == original_master)
            press(studio_button('Pause/resume playback'), lambda: until(
                lambda: player.state == 'paused', lambda: open_panel(configure_skip)))
        def configure_skip():
            view = observed['host'].view
            check('native named navigation controls are present', all(control.GetName() for control in
                (view.skip, view.percent, view.repeat, view.minutes, view.position, view.status)))
            check('estimated transport precision is disclosed', 'estimated position' in view.position.GetLabel())
            observed['before_skip'] = observed['player'].position_seconds
            view.skip.ChangeValue('1'); press(view.buttons['forward'], skipped)
        def skipped():
            player, view = observed['player'], observed['host'].view
            check('native configurable skip preserves pause', player.state == 'paused'
                  and abs(player.seeks[-1] - observed['before_skip'] - 1) < .001)
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
            check('marks alone never enable replay', controller.a is not None and controller.b > controller.a
                  and not controller.repeat)
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
            press(studio_button('Pause/resume playback'),
                lambda: until(lambda: len(observed['player'].seeks) > observed['paused_seeks'], repeated))
        def repeated():
            controller, player = observed['host'].controller, observed['player']
            check('native timer repeats the explicit range on the same master', player.state == 'playing'
                  and controller.repeat and abs(player.seeks[-1] - controller.a) < .001 and player.path == master)
            open_panel(configure_sleep)
        def configure_sleep():
            view = observed['host'].view
            view.minutes.ChangeValue('0.02'); press(view.buttons['sleep'], sleep_enabled)
        def sleep_enabled():
            check('native sleep action arms the bounded timer', observed['host'].controller.sleep_deadline is not None)
            close_panel(lambda: until(lambda: observed['player'].state == 'stopped', slept, timeout=5))
        def slept():
            controller = observed['host'].controller
            check('sleep survives closing controls and clears repeat through Studio Stop',
                  controller.audio is None and not controller.repeat and controller.sleep_deadline is None)
            check('navigation preserves the exact committed master', sha256(master.read_bytes()).hexdigest() == original_master)
            press(studio_button('Listen to &output'), lambda: until(lambda: controller.snapshot()['active'], second_playing))
        def second_playing():
            controller = observed['host'].controller
            check('explicit replay has a new generation without stale automation',
                  controller.audio.generation != observed['first_generation']
                  and controller.a is None and controller.sleep_deadline is None and not controller.repeat)
            open_panel(long_sleep)
        def long_sleep():
            view = observed['host'].view
            view.minutes.ChangeValue('1'); press(view.buttons['sleep'], lambda: close_panel(explicit_stop))
        def explicit_stop():
            press(studio_button('Stop playback'), stopped)
        def stopped():
            controller = observed['host'].controller
            check('native Stop revokes timer and current binding', observed['player'].state == 'stopped'
                  and controller.audio is None and controller.sleep_deadline is None)
            observed['workflow_complete'] = True
            press(studio_button('&Close'), lambda: None)
        def native_owner_lifetime():
            for mode in ('direct', 'native-parent'):
                parent = wx.Frame(frame, title='Owned Studio navigation teardown fixture')
                owner = wx.Frame(parent, title='Owned navigation timer')
                player = ClockedTransport()
                player.play(master)
                retained = original_host(owner, player, generation=lambda: 1,
                    alive=lambda: bool(owner), stop_current=player.stop, wx_module=wx)
                retained.bind()
                view = navigation_module.create_navigation_dialog(owner, retained.controller, wx_module=wx)
                retained.view = view
                view.Show()
                pending, delivered, events = [], [], []
                def defer(action, *, alive, on_error):
                    pending.append((action, alive, on_error))
                retained.controller.dispatch = defer
                retained.controller.seek_percent(50)
                native_id = owner.GetId()
                def destroyed(event):
                    events.append(event.GetId()); event.Skip()
                owner.Bind(wx.EVT_WINDOW_DESTROY, destroyed)
                def delivery():
                    action, alive, on_error = pending[0]
                    try:
                        if alive(): action()
                    except Exception as exc:
                        on_error(exc)
                    view.refresh(); view.perform(lambda: player.seek(1))
                    delivered.append(True)
                try:
                    if mode == 'direct': owner.Destroy()
                    else: parent.DestroyChildren()
                    deadline = monotonic() + 5
                    while owner or view or events.count(native_id) != 1:
                        if monotonic() >= deadline:
                            raise TimeoutError('Native navigation owner destruction did not finish.')
                        gui.Yield(); sleep(.005)
                    wx.CallAfter(delivery)
                    deadline = monotonic() + 5
                    while not delivered:
                        if monotonic() >= deadline:
                            raise TimeoutError('The held navigation action was not delivered after native deletion.')
                        gui.Yield(); sleep(.005)
                    check(mode + ': actual owner destruction is delivered once', events.count(native_id) == 1)
                    check(mode + ': timer and deferred seek are revoked before dead-peer delivery',
                        retained.closed and retained.controller.closed and not retained.timer.IsRunning()
                        and not view.alive and delivered == [True] and player.seeks == [])
                finally:
                    player.close()
                    if parent: parent.Destroy()
            observed['native_lifetime'] = True
        def finalized():
            check('Studio closes its timer, controller, owned registry and borrowed transport',
                  observed['host'].closed and not observed['host'].timer.IsRunning()
                  and observed['host'].controller.closed and observed['player'].closed
                  and observed.get('provider_closed') is True)
            check('closing Studio preserves durable rendered output', master.is_file()
                  and sha256(master.read_bytes()).hexdigest() == original_master)
            native_owner_lifetime()
            for value in runtimes: verify_runtime(value)
            frame.Destroy(); gui.ExitMainLoop()
        def launch():
            frame.Show(); until(loaded, initial)
            studio_module.show_studio(frame)
            release()
            if not errors:
                check('complete native Studio navigation workflow returned', observed.get('workflow_complete') is True)
                until(lambda: observed.get('provider_closed') is True, finalized)
        def timeout(): raise TimeoutError('Native Studio navigation exceeded its 90-second overall budget.')
        watchdog = later(timeout, 90000)
        try:
            later(launch); gui.MainLoop()
        finally:
            try:
                release()
            finally:
                try:
                    watchdog.Stop()
                finally:
                    runtime_settings.voice_registry, player_module.WavePlayer = original_registry, original_player
                    navigation_module.PlaybackNavigationHost = original_host
                    for window in tuple(wx.GetTopLevelWindows()):
                        try: window.Destroy()
                        except RuntimeError: pass
        if errors: raise RuntimeError('\n'.join(errors))
        check('normal native MainLoop completed', observed.get('workflow_complete') is True)
        print(json.dumps({'result': 'PASS', 'checks': len(checks), 'package_origin': str(origin),
            'source_identity': identities, 'audio_runtime': audio_proof, 'master_sha256': original_master,
            'actual_ffmpeg_master': 'PASS', 'native_ui': 'PASS', 'native_keyboard_automation': 'PASS',
            'native_direct_parent_timer_and_late_callbacks': 'PASS' if observed.get('native_lifetime') else 'NOT_RUN',
            'transport': 'explicit synthetic PCM clock fixture; no audio output',
            'mci': 'NOT_RUN', 'audio_device': 'NOT_RUN', 'voice_quality': 'NOT_RUN',
            'live_reader_chunk_navigation': 'NOT_RUN', 'manual_keyboard': 'NOT_RUN', 'nvda': 'NOT_RUN'}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc(); raise SystemExit(1)

