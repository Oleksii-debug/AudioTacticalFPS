"""Actual installed Windows MCI pause/seek/resume/close acceptance.

Uses a silent synthetic PCM file and the canonical WavePlayer. No alternate
player, speech provider, physical recording input or listening-quality claim.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import wave


def native_controls(player, check, commands):
    """Exercise real wx keyboard controls over this same measured MCI player."""
    import ctypes
    from ctypes import wintypes
    import traceback
    import wx
    from carr_library.ui import reader_playback

    check('installed reader playback controls',
          'site-packages' in [part.casefold() for part in Path(reader_playback.__file__).resolve().parts])
    errors, subscriptions, repetitions = [], [None], []
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

    frame.Bind(wx.EVT_TIMER, tick, timer)
    def drive():
        try:
            popup, = [window for window in wx.GetTopLevelWindows()
                      if window.IsShown() and window.GetTitle() == 'Reader playback controls']
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
                subscribe=lambda callback: subscriptions.__setitem__(0, callback))
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
        timer.Stop()
        frame.Destroy()
        gui.Yield()


def main():
    if os.name != 'nt':
        print(json.dumps({'result': 'NOT_RUN', 'reason': 'Actual Windows MCI is required'}))
        return 77
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    from carr_library.speech import player as implementation
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
                native_controls(player, check, commands)
            finally:
                player.close()
            check('native Close confirms alias closure', player._open is False and player.state == 'stopped')
            renamed = path.with_name('Closed native file.wav')
            path.rename(renamed)
            check('closed PCM is unchanged and releasable', renamed.read_bytes() == original)
            renamed.unlink()
            report = {'result': 'PASS', 'checks': checks, 'package_origin': str(origin),
                'actual_windows_mci': 'PASS', 'synthetic_pcm_sha256': sha256(original).hexdigest(),
                'paused_seek_no_play_command': 'PASS', 'native_ab_sleep_skip_controls': 'PASS', 'audible_listening': 'NOT_RUN', 'nvda': 'NOT_RUN'}
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 0
    finally:
        if handle is not None:
            handle.close()


if __name__ == '__main__':
    raise SystemExit(main())
