"""Actual Windows player, private listening and retained-queue acceptance.

Native controls use the existing desktop worker and one canonical WavePlayer.
Only purpose-written silent PCM is used; no inference, microphone, manual
listening or NVDA quality claim is made. Run using python -I -B.
"""
from hashlib import file_digest, sha256
import faulthandler
import importlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import threading
import time
import traceback
import wave

def _installed_package_root(distributions, package_file, checkout):
    """Bind installed acceptance to the imported package's actual wheel record.

    A checkout egg-info entry may precede the wheel in metadata discovery.
    Its project-relative locate_file('carr_library') is not package evidence.
    Source acceptance never consults installed distribution metadata.
    """
    actual = Path(package_file).resolve(strict=True)
    checkout = Path(checkout).resolve(strict=True)
    if actual.is_relative_to(checkout):
        raise RuntimeError('Installed acceptance imported the checkout package.')
    matches = []
    for distribution in distributions:
        for member in distribution.files or ():
            if str(member).replace('\\', '/') != 'carr_library/__init__.py':
                continue
            try:
                declared = Path(distribution.locate_file(member)).resolve(strict=True)
            except FileNotFoundError:
                continue  # An unrelated stale metadata entry is not this package.
            if declared == actual:
                matches.append(declared.parent)
    if len(matches) != 1:
        raise RuntimeError('Exactly one installed wheel record must own the imported package.')
    return matches[0]




def native_controls(player, check, commands, selected_output, private_root, runtime_factory, broker):
    """Exercise real wx keyboard controls over this same measured MCI player."""
    import ctypes
    from ctypes import wintypes
    import traceback
    import wx
    from carr_library.ui import reader_playback

    check('actual native playback controls are imported', callable(reader_playback.show_reader_playback))
    errors, subscriptions, repetitions = [], [None], []
    from carr_library.application import LibraryApp
    from carr_library.ui.desktop_app import DesktopFrame
    cancel, prepared_values = threading.Event(), []
    class App(wx.App):
        def OnExceptionInMainLoop(self):
            errors.append(traceback.format_exc())
            self.ExitMainLoop()
            return False
    gui = App(False)
    gui.SetExitOnFrameDelete(False)
    library = LibraryApp(private_root / 'desktop-library')
    workspace = library.create_workspace('Native playback queue')
    frame = DesktopFrame(library)
    frame.operation_progress.register_player(player)
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

    repeat_waiting = [False]
    from carr_library.speech.progress_speech import play_when_quiet
    def execute(action, *, audible=False):
        if audible:
            play_when_quiet((frame.operation_progress,), action, schedule=wx.CallLater,
                alive=lambda: not frame._closing, on_error=lambda error: errors.append(str(error)))
        else:
            action()

    def tick(_event=None):
        try:
            outcome = player.poll(restart=False)
            if outcome == 'repeat_due' and not repeat_waiting[0]:
                repeat_waiting[0] = True
                def repeat():
                    try:
                        if player.restart_repeat():
                            repetitions.append(True)
                    finally:
                        repeat_waiting[0] = False
                execute(repeat, audible=True)
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
        def stopped():
            return cancel.is_set() or frame.cancel_event.is_set() or not active()
        def work():
            value = player.prepare_listening(request, private_root, cancelled=stopped,
                                             runtime_factory=runtime_factory, broker=broker)
            prepared_values.append(value)
            return value
        def finish(value):
            try:
                if stopped():
                    raise InterruptedError('Native listening preparation cancelled')
                player.apply_listening(value)
                completed()
            except Exception as exc:
                if value._owner is None:
                    value.close()
                failed(exc)
        frame.run_job('Native private listening conversion', work, finish, failed=failed,
                      discard=lambda value: value.close())
        return cancel.set

    def review_outputs(completed, failed, *, active):
        cancel.clear()
        frame.run_job('Native output inventory',
            lambda: player.output_devices(cancelled=lambda: cancel.is_set() or not active()),
            lambda values: completed(values) if active() and not cancel.is_set() else None,
            failed=failed)
        return cancel.set

    frame.Bind(wx.EVT_TIMER, tick, timer)
    def drive():
        try:
            popup, = [window for window in wx.GetTopLevelWindows()
                      if isinstance(window, wx.Dialog) and window.IsModal() and window.IsShown()
                      and window.GetTitle() == 'Reader playback controls']
            before = play_count()
            output = find(popup, 'Requested playback output; apply explicitly after selection')
            check('opening output controls does not enumerate or change the route',
                  output.GetCount() == 1 and player.output_device is None)
            press(find(popup, 'Reload available playback outputs without playing audio'))
            outputs = player.output_devices()
            pump(lambda: not frame.working and output.GetCount() == len(outputs) + 1,
                 'Actual worker output enumeration did not complete')
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
            press(find(popup, 'Set A–B points'))
            check('native stopped repeat configuration is silent',
                  player.state == 'stopped' and play_count() == before and player.ab_loop == (.2, .6))
            find(popup, 'Backward skip in seconds, 0.1 to 600').ChangeValue('.1')
            find(popup, 'Forward skip in seconds, 0.1 to 600').ChangeValue('.15')
            press(find(popup, 'Apply skip intervals'))
            press(find(popup, 'Skip forward'))
            check('native stopped configurable skip is silent',
                  player.state == 'stopped' and play_count() == before
                  and abs(player.position_seconds - .15) <= .02)
            press(find(popup, 'Play current recording'))
            check('setting A/B points did not enable repeat', not player.repeat_enabled)
            press(find(popup, 'Enable A–B repeat'))
            pump(lambda: bool(repetitions), 'Actual native A–B did not repeat')
            if selected_output is not None:
                check('real selected MCI route confirms output before repeated playback',
                      player.output_device.identity == selected_output.identity
                      and player.active_output_index == selected_output.index)
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
            pump(lambda: player.sleep_remaining_seconds is None and player.state == 'paused',
                 'Actual native sleep timer did not stop playback')
            before = play_count()
            deadline = time.monotonic() + .2
            pump(lambda: time.monotonic() >= deadline, 'Sleep repeat suppression observation')
            check('sleep prevents a later native repeat', play_count() == before and not player.repeat_enabled)
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
            before = play_count()
            player.seek(1.2)
            check('transformed paused seek maps original seconds without opening or playing',
                  not player._open and player.state == 'paused' and play_count() == before
                  and abs(player._to_transport(1.2) - .6) <= .0001
                  and abs(player.position_seconds - 1.2) <= .02)
            press(find(popup, 'Pause or resume recording'))
            pump(lambda: player.position_seconds >= 1.3, 'Transformed native resume did not advance')
            check('explicit transformed resume starts at the measured derivative frame',
                  any(command.startswith('play ') and 'from 600' in command for command in commands))
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
            before = play_count()
            volume = find(popup, 'This player volume, 0 to 100')
            balance = find(popup, 'This player stereo balance, minus 100 left to 100 right')
            mute = find(popup, 'Mute this player only')
            volume.ChangeValue('25')
            balance.ChangeValue('25')
            if not mute.GetValue():
                press(mute)
            press(find(popup, 'Apply player volume, mute and balance'))
            pump(lambda: player.levels.volume == 25 and player.levels.muted,
                 'Actual live-level admission did not complete')
            check('stopped live-level preparation does not start any native output',
                  player.state == 'stopped' and player._wave is None and not player._open
                  and player.levels.volume == 25 and player.levels.muted
                  and player.levels.balance == .25 and play_count() == before)
            press(find(popup, 'Play current recording'))
            pump(lambda: player._wave is not None and player.position_seconds >= .1,
                 'Actual owned WinMM playback with per-instance levels did not advance')
            check('live controls use one real owned WinMM handle',
                  bool(player._wave.handle.value) and player.state == 'playing')
            press(find(popup, 'Pause or resume recording'))
            paused = player.position_seconds
            until = time.monotonic() + .15
            pump(lambda: time.monotonic() >= until, 'Actual muted WinMM pause observation')
            check('live mixer pause retains the same source position',
                  player.state == 'paused' and abs(player.position_seconds - paused) <= .02)
            press(find(popup, 'Stop current recording'))
            check('live mixer Stop releases native buffers before Close',
                  player._wave is None and player.state == 'stopped')
            press(find(popup, 'Close playback controls'))
        except Exception:
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            gui.ExitMainLoop()

    def queue_acceptance():
        from unittest.mock import patch
        from carr_library.speech.playlist import PlaylistStore
        from carr_library.ui.audio_playback import show_audio_playback
        finished, saved_bytes, review_threads = [], [], []
        expected_path, expected_digest = player.path, sha256(player.path.read_bytes()).hexdigest()
        bindings = [binding for binding in frame.keyboard_actions.bindings
            if binding.command.identity.endswith('/Audio playback and saved playlist')]
        check('standalone playback is captured by the existing keyboard registry', len(bindings) == 1)

        def fail():
            errors.append(traceback.format_exc())
            for window in wx.GetTopLevelWindows():
                if isinstance(window, wx.Dialog) and window.IsModal():
                    window.EndModal(wx.ID_CANCEL)
            finished.append(False)

        def wait_popup(callback):
            until = time.monotonic() + 20
            def ready():
                try:
                    found = [window for window in wx.GetTopLevelWindows()
                        if bool(window) and isinstance(window, wx.Dialog) and window.IsModal()
                        and window.IsShown() and window.GetParent() is frame
                        and window.GetTitle() == 'Audio playback and saved playlist']
                    if found:
                        check('exact current playback modal', len(found) == 1)
                        callback(found[0])
                    elif time.monotonic() >= until:
                        raise TimeoutError('The actual shared-worker playback modal did not open.')
                    else:
                        wx.CallLater(25, ready)
                except BaseException:
                    fail()
            wx.CallLater(25, ready)

        def open_menu(callback):
            wait_popup(callback)
            event = wx.CommandEvent(wx.EVT_MENU.typeId, bindings[0].item.GetId())
            check('native existing-registry playback command dispatch',
                  frame.GetEventHandler().ProcessEvent(event))

        def reopened(popup):
            try:
                owner = frame._playback_owner
                check('reopened saved queue remains passive before review',
                      owner.pending_saved is not None and owner.player.path is None
                      and owner.player.playlist == () and not owner.player._open)
                check('metadata reopen preserves the exact saved bytes',
                      owner.store.path.read_bytes() == saved_bytes[0])
                before = owner.player.session_generation
                press(find(popup, 'Review saved queue without playing'))
                pump(lambda: not frame.working and owner.pending_saved is None,
                     'Actual saved queue review did not complete', timeout=30)
                check('explicit saved review restores cursor silently',
                      owner.player.playlist_index == 0 and owner.player.position_seconds > 0
                      and owner.player.state == 'stopped' and not owner.player.queue_active
                      and not owner.player.repeat_enabled and owner.player.sleep_remaining_seconds is None
                      and not owner.player._open and owner.player._wave is None
                      and owner.player.session_generation > before)
                popup.Destroy()
                def destroyed():
                    try:
                        check('actual direct modal destruction releases its playback owner',
                              owner.closed and owner.silence_confirmed and frame._playback_owner is None)
                        check('selected original PCM remains byte-identical',
                              sha256(expected_path.read_bytes()).hexdigest() == expected_digest)
                        finished.append(True)
                    except BaseException:
                        fail()
                wx.CallLater(75, destroyed)
            except BaseException:
                fail()

        def selected(popup):
            try:
                owner = frame._playback_owner
                queue_native_events = []
                actual_queue_mci = owner.player._mci
                def queue_mci(command):
                    try:
                        result = actual_queue_mci(command)
                    except BaseException as error:
                        if len(queue_native_events) < 24:
                            queue_native_events.append({'operation': command.partition(' ')[0],
                                'error': type(error).__name__ + ': ' + str(error)[:512]})
                        raise
                    if not command.startswith('status ') and len(queue_native_events) < 24:
                        queue_native_events.append({'operation': command.partition(' ')[0], 'ok': True})
                    return result
                owner.player._mci = queue_mci
                actual_queue_open = owner.player._mci_open
                def queue_open(path):
                    event = {'operation': 'open', 'api': 'mciSendCommandW',
                             'filename_utf16_units': len(str(path).encode('utf-16-le')) // 2,
                             'suffix': Path(path).suffix}
                    try:
                        result = actual_queue_open(path)
                    except BaseException as error:
                        event['error'] = type(error).__name__ + ': ' + str(error)[:512]
                        if len(queue_native_events) < 24:
                            queue_native_events.append(event)
                        raise
                    event['ok'] = True
                    if len(queue_native_events) < 24:
                        queue_native_events.append(event)
                    return result
                owner.player._mci_open = queue_open
                check('fresh standalone player does not borrow document playback',
                      owner.player is not player and owner.player.path is None)
                actual_prepare = owner.player.prepare_playlist
                def review(paths, **kwargs):
                    review_threads.append(threading.get_ident())
                    return actual_prepare(paths, **kwargs)
                owner.player.prepare_playlist = review
                class SelectedFiles:
                    def __init__(self, *args, **kwargs): pass
                    def __enter__(self): return self
                    def __exit__(self, *args): return False
                    def ShowModal(self): return wx.ID_OK
                    def GetPaths(self): return [str(expected_path)]
                with patch.object(wx, 'FileDialog', SelectedFiles):
                    press(find(popup, 'Replace session playlist...'))
                pump(lambda: not frame.working and len(owner.player.playlist) == 1,
                     'Actual retained queue review did not complete', timeout=30)
                check('actual file review uses the existing desktop worker',
                      review_threads and all(identity != threading.get_ident() for identity in review_threads))
                item = owner.player.playlist[0]
                check('queue retains a hash-bound private copy without autoplay',
                      item.path.parent == library.root / 'state/studio/inputs'
                      and item.path.read_bytes() == expected_path.read_bytes()
                      and item.sha256 == expected_digest and owner.player.state == 'stopped')
                listing = find(popup, 'Reviewed session playlist; choose an item explicitly')
                choose(listing, 0)
                press(find(popup, 'Stop and select item'))
                check('queue selection alone is silent', owner.player.playlist_index == 0
                      and owner.player.state == 'stopped' and not owner.player._open)
                press(find(popup, 'Play queue from selected item'))
                try:
                    pump(lambda: owner.player.state == 'playing' and owner.player.position_seconds >= .08,
                         'Actual queue playback did not advance')
                except BaseException as exc:
                    # Keep the strict native condition; expose the real admission
                    # result rather than inferring a reason from a timeout alone.
                    diagnostic = {
                        'notice': str(owner.notice)[:1024],
                        'status': wx.Window.FindWindowByName(
                            'Current reader playback status', popup).GetValue()[:2048],
                        'cancelled': frame.cancel_event.is_set(),
                        'working': bool(frame.working),
                        'closed': owner.closed,
                        'generation': owner.generation,
                        'session_generation': owner.player.session_generation,
                        'queue_active': owner.player.queue_active,
                        'playlist_index': owner.player.playlist_index,
                        'mci_open': owner.player._open,
                        'wave_open': owner.player._wave is not None,
                        'selected_output': (owner.player.output_device.identity
                                            if owner.player.output_device is not None else None),
                        'native_events': queue_native_events,
                    }
                    for name in ('state', 'position_seconds', 'duration_seconds'):
                        try:
                            diagnostic[name] = getattr(owner.player, name)
                        except BaseException as error:
                            diagnostic[name] = type(error).__name__ + ': ' + str(error)[:512]
                    raise RuntimeError('Actual queue playback did not advance: ' +
                                       json.dumps(diagnostic, sort_keys=True)) from exc
                check('retained queue PCM opens through the typed Unicode MCI element',
                      owner.player.backend == 'Windows MCI'
                      and any(event.get('api') == 'mciSendCommandW' and event.get('ok')
                              and event.get('suffix') == '.audio' for event in queue_native_events))
                press(find(popup, 'Pause or resume recording'))
                check('actual queue pause keeps the retained original frame', owner.player.state == 'paused')
                press(find(popup, 'Save playback queue'))
                saved, token = PlaylistStore(library.root).load()
                check('native Save records exact source hash and positive original-frame cursor',
                      saved.items[0].sha256 == expected_digest and saved.frame > 0
                      and token == owner.token)
                press(find(popup, 'Close playback controls'))
                def closed():
                    try:
                        check('normal queue Close confirms native silence', owner.closed and owner.silence_confirmed)
                        saved_bytes.append(owner.store.path.read_bytes())
                        open_menu(reopened)
                    except BaseException:
                        fail()
                wx.CallLater(75, closed)
            except BaseException:
                fail()

        open_menu(selected)
        pump(lambda: bool(finished), 'Native queue and saved-review acceptance did not finish', timeout=90)
        check('native queue and direct-destruction scenario completed', finished == [True])

    def scenario():
        try:
            wx.CallLater(100, drive)
            reader_playback.show_reader_playback(frame, player,
                execute=execute, stop=player.stop,
                pause=lambda: player.resume() if player.state == 'paused' else player.pause(),
                subscribe=lambda callback: subscriptions.__setitem__(0, callback),
                prepare_listening=prepare, review_outputs=review_outputs)
            check('closed controls release their status subscription', subscriptions[0] is None)
            timer.Stop()
            player.stop()
            queue_acceptance()
        except Exception:
            errors.append(traceback.format_exc())
        finally:
            gui.ExitMainLoop()

    try:
        frame.Show()
        gui.SetTopWindow(frame)
        pump(lambda: frame.active == (workspace, None) and not frame.working and not frame.busy,
             'Actual desktop initialization did not complete', timeout=25)
        timer.Start(100)
        wx.CallAfter(scenario)
        gui.MainLoop()
        if errors:
            raise RuntimeError('\n'.join(errors))
    finally:
        cancel.set()
        frame.cancel_event.set()
        frame._closing = True
        frame.executor.shutdown(wait=True, cancel_futures=True)
        for value in prepared_values:
            if value._owner is None:
                value.close()
        timer.Stop()
        frame.operation_progress.unregister_player(player, silence_confirmed=not player._open and player._wave is None)
        frame.operation_progress.close()
        frame.Destroy()
        gui.Yield()


def _run(options, report):
    if options.source is not None:
        sys.path.insert(0, str(options.source.resolve(strict=True) / 'src'))
    from carr_library.native_runtime import activate_bundled_runtime
    handle = activate_bundled_runtime()
    from carr_library.speech import player as implementation
    from carr_library.speech import output_devices, listening
    from carr_library.audio import AudioRuntime
    from carr_library.audio.runtime import resolve_active_audio_tool
    from carr_library.operations import ResourceBroker
    import carr_library
    checks = report['checks']

    def checkpoint():
        report['count'] = len(checks)
        if options.report is not None:
            options.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        checkpoint()
        print('PASS: ' + name, flush=True)

    def wait_for(predicate):
        deadline = time.monotonic() + 8
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError('Actual Windows playback did not reach its measured state.')
            time.sleep(.02)

    try:
        checkout = Path(__file__).resolve().parents[1]
        if options.source is None:
            from importlib.metadata import distributions
            installed = _installed_package_root(distributions(name='universal-research-library'),
                                                carr_library.__file__, checkout)
        else:
            installed = None
        source_identity = {}
        names = ('carr_library.speech.player', 'carr_library.speech.output_devices',
                 'carr_library.speech._wave_output', 'carr_library.speech.listening',
                 'carr_library.speech.playlist', 'carr_library.ui.reader_playback',
                 'carr_library.ui.reader_transport', 'carr_library.ui.audio_playback',
                 'carr_library.ui.speech_dialog', 'carr_library.ui.desktop_app')
        for name in names:
            module = importlib.import_module(name)
            loaded = Path(module.__file__).resolve(strict=True)
            expected = checkout / 'src' / Path(*name.split('.')).with_suffix('.py')
            check('exact imported playback module: ' + name,
                  sha256(loaded.read_bytes()).digest() == sha256(expected.read_bytes()).digest()
                  and (loaded.is_relative_to(options.source.resolve() / 'src') if options.source is not None
                       else loaded.is_relative_to(installed) and not loaded.is_relative_to(checkout)))
            source_identity[name] = {'path': str(loaded), 'sha256': sha256(loaded.read_bytes()).hexdigest()}
        report['source_identity'] = source_identity
        outputs = output_devices.output_devices()
        check('actual Windows output inventory stays within its bound',
              len(outputs) <= output_devices._MAX_DEVICES)
        selected = (next((value for value in outputs if value.identity == options.expected_device_identity), None)
                    if options.expected_device_identity else (outputs[0] if outputs else None))
        if options.require_output_device or options.expected_device_identity:
            check('required explicitly identified output exists', selected is not None)
        tools = {name: Path(getattr(options, name) or resolve_active_audio_tool(name)).resolve(strict=True)
                 for name in ('ffmpeg', 'ffprobe')}
        audio_proof = {}
        def runtime_factory(directory, *, budget):
            runtime = AudioRuntime(directory, ffmpeg=tools['ffmpeg'], ffprobe=tools['ffprobe'], budget=budget)
            for name in ('ffmpeg', 'ffprobe'):
                binary = Path(getattr(runtime, name)).resolve(strict=True)
                check('exact declared listening tool: ' + name, binary == tools[name])
                with binary.open('rb') as stream:
                    digest = file_digest(stream, 'sha256').hexdigest()
                audio_proof[name] = {'path': str(binary), 'sha256': digest}
            return runtime

        with TemporaryDirectory(prefix='carr-native-player-') as temporary:
            root = Path(temporary)
            resource_root = root / 'resource-library'
            resource_root.mkdir()
            broker = ResourceBroker.for_app(resource_root)
            path = root / 'Silent synthetic PCM — Україна.wav'
            with wave.open(str(path), 'wb') as stream:
                stream.setparams((2, 2, 24000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\0\0' * 144000)
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
                check('paused seek issues no native Play or Resume',
                      not any(command.startswith(('play ', 'resume ')) for command in commands[first:]))
                check('paused seek preserves logical pause without an open device',
                      player.state == 'paused' and not player._open
                      and abs(player.position_seconds - .7) <= .02)
                position = player.position_seconds
                time.sleep(.15)
                check('seek remains silent until explicit resume',
                      not player._open and abs(player.position_seconds - position) <= .02)
                check('explicit resume accepted', player.resume() is True)
                wait_for(lambda: player.position_seconds >= .78)
                check('actual resumed playback uses the reviewed seek position',
                      any(command.startswith('play ') and 'from 700' in command for command in commands))
                player.stop()
                check('native Stop ends playback', player.state == 'stopped')
                native_controls(player, check, commands, selected, root, runtime_factory, broker)
            finally:
                player.close()
            check('native Close confirms all owned output closure',
                  not player._open and player._wave is None and player.state == 'stopped')
            renamed = path.with_name('Closed native file.wav')
            path.rename(renamed)
            check('closed PCM is unchanged and releasable', renamed.read_bytes() == original)
            renamed.unlink()
            report.update(status='PASS', actual_windows_mci='PASS',
                synthetic_pcm_sha256=sha256(original).hexdigest(),
                actual_listening_conversion_and_mapping='PASS', listening_audio_runtime=audio_proof,
                actual_windows_output_inventory='PASS', output_count=len(outputs),
                selected_mci_output='PASS' if selected is not None else 'NOT_RUN_NO_IDENTIFIED_OUTPUT',
                selected_output_identity=selected.identity if selected is not None else None,
                paused_seek_no_play_command='PASS', native_ab_sleep_skip_controls='PASS',
                native_owned_volume_controls='PASS', native_retained_playlist='PASS',
                native_direct_modal_destruction='PASS')
            checkpoint()
    finally:
        if handle is not None:
            handle.close()


def main():
    from argparse import ArgumentParser
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--report', '--output', dest='report', type=Path)
    parser.add_argument('--require-output-device', action='store_true')
    parser.add_argument('--expected-device-identity')
    parser.add_argument('--ffmpeg')
    parser.add_argument('--ffprobe')
    options = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('Actual Windows and native playback are required.')
    report = {'status': 'FAILED', 'checks': [], 'count': 0,
              'fixture': 'purpose-written silent PCM', 'source_override': str(options.source) if options.source else None,
              'audible_listening': 'NOT_RUN', 'nvda': 'NOT_RUN', 'voice_inference': 'NOT_RUN',
              'independent_deadline_seconds': 300,
              'failure': 'The probe has not reached final native reporting.'}
    if options.report is not None:
        options.report.parent.mkdir(parents=True, exist_ok=True)
        options.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    faulthandler.dump_traceback_later(300, exit=True)
    try:
        try:
            _run(options, report)
        except BaseException:
            report['status'] = 'FAILED'
            report['failure'] = traceback.format_exc()
        else:
            report.pop('failure', None)
        report['count'] = len(report['checks'])
        if options.report is not None:
            options.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 0 if report['status'] == 'PASS' else 1
    finally:
        faulthandler.cancel_dump_traceback_later()


if __name__ == '__main__':
    raise SystemExit(main())
