"""Actual installed Windows MCI pause/seek/resume/close acceptance.

Uses a silent synthetic PCM file and the canonical WavePlayer. No alternate
player, speech provider, physical recording input or listening-quality claim.
"""
from hashlib import file_digest, sha256
from concurrent.futures import ThreadPoolExecutor
import threading
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import wave


def native_controls(player, check, commands, selected_output, private_root, runtime_factory):
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
            value = player.prepare_listening(request, private_root, cancelled=cancel.is_set,
                                             runtime_factory=runtime_factory)
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
                    value.close()
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
        executor.shutdown(wait=True, cancel_futures=True)
        for value in prepared_values:
            if value._owner is None:
                value.close()
        timer.Stop()
        frame.Destroy()
        gui.Yield()


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--require-output-device', action='store_true',
                        help='Fail if the runner has no identifiable output for actual MCI routing.')
    parser.add_argument('--ffmpeg')
    parser.add_argument('--ffprobe')
    options = parser.parse_args()
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows MCI is required'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    from carr_library.speech import player as implementation
    from carr_library.speech import playback_devices, listening
    from carr_library.audio import dsp
    from carr_library.audio_runtime import bundled_audio_tools, verify_audio_runtime
    from carr_library.ui import reader_playback
    origin = Path(implementation.__file__).resolve()
    if 'site-packages' not in [part.casefold() for part in origin.parts]:
        raise RuntimeError('The player acceptance gate must load the installed package.')
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
    try:
        source_identity = {}
        for module in (implementation, playback_devices, listening, dsp, reader_playback):
            loaded = Path(module.__file__).resolve()
            check('installed reader output module: ' + module.__name__,
                  'site-packages' in [part.casefold() for part in loaded.parts])
            source_identity[module.__name__] = sha256(loaded.read_bytes()).hexdigest()
        outputs = playback_devices.output_devices()
        check('actual Windows output inventory stays within its bound',
              len(outputs) <= playback_devices.MAX_DEVICES)
        selected = next((value for value in outputs if value.selectable), None)
        if options.require_output_device and selected is None:
            raise RuntimeError('Actual output-device qualification requires an identifiable Windows output.')
        tools = bundled_audio_tools()
        if tools is None and os.environ.get('CARR_V3_AUDIO'):
            tools = verify_audio_runtime(Path(os.environ['CARR_V3_AUDIO']))['tools']
        audio_proof = {}
        def runtime_factory(directory, *, budget):
            runtime = dsp.AudioRuntime(directory, ffmpeg=options.ffmpeg or (tools or {}).get('ffmpeg'),
                                       ffprobe=options.ffprobe or (tools or {}).get('ffprobe'), budget=budget)
            for name in ('ffmpeg', 'ffprobe'):
                binary = Path(getattr(runtime, name)).resolve(strict=True)
                if os.environ.get('CARR_PROGRAM_DIR'):
                    check('exact sealed listening tool: ' + name,
                          tools is not None and binary == Path(tools[name]).resolve(strict=True))
                with binary.open('rb') as stream:
                    digest = file_digest(stream, 'sha256').hexdigest()
                audio_proof[name] = {'path': str(binary), 'sha256': digest}
            return runtime
        with TemporaryDirectory(prefix='resonance-native-player-') as temporary:
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
                player.play(path)
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
                native_controls(player, check, commands, selected, Path(temporary), runtime_factory)
            finally:
                player.close()
            check('native Close confirms alias closure', player._open is False and player.state == 'stopped')
            renamed = path.with_name('Closed native file.wav')
            path.rename(renamed)
            check('closed PCM is unchanged and releasable', renamed.read_bytes() == original)
            renamed.unlink()
            report = {'result': 'PASS', 'checks': checks, 'package_origin': str(origin),
                'source_identity': source_identity,
                'actual_windows_mci': 'PASS', 'synthetic_pcm_sha256': sha256(original).hexdigest(),
                'actual_listening_conversion_and_mapping': 'PASS', 'listening_audio_runtime': audio_proof,
                'actual_windows_output_inventory': 'PASS', 'output_count': len(outputs),
                'selected_mci_output': 'PASS' if selected is not None else 'NOT_RUN_NO_IDENTIFIED_OUTPUT',
                'paused_seek_no_play_command': 'PASS', 'native_ab_sleep_skip_controls': 'PASS', 'audible_listening': 'NOT_RUN', 'nvda': 'NOT_RUN'}
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 0
    finally:
        if handle is not None:
            handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
