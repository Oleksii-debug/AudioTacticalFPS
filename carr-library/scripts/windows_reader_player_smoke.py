"""Actual installed Windows MCI pause/seek/resume/close acceptance.

Uses a silent synthetic PCM file and the canonical WavePlayer. No alternate
player, speech provider, physical recording input or listening-quality claim.
"""
from hashlib import file_digest, sha256
from concurrent.futures import ThreadPoolExecutor
import threading
import json
import os
import runpy
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import wave


def native_controls(player, check, commands, selected_output, private_root, state_root, runtime_factory):
    """Exercise real wx keyboard controls over this same measured MCI player."""
    import ctypes
    from ctypes import wintypes
    import traceback
    import wx
    from carr_library.ui import reader_playback

    check('installed reader playback controls',
          'site-packages' in [part.casefold() for part in Path(reader_playback.__file__).resolve().parts])
    errors, subscriptions, repetitions = [], [None], []
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='reader-listening-probe')
    cancel, prepared_values = threading.Event(), []
    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False
    gui = App(False)
    gui.SetExitOnFrameDelete(False)
    frame = wx.Frame(None, title='Reader playback acceptance')
    timer = wx.Timer(frame)
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    simulator = wx.UIActionSimulator()

    def pump(predicate, message, timeout=5):
        deadline = time.monotonic() + timeout
        while not predicate():
            if errors:
                raise RuntimeError('\n'.join(errors))
            if time.monotonic() >= deadline:
                raise TimeoutError(message)
            gui.Yield()
            time.sleep(.01)

    def find(dialog, name):
        found = wx.Window.FindWindowByName(name, dialog)
        check('named playback control: ' + name, found is not None)
        return found

    def press(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle(), 'Native playback focus')
        check('native playback Space down', simulator.KeyDown(wx.WXK_SPACE))
        try:
            gui.Yield()
            time.sleep(.02)
        finally:
            check('native playback Space up', simulator.KeyUp(wx.WXK_SPACE))
        gui.Yield()

    def choose(control, index):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control
             and user32.GetForegroundWindow() == top.GetHandle(), 'Native playback output focus')
        for key in [wx.WXK_HOME, *([wx.WXK_DOWN] * index)]:
            check('native playback output key down', simulator.KeyDown(key))
            try:
                gui.Yield()
            finally:
                check('native playback output key up', simulator.KeyUp(key))
            gui.Yield()
        pump(lambda: control.GetSelection() == index, 'Native playback output selection')

    def play_count():
        return sum(command.startswith(('play ', 'resume ')) for command in commands)

    def tick(_event=None):
        try:
            outcome = player.poll()
            if outcome == 'repeat-pending' and player.repeat_if_ready():
                repetitions.append(True)
            if subscriptions[0] is not None:
                subscriptions[0]()
        except Exception:
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            gui.ExitMainLoop()

    def prepare(request, completed, failed, *, active):
        cancel.clear()
        def work():
            value = player.prepare_listening(request, private_root, state_root=state_root,
                                             cancelled=cancel.is_set, runtime_factory=runtime_factory)
            prepared_values.append(value)
            return value
        future = executor.submit(work)
        def finish():
            value = None
            try:
                value = future.result()
                if cancel.is_set() or not active():
                    raise InterruptedError('Native listening preparation cancelled')
                player.apply_listening(value)
                completed()
            except Exception as exc:
                if value is not None and value._owner is None:
                    executor.submit(value.close)
                if active():
                    failed(exc)
        future.add_done_callback(lambda completed_future: wx.CallAfter(finish))
        return cancel.set

    frame.Bind(wx.EVT_TIMER, tick, timer)
    def drive():
        try:
            popup, = [window for window in wx.GetTopLevelWindows()
                      if window.IsShown() and window.GetTitle() == 'Reader playback controls']
            before = play_count()
            output = find(popup, 'Requested playback output; apply explicitly after selection')
            check('opening output controls does not enumerate or change the route',
                  output.GetCount() == 1 and player.output_device is None)
            press(find(popup, 'Reload available playback outputs without playing audio'))
            outputs = player.output_devices()
            check('actual native inventory is displayed without audio',
                  output.GetCount() == len(outputs) + 1 and play_count() == before)
            if selected_output is not None:
                index = next(index for index, item in enumerate(outputs, 1)
                             if item.identity == selected_output.identity)
                choose(output, index)
                check('native output selection alone does not apply or autoplay',
                      player.output_device is None and play_count() == before)
                press(find(popup, 'Stop audio and use selected playback output'))
                check('native explicit output application stops and preserves identity',
                      player.state == 'stopped' and not player._open
                      and player.output_device.identity == selected_output.identity
                      and play_count() == before)
            find(popup, 'Repeat A in seconds').ChangeValue('.2')
            find(popup, 'Repeat B in seconds').ChangeValue('.6')
            before = play_count()
            press(find(popup, 'Apply A–B repeat'))
            check('native stopped repeat configuration is silent',
                  player.state == 'stopped' and play_count() == before and player.ab_loop == (.2, .6))
            find(popup, 'Backward skip in seconds, 0.1 to 600').ChangeValue('.1')
            find(popup, 'Forward skip in seconds, 0.1 to 600').ChangeValue('.15')
            press(find(popup, 'Apply skip intervals'))
            press(find(popup, 'Skip forward'))
            check('native stopped configurable skip is silent',
                  player.state == 'stopped' and play_count() == before
                  and abs(player.position_seconds - .35) <= .02)
            press(find(popup, 'Play current recording'))
            pump(lambda: bool(repetitions), 'Actual native A–B did not repeat')
            if selected_output is not None:
                check('real selected MCI route confirms output before repeated playback',
                      player._opened_output.identity == selected_output.identity
                      and player._mci(f'status {player._alias} output').strip()
                      == str(player._opened_output.index))
            check('native repeat uses reviewed endpoint',
                  any(command.startswith('play ') and command.endswith('from 200 to 600')
                      for command in commands))
            press(find(popup, 'Pause or resume recording'))
            check('native repeat can be paused', player.state == 'paused')
            position, before = player.position_seconds, play_count()
            deadline = time.monotonic() + .15
            pump(lambda: time.monotonic() >= deadline, 'Paused repeat observation')
            check('native repeated segment stays paused',
                  player.state == 'paused' and abs(player.position_seconds - position) <= .02
                  and play_count() == before)
            press(find(popup, 'Skip backward'))
            check('native paused skip remains silent', player.state == 'paused' and play_count() == before)
            find(popup, 'Sleep timer in minutes, greater than zero and at most 10080').ChangeValue('.02')
            press(find(popup, 'Set sleep timer'))
            check('native sleep timer is active', player.sleep_remaining_seconds is not None
                  and player.sleep_remaining_seconds > 0)
            press(find(popup, 'Pause or resume recording'))
            pump(lambda: player.sleep_remaining_seconds is None and player.state == 'stopped',
                 'Actual native sleep timer did not stop playback')
            before = play_count()
            deadline = time.monotonic() + .2
            pump(lambda: time.monotonic() >= deadline, 'Sleep repeat suppression observation')
            check('sleep prevents a later native repeat', play_count() == before and not player.repeat_pending)
            press(find(popup, 'Clear A–B repeat'))
            check('native clear repeat does not autoplay', player.ab_loop is None and play_count() == before)
            press(find(popup, 'Stop current recording'))
            if selected_output is not None:
                before = play_count()
                choose(output, 0)
                check('choosing automatic output still requires explicit application',
                      player.output_device is not None and play_count() == before)
                press(find(popup, 'Stop audio and use selected playback output'))
                check('explicit automatic output clears only session routing without autoplay',
                      player.output_device is None and not player._open and play_count() == before)
            before = play_count()
            speed = find(popup, 'Listening speed, 0.25 to 4; synthesis rate is unchanged')
            pitch = find(popup, 'Listening pitch, minus 12 to 12 semitones; speed is independent')
            speed.ChangeValue('2')
            pitch.ChangeValue('0')
            press(find(popup, 'Stop and prepare listening speed and pitch without autoplay'))
            pump(lambda: player.listening_settings.speed == 2,
                 'Actual private FFmpeg listening conversion did not complete', timeout=30)
            check('native listening Apply does not autoplay or change the original timeline',
                  play_count() == before and player.state == 'stopped' and not player._open
                  and player.duration_seconds == 3 and player.listening_duration_seconds == 1.5
                  and player.listening_path != player.path)
            press(find(popup, 'Play current recording'))
            pump(lambda: player.position_seconds >= .2, 'Actual faster MCI playback did not advance')
            press(find(popup, 'Pause or resume recording'))
            check('transformed native playback pauses on the original timeline', player.state == 'paused')
            player.seek(1.2)
            check('transformed native seek maps original seconds to measured derivative frames',
                  commands[-1] == f'seek {player._alias} to 600 wait'
                  and abs(player.position_seconds - 1.2) <= .02)
            press(find(popup, 'Pause or resume recording'))
            pump(lambda: player.position_seconds >= 1.3, 'Transformed native resume did not advance')
            press(find(popup, 'Stop current recording'))
            before = play_count()
            pitch.ChangeValue('12')
            press(find(popup, 'Stop and prepare listening speed and pitch without autoplay'))
            pump(lambda: player.listening_settings.pitch_semitones == 12,
                 'Actual independent pitch conversion did not complete', timeout=30)
            check('native pitch choice keeps speed and actual output duration independent',
                  player.listening_settings.speed == 2 and player.listening_duration_seconds == 1.5
                  and player.state == 'stopped' and play_count() == before)
            speed.ChangeValue('1')
            pitch.ChangeValue('0')
            press(find(popup, 'Stop and prepare listening speed and pitch without autoplay'))
            pump(lambda: player.listening_settings.speed == 1 and player.listening_settings.pitch_semitones == 0,
                 'Exact original listening reset did not complete', timeout=30)
            check('native listening reset preserves exact original PCM without autoplay',
                  player.listening_path.read_bytes() == player.path.read_bytes()
                  and player.listening_duration_seconds == player.duration_seconds == 3
                  and play_count() == before)
            press(find(popup, 'Close playback controls'))
        except Exception:
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            gui.ExitMainLoop()

    def scenario():
        try:
            wx.CallLater(100, drive)
            reader_playback.show_reader_playback(frame, player,
                execute=lambda action, **options: action(), stop=player.stop,
                pause=lambda: player.resume() if player.state == 'paused' else player.pause(),
                subscribe=lambda callback: subscriptions.__setitem__(0, callback),
                prepare_listening=prepare)
            check('closed controls release their status subscription', subscriptions[0] is None)
        except Exception:
            errors.append(traceback.format_exc())
        finally:
            gui.ExitMainLoop()

    try:
        frame.Show()
        gui.SetTopWindow(frame)
        timer.Start(50)
        wx.CallAfter(scenario)
        gui.MainLoop()
        if errors:
            raise RuntimeError('\n'.join(errors))
    finally:
        cancel.set()
        # This callback is a declared probe fixture, not the host reader bridge.
        # Unused results are released on its same serial worker before shutdown.
        try:
            futures = [executor.submit(value.close) for value in prepared_values if value._owner is None]
            for future in futures:
                future.result(timeout=10)
        finally:
            executor.shutdown(wait=True, cancel_futures=False)
            timer.Stop()
            frame.Destroy()
            gui.Yield()


def audio_diagnostics(stage):
    """Preserve prerequisite evidence without replacing native MCI assertions."""
    try:
        path = Path(__file__).with_name('windows_audio_diagnostics.py').resolve(strict=True)
        value = runpy.run_path(str(path))['snapshot']()
    except Exception as error:
        value = {'schema': 'carr-windows-audio-diagnostics/1',
                 'diagnostic_error_type': type(error).__name__, 'playback_evidence': 'NOT_RUN'}
    print(json.dumps(dict(value, stage=stage), ensure_ascii=True, sort_keys=True), flush=True)


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-output-device', action='store_true',
                        help='Fail if the runner has no identifiable output for actual MCI routing.')
    parser.add_argument('--audio-directory', type=Path,
                        help='Use the existing reviewed external CI audio directory.')
    parser.add_argument('--audio-lock', type=Path,
        default=Path(__file__).resolve().parents[1] / 'packaging' / 'windows-audio.lock.json')
    options = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows MCI is required'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    from carr_library.speech import player as implementation
    from carr_library.speech import playback_devices, listening
    from carr_library.audio import dsp, process
    from carr_library import audio_runtime
    from carr_library.operations import broker
    from carr_library.ui import reader_playback, speech_dialog
    origin = Path(implementation.__file__).resolve()
    if 'site-packages' not in [part.casefold() for part in origin.parts]:
        raise RuntimeError('The player acceptance gate must load the installed package.')
    reference = Path(__file__).resolve().parents[1] / 'src' / 'carr_library'
    modules = (('speech/player.py', implementation), ('speech/playback_devices.py', playback_devices),
        ('speech/listening.py', listening), ('audio/dsp.py', dsp), ('audio/process.py', process),
        ('audio_runtime.py', audio_runtime), ('operations/broker.py', broker),
        ('ui/reader_playback.py', reader_playback), ('ui/speech_dialog.py', speech_dialog))
    def checked_sources():
        source_hashes = {}
        for relative, module in modules:
            installed = Path(module.__file__).resolve(strict=True)
            if (not installed.is_relative_to(Path(sys.prefix).resolve())
                    or 'site-packages' not in [part.casefold() for part in installed.parts]):
                raise RuntimeError('The native player gate imported outside its installed runtime.')
            expected, actual = (reference / relative).read_bytes(), installed.read_bytes()
            if actual != expected:
                raise RuntimeError('Installed native player source differs: ' + relative)
            source_hashes[relative] = sha256(actual).hexdigest()
        return source_hashes
    source_hashes = checked_sources()
    checks = []
    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print('PASS:', name, flush=True)
    def wait_for(predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError('Actual MCI did not reach its measured playback state.')
            time.sleep(.02)
    def expired():
        # Bound a broken modal/native driver as well as the measured state waits.
        try:
            os.write(2, b'Native reader playback acceptance exceeded its 75-second deadline.\n')
        finally:
            os._exit(124)
    watchdog = threading.Timer(75, expired)
    watchdog.daemon = True
    watchdog.start()
    try:
        source_identity = {}
        for module in (implementation, playback_devices, listening, dsp, reader_playback):
            loaded = Path(module.__file__).resolve()
            check('installed reader output module: ' + module.__name__,
                  'site-packages' in [part.casefold() for part in loaded.parts])
            source_identity[module.__name__] = sha256(loaded.read_bytes()).hexdigest()
        audio_diagnostics('before-output-device-admission')
        outputs = playback_devices.output_devices()
        check('actual Windows output inventory stays within its bound',
              len(outputs) <= playback_devices.MAX_DEVICES)
        selected = next((value for value in outputs if value.selectable), None)
        if options.require_output_device and selected is None:
            raise RuntimeError('Actual output-device qualification requires an identifiable Windows output.')
        selected_audio = options.audio_directory.resolve(strict=True) if options.audio_directory else None
        pin = json.loads(options.audio_lock.read_text(encoding='utf-8')) if selected_audio else None
        tools = ({name: str(selected_audio / 'bin' / (name + '.exe')) for name in ('ffmpeg', 'ffprobe')}
                 if selected_audio else audio_runtime.bundled_audio_tools())
        if tools is None:
            raise RuntimeError('This native gate needs the sealed audio tools or an explicit reviewed audio directory.')
        audio_proof = {}
        def verify_audio():
            current = audio_runtime.bundled_audio_tools() if selected_audio is None else tools
            if current != tools:
                raise RuntimeError('The selected listening runtime changed.')
            proof = {}
            for name in ('ffmpeg', 'ffprobe'):
                binary = Path(tools[name]).resolve(strict=True)
                with binary.open('rb') as stream:
                    digest = file_digest(stream, 'sha256').hexdigest()
                if selected_audio is not None:
                    expected = pin['output_files']['bin/' + name + '.exe']
                    check('explicit selected listening tool: ' + name,
                          binary.samefile(selected_audio / 'bin' / (name + '.exe')))
                    check('pinned listening executable bytes: ' + name,
                          binary.stat().st_size == expected['size'] and digest == expected['sha256'])
                else:
                    check('exact sealed listening tool: ' + name,
                          binary == Path(current[name]).resolve(strict=True))
                proof[name] = {'path': str(binary), 'sha256': digest}
            return proof
        audio_proof = verify_audio()
        def runtime_factory(directory, *, budget):
            runtime = dsp.AudioRuntime(directory, ffmpeg=tools['ffmpeg'], ffprobe=tools['ffprobe'],
                                       budget=budget)
            for name in ('ffmpeg', 'ffprobe'):
                if not Path(getattr(runtime, name)).samefile(tools[name]):
                    raise RuntimeError('The listening adapter selected a different executable.')
            if verify_audio() != audio_proof:
                raise RuntimeError('Listening executable bytes changed during preparation.')
            return runtime
        with TemporaryDirectory(prefix='resonance-native-player-') as temporary:
            library_root = Path(temporary) / 'selected-probe-library'
            library_root.mkdir()
            path = Path(temporary) / 'Silent synthetic PCM — Україна.wav'
            with wave.open(str(path), 'wb') as stream:
                stream.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\0\0' * 72000)
            original = path.read_bytes()
            player = implementation.WavePlayer()
            actual_mci, commands = player._mci, []
            def observed(command):
                commands.append(command)
                return actual_mci(command)
            player._mci = observed
            try:
                audio_diagnostics('before-canonical-mci-open')
                try:
                    player.play(path)
                except Exception:
                    audio_diagnostics('after-canonical-mci-open-failure')
                    raise
                check('canonical measured MCI backend', player.backend == 'Windows MCI'
                      and player.position_is_approximate is False)
                wait_for(lambda: player.position_seconds >= .08)
                check('native pause accepted', player.pause() is True)
                before = player.position_seconds
                time.sleep(.15)
                check('native pause position remains stable', player.state == 'paused'
                      and abs(player.position_seconds - before) <= .02)
                first = len(commands)
                player.seek(.7)
                seek_commands = commands[first:]
                check('paused seek issues no native Play or Resume',
                      seek_commands == [f'seek {player._alias} to 700 wait'])
                check('paused seek preserves logical pause and measured target',
                      player.state == 'paused' and abs(player.position_seconds - .7) <= .02)
                position = player.position_seconds
                time.sleep(.15)
                check('seek remains silent until explicit resume',
                      player._mci(f'status {player._alias} mode') == 'stopped'
                      and abs(player.position_seconds - position) <= .02)
                check('explicit resume accepted', player.resume() is True)
                wait_for(lambda: player.position_seconds >= .78)
                player.stop()
                check('native Stop ends playback', player.state == 'stopped')
                native_controls(player, check, commands, selected, Path(temporary),
                                library_root / 'state', runtime_factory)
            finally:
                player.close()
            check('native Close confirms alias closure', player._open is False and player.state == 'stopped')
            renamed = path.with_name('Closed native file.wav')
            path.rename(renamed)
            check('closed PCM is unchanged and releasable', renamed.read_bytes() == original)
            renamed.unlink()
            check('listening runtime and installed source remain exact after native execution',
                  verify_audio() == audio_proof and checked_sources() == source_hashes)
            report = {'result': 'PASS', 'checks': checks, 'package_origin': str(origin),
                'source_identity': source_identity,
                'installed_modules': 'PASS', 'source_hashes': source_hashes,
                'reader_source_admission': 'required separately by the strict real reader bridge tests',
                'native_parent_callback': 'controlled probe callback; actual reader bridge tested separately',
                'audio_runtime_scope': 'explicit reviewed external CI runtime' if selected_audio else 'sealed bundle',
                'actual_windows_mci': 'PASS', 'synthetic_pcm_sha256': sha256(original).hexdigest(),
                'actual_listening_conversion_and_mapping': 'PASS', 'listening_audio_runtime': audio_proof,
                'actual_windows_output_inventory': 'PASS', 'output_count': len(outputs),
                'selected_mci_output': 'PASS' if selected is not None else 'NOT_RUN_NO_IDENTIFIED_OUTPUT',
                'paused_seek_no_play_command': 'PASS', 'native_ab_sleep_skip_controls': 'PASS', 'audible_listening': 'NOT_RUN', 'nvda': 'NOT_RUN'}
        print(json.dumps(report, ensure_ascii=True), flush=True)
        return 0
    finally:
        watchdog.cancel()
        if handle is not None:
            handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
