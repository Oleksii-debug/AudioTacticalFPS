"""Actual Windows saved queue, owned live mixer and native modal cleanup.

Uses silent synthetic stereo PCM and the canonical WavePlayer. Requires real
WinMM and wx; missing native prerequisites fail this probe. No listening-quality
or NVDA qualification is inferred from native API acknowledgements.
"""
import argparse
import ctypes as C
import faulthandler
from ctypes import wintypes
from hashlib import sha256
import json
import os
from pathlib import Path
import sysconfig
from tempfile import TemporaryDirectory
import time
import traceback
import threading
import wave


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installed', action='store_true')
    parser.add_argument('--failure-cleanup-only', action='store_true',
                        help='Prove only controlled passive-owner failure cleanup; playback stays NOT_RUN.')
    options = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('This probe requires actual Windows playback and its interactive desktop.')
    import wx
    from carr_library.application import LibraryApp
    from carr_library.speech import player, playlist, _wave_output
    from carr_library.ui import audio_playback, reader_playback, desktop_app
    from carr_library.studio import clips
    from carr_library.speech import playback_devices
    from carr_library.ui import save_all
    from carr_library.operations import broker as resource_broker
    from carr_library import backup_playback_queue

    checks, errors, destroyed = [], [], []
    owned_modals, playback_owners, scheduled = [], [], []
    cleanup_state = {'started': False, 'worker_drained': False}
    def check(label, value):
        if not value:
            raise AssertionError(label)
        checks.append(label)
        print('PASS: ' + label, flush=True)
    identities = {}
    checkout = Path(__file__).resolve().parents[1]
    installed = (Path(os.environ['CARR_PROGRAM_DIR']).resolve() if os.environ.get('CARR_PROGRAM_DIR')
                 else Path(sysconfig.get_path('purelib')).resolve())
    modules = (player, playlist, _wave_output, playback_devices, audio_playback,
               reader_playback, desktop_app, clips, save_all, resource_broker, backup_playback_queue)
    for module in modules:
        actual = Path(module.__file__).resolve()
        identities[module.__name__] = sha256(actual.read_bytes()).hexdigest()
        if options.installed:
            expected = checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')
            check('exact installed player module: ' + module.__name__,
                  actual.is_relative_to(installed) and not actual.is_relative_to(checkout)
                  and identities[module.__name__] == sha256(expected.read_bytes()).hexdigest())

    def cleanup_step(label, operation):
        try:
            operation()
            return True
        except BaseException:
            detail = label + ':\n' + traceback.format_exc()
            errors.append(detail)
            try:
                print('FAIL: saved playback cleanup ' + json.dumps(detail[:8192], ensure_ascii=True), flush=True)
            except Exception:
                pass  # A failed diagnostic stream must not abandon later owners.
            return False

    def schedule(delay, callback):
        timer = wx.CallLater(delay, callback)
        scheduled.append(timer)
        return timer

    def own_modal(window):
        loop = wx.EventLoopBase.GetActive()
        observe_stage('modal-admission', parent_is_host=window.GetParent() is frame,
                      modal=bool(window.IsModal()), active_loop=loop is not None,
                      active_main=bool(loop is not None and loop.IsMain()),
                      active_running=bool(loop is not None and loop.IsRunning()))
        if (window.GetParent() is not frame or not window.IsModal()
                or loop is None or loop.IsMain() or not loop.IsRunning()):
            raise AssertionError('Playback cleanup requires its actual owned running modal.')
        handle = int(window.GetHandle())
        if not any(item[0] is window and item[1] == handle for item in owned_modals):
            owned_modals.append((window, handle, loop))
        owner = frame._playback_owner
        if owner is not None and not any(value is owner for value in playback_owners):
            playback_owners.append(owner)

    def unwind():
        # A suspended parent loop must not be ended while its child is active.
        for window, handle, loop in reversed(owned_modals):
            try:
                eligible = (user32.IsWindow(handle) and int(window.GetHandle()) == handle
                            and window.IsModal() and loop.IsRunning() and not loop.IsMain())
            except RuntimeError:
                continue  # A recorded native dialog has already been destroyed.
            if eligible:
                # EndModal requests loop exit; IsModal/IsRunning may remain
                # true until the current pending-event batch returns. Reposting
                # CallAfter here can keep that same batch alive indefinitely.
                requested = cleanup_state.setdefault('requested_modal_ends', set())
                ticket = (id(window), handle, id(loop))
                if ticket not in requested:
                    requested.add(ticket)
                    if not cleanup_step('owned playback modal', lambda: window.EndModal(wx.ID_CANCEL)):
                        return
                if not cleanup_state.get('modal_unwind_pending', False):
                    cleanup_state['modal_unwind_pending'] = True
                    def next_native_turn():
                        cleanup_state['modal_unwind_pending'] = False
                        unwind()
                    schedule(25, next_native_turn)
                return

    def fail():
        detail = traceback.format_exc()
        errors.append(detail)
        try:
            print('SAVED_PLAYBACK_CALLBACK_FAILURE: ' + json.dumps(detail[:8192], ensure_ascii=True), flush=True)
        except Exception:
            pass
        wx.CallAfter(unwind)

    class App(wx.App):
        def OnExceptionInMainLoop(self):
            fail()
            return True
    app = App(False)
    app.SetExitOnFrameDelete(False)
    simulator = wx.UIActionSimulator()
    user32 = C.WinDLL('user32', winmode=0x0800, use_last_error=True)
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = ()
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.restype = wintypes.BOOL

    def pump(predicate, label, timeout=12, *, allow_errors=False):
        deadline = time.monotonic() + timeout
        while not predicate():
            if errors and not allow_errors:
                raise RuntimeError('\n'.join(errors))
            if time.monotonic() >= deadline:
                raise TimeoutError(label)
            app.Yield()
            time.sleep(.01)

    def settle(seconds=.1):
        end = time.monotonic() + seconds
        pump(lambda: time.monotonic() >= end, 'Native playback event settlement')

    def named(dialog, label):
        control = wx.Window.FindWindowByName(label, dialog)
        check('native playback control: ' + label, control is not None)
        return control

    def press(control):
        top = wx.GetTopLevelParent(control)
        top.Raise()
        user32.SetForegroundWindow(top.GetHandle())
        control.SetFocus()
        pump(lambda: wx.Window.FindFocus() is control and
             user32.GetForegroundWindow() == top.GetHandle(), 'Native playback control focus')
        check('actual keyboard Space', simulator.Char(wx.WXK_SPACE))
        settle()

    def observe_stage(event, **values):
        try:
            print('SAVED_PLAYBACK_STAGE: ' + json.dumps(
                {'event': event, **values}, ensure_ascii=True), flush=True)
        except Exception:
            pass

    def observe_close(event, **values):
        try:
            print('SAVED_PLAYBACK_HOST_CLOSE: ' + json.dumps(
                {'event': event, **values}, ensure_ascii=True), flush=True)
        except Exception:
            pass  # Observation cannot change a Close result or cleanup ordering.

    def close_host():
        host_cancel.set()
        if not user32.IsWindow(host_handle):
            return
        pump(lambda: not frame.working, 'Failure-path playlist review cleanup', allow_errors=True)
        before = {'working': bool(frame.working), 'busy': bool(frame.busy),
                  'closing': bool(frame._closing), 'main_loop': bool(app.IsMainLoopRunning()),
                  'destroy_event_seen': bool(destroyed)}
        try:
            before['status'] = frame.status.GetValue()[:2048]
        except Exception as error:
            before['status_error'] = type(error).__name__[:64]
        accepted = bool(frame.Close())
        observe_close('requested', accepted=accepted, before=before,
                      native_window_present=bool(user32.IsWindow(host_handle)))
        if not accepted:
            raise RuntimeError('The actual desktop refused ordinary Close; its owners remain retained.')
        try:
            pump(lambda: not user32.IsWindow(host_handle),
                 'Failure-path desktop close', allow_errors=True)
        finally:
            observe_close('settled', native_window_present=bool(user32.IsWindow(host_handle)),
                          destroy_event_seen=bool(destroyed))

    def finish_host(*, exit_loop=False):
        # Preserve the original failure and attempt every independent owner,
        # including the retained executor even if ordinary Close is refused.
        if not cleanup_state['started']:
            cleanup_state['started'] = True
            for timer in scheduled:
                cleanup_step('pending probe callback', timer.Stop)
            cleanup_step('host cancellation', host_cancel.set)
            cleanup_step('independent WavePlayer', other.close)
            for owner in playback_owners:
                cleanup_step('standalone PlaybackOwner', owner.close)
            close_ok = cleanup_step('actual desktop Close', close_host)
            def drain():
                executor.shutdown(wait=True, cancel_futures=True)
                cleanup_state['worker_drained'] = True
            drained = cleanup_step('existing desktop executor', drain)
            cleanup_step('native ownership acknowledgement', lambda: check(
                'actual host Close drains saved playback owners and the existing worker',
                close_ok and drained and cleanup_state['worker_drained']
                and not user32.IsWindow(host_handle) and not executor.cleanup_failures
                and not _wave_output._UNRELEASED_OUTPUTS
                and all(owner.silence_confirmed for owner in playback_owners)))
        if exit_loop:
            loop = wx.EventLoopBase.GetActive()
            if loop is not None and loop.IsMain() and loop.IsRunning():
                app.ExitMainLoop()

    def result_report():
        playback = 'NOT_RUN' if options.failure_cleanup_only else 'PASS'
        return {'status': 'PASS', 'checks': checks, 'source_identity': identities,
            'installed_modules': 'PASS' if options.installed else 'NOT_RUN',
            'mode': 'failure-cleanup-only' if options.failure_cleanup_only else 'full-playback',
            'failure_cleanup': 'PASS',
            'actual_windows_waveout': playback, 'actual_native_queue_controls': playback,
            'instance_volume_isolation': playback, 'physical_device': 'NOT_RUN',
            'audible_listening': 'NOT_RUN', 'nvda': 'NOT_RUN'}

    with TemporaryDirectory(prefix='resonance-playlist-native-') as temporary:
        root = Path(temporary)
        files = []
        originals = {}
        for index in range(2):
            path = root / f'Черга — silent stereo {index}.wav'
            with wave.open(str(path), 'wb') as stream:
                stream.setparams((2, 2, 24000, 0, 'NONE', 'uncompressed'))
                stream.writeframes(b'\0\0\0\0' * 72000)
            files.append(path)
            originals[path.name] = sha256(path.read_bytes()).hexdigest()
        library = LibraryApp(root / 'library')
        library.create_workspace('Native playlist acceptance')
        frame = desktop_app.DesktopFrame(library)
        executor = frame.executor
        host_handle, host_cancel = int(frame.GetHandle()), frame.cancel_event
        frame.Bind(wx.EVT_WINDOW_DESTROY,
            lambda event: (destroyed.append(True) if event.GetEventObject() is frame else None, event.Skip()))
        original_picker = wx.FileDialog
        picked = []
        other = player.WavePlayer()

        def picker(*args, **kwargs):
            # Still the real Windows file chooser. Preselect one controlled
            # synthetic file and accept it through the actual keyboard.
            dialog = original_picker(*args, **kwargs)
            target = files[len(picked)]
            picked.append(target)
            dialog.SetPath(str(target))
            def accept():
                check('native file chooser Enter', simulator.Char(wx.WXK_RETURN))
            schedule(250, accept)
            return dialog

        def controls():
            try:
                popup, = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.Dialog) and window.IsModal()
                           and window.GetParent() is frame
                           and window.GetTitle() == 'Audio playback and saved playlist']
                own_modal(popup)
                owner = frame._playback_owner
                current = owner.player
                check('standalone opens without a source or native sound',
                      current.path is None and current._wave is None and not current._open)
                press(named(popup, 'Replace session playlist...'))
                pump(lambda: not frame.working and len(current.playlist) == 1,
                     'First native selected file was not reviewed')
                press(named(popup, 'Add files to session playlist...'))
                pump(lambda: not frame.working and len(current.playlist) == 2,
                     'Second native selected file was not reviewed')
                check('actual file picker order and original hashes',
                      picked == files and tuple(item.display_name for item in current.playlist) == tuple(path.name for path in files)
                      and all(item.sha256 == originals[item.display_name]
                              and item.path.parent == library.root / 'state' / 'studio' / 'inputs'
                              for item in current.playlist))
                check('review is passive', current.path is None and current._wave is None and not current._open)
                press(named(popup, 'Stop and select item'))
                check('queue selection is silent', current.path == current.playlist[0].path and current.state == 'stopped'
                      and current._wave is None)
                named(popup, 'This player volume, 0 to 100').ChangeValue('43')
                press(named(popup, 'Apply player volume, mute and balance'))
                check('stopped live setting does not open a device',
                      current.levels.volume == 43 and current._wave is None)
                press(named(popup, 'Play queue from selected item'))
                pump(lambda: current.position_seconds >= .05, 'Real waveOut did not advance')
                check('same canonical player owns bounded native output',
                      current.backend == 'Windows waveOut' and current._wave.handle.value
                      and len(current._wave.pending) <= 4 and not current._open)
                press(named(popup, 'Pause or resume recording'))
                paused_position = current.position_seconds
                settle(.15)
                check('actual pause is stable and disarms queue advancement',
                      current.state == 'paused' and not current.queue_active
                      and abs(current.position_seconds - paused_position) < .02)

                # A second real instance is a control for accidental device-wide
                # mixer changes. Its recording is also silent synthetic PCM.
                other.set_levels(61)
                other.play(files[1])
                other.pause()
                current_device, other_device = C.c_uint32(), C.c_uint32()
                check('independent handles refer to the same Windows output',
                      current._wave.handle.value != other._wave.handle.value
                      and current._wave.library.waveOutGetID(current._wave.handle, C.byref(current_device)) == 0
                      and other._wave.library.waveOutGetID(other._wave.handle, C.byref(other_device)) == 0
                      and current_device.value == other_device.value)
                other_before = C.c_uint32()
                check('read independent owned output level',
                      other._wave.library.waveOutGetVolume(other._wave.handle, C.byref(other_before)) == 0)
                named(popup, 'This player stereo balance, minus 100 left to 100 right').ChangeValue('-25')
                press(named(popup, 'Mute this player only'))
                press(named(popup, 'Apply player volume, mute and balance'))
                current_level, other_after = C.c_uint32(), C.c_uint32()
                check('read muted owned output level',
                      current._wave.library.waveOutGetVolume(current._wave.handle, C.byref(current_level)) == 0)
                check('read unaffected independent output level',
                      other._wave.library.waveOutGetVolume(other._wave.handle, C.byref(other_after)) == 0)
                check('live mute and balance preserve volume and other player',
                      current.levels.muted and current.levels.volume == 43 and current.levels.balance == -.25
                      and current_level.value == 0 and other_before.value == other_after.value)
                check('live paused controls never resume audio', current.state == 'paused'
                      and abs(current.position_seconds - paused_position) < .02)
                other.close()
                press(named(popup, 'Mute this player only'))
                press(named(popup, 'Apply player volume, mute and balance'))
                check('unmute retains reviewed native level', not current.levels.muted and current.levels.volume == 43)
                applied = C.c_uint32()
                check('actual unmuted stereo balance matches the owned native handle',
                      current._wave.library.waveOutGetVolume(current._wave.handle, C.byref(applied)) == 0
                      and applied.value == current.levels.packed)
                press(named(popup, 'Play queue from selected item'))
                pump(lambda: current.playlist_index == 1, 'Real EOF did not admit the next reviewed item', timeout=10)
                check('actual queue transition keeps levels and exact next file',
                      current.path == current.playlist[1].path and current.playlist[1].display_name == files[1].name
                      and current.levels.volume == 43 and current.queue_active)
                press(named(popup, 'Stop current recording'))
                settle(.2)
                check('Stop closes owned native output and cancels queue',
                      current.state == 'stopped' and not current.queue_active and current._wave is None)
                press(named(popup, 'Stop and move item down'))
                check('native reorder is passive', current.playlist[0].display_name == files[1].name
                      and current.path is None and not current.queue_active)
                press(named(popup, 'Stop and select item'))
                named(popup, 'Forward skip in seconds, 0.1 to 600').ChangeValue('.375')
                press(named(popup, 'Apply skip intervals'))
                press(named(popup, 'Skip forward'))
                check('silent original-frame cursor before save', current.position_seconds == .375
                      and current.state == 'stopped' and current._wave is None)
                save = named(popup, 'Save playback queue')
                save.SetFocus()
                pump(lambda: wx.Window.FindFocus() is save, 'Save queue keyboard focus')
                check('actual Ctrl+S down', simulator.KeyDown(wx.WXK_CONTROL))
                try:
                    check('actual Ctrl+S character', simulator.Char(ord('S')))
                finally:
                    check('actual Ctrl+S up', simulator.KeyUp(wx.WXK_CONTROL))
                settle()
                saved, token = playlist.PlaylistStore(library.root).load()
                check('Ctrl+S durably saved order and exact original-frame cursor',
                      token is not None and saved.frame == 9000 and saved.index == 0
                      and [item.label for item in saved.items] == [files[1].name, files[0].name])
                press(named(popup, 'Close playback controls'))
            except Exception:
                fail()

        def recovery_controls():
            try:
                popup, = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.Dialog) and window.IsModal()
                           and window.GetParent() is frame
                           and window.GetTitle() == 'Audio playback and saved playlist']
                own_modal(popup)
                owner = frame._playback_owner
                current = owner.player
                check('restart loads passive metadata without opening audio',
                      owner.pending_saved is not None and current.playlist == ()
                      and current.path is None and current._wave is None and not current._open)
                press(named(popup, 'Review saved queue without playing'))
                pump(lambda: not frame.working and owner.pending_saved is None
                     and len(current.playlist) == 2, 'Retained queue review after original relocation')
                check('explicit review restores original cursor and settings without autoplay',
                      current.state == 'stopped' and not current.queue_active
                      and current.position_seconds == .375 and current.playlist_index == 0
                      and current.levels.volume == 43 and current.levels.balance == -.25
                      and current._wave is None and not current._open
                      and [item.display_name for item in current.playlist] == [files[1].name, files[0].name])
                press(named(popup, 'Close playback controls'))
            except Exception:
                fail()

        controlled_failure = {'error': RuntimeError('Controlled passive playback worker failure.')}

        def failure_controls():
            try:
                observe_stage('failure-controls-entered', modal_candidates=sum(
                    isinstance(window, wx.Dialog) and window.IsModal()
                    and window.GetParent() is frame for window in wx.GetTopLevelWindows()))
                popup, = [window for window in wx.GetTopLevelWindows()
                           if isinstance(window, wx.Dialog) and window.IsModal()
                           and window.GetParent() is frame
                           and window.GetTitle() == 'Audio playback and saved playlist']
                own_modal(popup)
                controlled_failure['owner'] = frame._playback_owner
                observe_stage('failure-owner-recorded', host_working=bool(frame.working),
                              owner_present=frame._playback_owner is not None)
                def work():
                    raise controlled_failure['error']
                def failed(error):
                    controlled_failure['observed'] = error
                    observe_stage('failure-callback', exact_error=error is controlled_failure['error'],
                                  host_working=bool(frame.working))
                    wx.CallAfter(unwind)
                future = frame.run_job('Controlled passive playback worker failure', work, failed=failed)
                if future is None:
                    raise AssertionError('The controlled worker failure was not admitted.')
                controlled_failure['future'] = future
                observe_stage('failure-job-admitted', future_done=bool(future.done()))
            except Exception:
                fail()

        def scenario():
            try:
                wx.FileDialog = picker
                specification = frame.keyboard_commands.registry.get('audio.playback')
                item = frame.keyboard_commands.bindings['audio.playback'].item
                check('one stable native command without a conflicting default shortcut',
                      specification.command_id == 'audio.playback'
                      and specification.context == 'main' and specification.default_shortcut == '')
                schedule(100, failure_controls)
                observe_stage('failure-menu-invocation', host_working=bool(frame.working),
                              host_closing=bool(frame._closing))
                frame.GetEventHandler().ProcessEvent(wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId()))
                observe_stage('failure-menu-returned', errors=len(errors))
                if errors:
                    raise RuntimeError('\n'.join(errors))
                observed_owner = controlled_failure['owner']
                check('controlled worker failure closes only its owned passive playback modal',
                      controlled_failure.get('observed') is controlled_failure['error']
                      and controlled_failure['future'].done()
                      and controlled_failure['future'].exception() is controlled_failure['error']
                      and observed_owner.closed and observed_owner.silence_confirmed
                      and frame._playback_owner is None and not _wave_output._UNRELEASED_OUTPUTS
                      and observed_owner.player.path is None and not frame.working
                      and frame.executor is executor and not frame._closing
                      and user32.IsWindow(host_handle))
                if options.failure_cleanup_only:
                    return  # The same finally must still Close and drain the actual host.
                schedule(100, controls)
                frame.GetEventHandler().ProcessEvent(wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId()))
                if errors:
                    raise RuntimeError('\n'.join(errors))
                check('native modal close acknowledges player ownership', frame._playback_owner is None
                      and not _wave_output._UNRELEASED_OUTPUTS)
                for path in files:
                    check('original PCM remains exact after live controls', sha256(path.read_bytes()).hexdigest() == originals[path.name])
                    renamed = path.with_name('Closed ' + path.name)
                    path.rename(renamed)
                    check('closed source handle is released', renamed.is_file())
                schedule(100, recovery_controls)
                frame.GetEventHandler().ProcessEvent(wx.CommandEvent(wx.EVT_MENU.typeId, item.GetId()))
                if errors:
                    raise RuntimeError('\n'.join(errors))
                check('saved queue reopens after original files moved', frame._playback_owner is None
                      and not _wave_output._UNRELEASED_OUTPUTS)
            except Exception:
                errors.append(traceback.format_exc())
            finally:
                # Close/drain before leaving the original main loop, on both
                # success and failure. The captured HWND is the acknowledgement.
                finish_host(exit_loop=True)

        try:
            frame.Show()
            app.SetTopWindow(frame)
            wx.CallAfter(scenario)
            app.MainLoop()
        finally:
            try:
                finish_host()
            finally:
                wx.FileDialog = original_picker
        if errors:
            raise RuntimeError('\n'.join(errors))
    check('all installed queue sources stay exact after native execution',
          all(sha256(Path(module.__file__).resolve().read_bytes()).hexdigest() == identities[module.__name__]
              and identities[module.__name__] == sha256(
                  (checkout / 'src' / Path(*module.__name__.split('.')).with_suffix('.py')).read_bytes()).hexdigest()
              for module in modules))
    print(json.dumps(result_report(), ensure_ascii=True), flush=True)


if __name__ == '__main__':
    def expired():
        print('FAIL: native saved queue and owned-output probe exceeded 90 seconds.', flush=True)
        try:
            faulthandler.dump_traceback(all_threads=True)
        finally:
            os._exit(2)
    watchdog = threading.Timer(90, expired)
    watchdog.daemon = True
    watchdog.start()
    native = None
    try:
        from carr_library.native_runtime import activate_bundled_runtime
        native = activate_bundled_runtime()
        main()
    finally:
        try:
            if native is not None:
                native.close()
        finally:
            watchdog.cancel()
